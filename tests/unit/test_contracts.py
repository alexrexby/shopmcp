"""Stage 0 frozen contracts: stage-1 modules are built against exactly these."""

import inspect
import json
import re
from pathlib import Path
from typing import Annotated

import pytest
from fakes import FakeAdapter, make_product
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import TypeAdapter

from shopmcp import i18n
from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterBase, AdapterContext, SearchQuery, ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.config import AdapterConfig, ConfigError, load
from shopmcp.domain.models import PageMeta, Product, ProductPage, ProductSummary
from shopmcp.domain.text import clean_text
from shopmcp.envelope import fail, ok
from shopmcp.errors import InvalidInput, NotFound, NotSupported, ShopError, Unavailable
from shopmcp.i18n import L10n, Texts
from shopmcp.tools.spec import ToolSpec, with_param_docs

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "stores/demo/store.toml"
CYRILLIC = re.compile("[а-яё]", re.IGNORECASE)


# --- adapter contract ---------------------------------------------------------

PROTOCOL_METHODS = [
    n for n, v in vars(ShopAdapter).items() if callable(v) and not n.startswith("_")
]


def test_protocol_lists_every_method():
    assert len(PROTOCOL_METHODS) == 13


def test_fake_adapter_satisfies_protocol():
    assert isinstance(FakeAdapter(), ShopAdapter)


@pytest.mark.parametrize("impl", [FakeAdapter, AdapterBase])
@pytest.mark.parametrize("name", PROTOCOL_METHODS)
def test_signatures_match_protocol(impl, name):
    # runtime_checkable only checks names; this pins parameters and async-ness too.
    want, got = getattr(ShopAdapter, name), getattr(impl, name)
    assert list(inspect.signature(got).parameters) == list(inspect.signature(want).parameters)
    assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(want)


@pytest.mark.anyio
async def test_adapter_base_raises_not_supported():
    with pytest.raises(NotSupported):
        await AdapterBase().search(SearchQuery(q="x"))
    assert AdapterBase().capabilities() == frozenset()


@pytest.mark.anyio
async def test_fake_adapter_capabilities_are_configurable():
    fake = FakeAdapter(capabilities={Capability.SEARCH})
    items, total, _ = await fake.search(SearchQuery(q="молоко"))
    assert [p.id for p in items] == ["1"] and total == 1
    with pytest.raises(NotSupported):
        await fake.by_barcode("4600000000017")


def test_registry_unknown_kind_is_config_error():
    ctx = AdapterContext(data_dir=Path("."), http_timeout=5, store_id="t")
    with pytest.raises(ConfigError, match="unknown"):
        registry.create(AdapterConfig(kind="nope"), ctx)
    registry.register("fake", lambda cfg, ctx: FakeAdapter())
    assert isinstance(registry.create(AdapterConfig(kind="fake"), ctx), FakeAdapter)


# --- text --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("180&nbsp;г<br>x", "180 г x"),
        ("a &amp;nbsp; b", "a b"),
        ("<p>Состав:</p><ul><li>молоко</li></ul>", "Состав: молоко"),
        ("<b>Мол</b>око", "Молоко"),
        ("<![CDATA[<b>x</b>]]>y", "xy"),
        ("   ", None),
        (None, None),
    ],
)
def test_clean_text(raw, clean):
    assert clean_text(raw) == clean


# --- config ------------------------------------------------------------------


def test_demo_config_is_valid():
    cfg = load(DEMO)
    assert (cfg.prefix, cfg.locale, cfg.cart.strategy) == ("demo", "ru", "url_template")
    assert cfg.adapter.kind == "yml" and cfg.adapter.model_extra["feed"] == "feed.xml"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "store.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _variant(old: str, new: str) -> str:
    src = DEMO.read_text(encoding="utf-8")
    assert old in src
    return src.replace(old, new, 1)


def test_bad_prefix_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="prefix"):
        load(_write(tmp_path, _variant('prefix = "demo"', 'prefix = "Bad-Prefix"')))


def test_bad_locale_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="locale"):
        load(_write(tmp_path, _variant('locale = "ru"', 'locale = "de"')))


def test_locale_defaults_to_ru(tmp_path):
    assert load(_write(tmp_path, _variant('locale = "ru"', ""))).locale == "ru"


def test_env_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_FEED", "https://demo.example/feed.xml")
    cfg = load(_write(tmp_path, _variant('"feed.xml"', '"${DEMO_FEED}"')))
    assert cfg.adapter.model_extra["feed"] == "https://demo.example/feed.xml"


def test_missing_env_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_FEED", raising=False)
    with pytest.raises(ConfigError, match="DEMO_FEED"):
        load(_write(tmp_path, _variant('"feed.xml"', '"${DEMO_FEED}"')))


def test_config_error_does_not_echo_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_SECRET", "s3cr3t-value")
    with pytest.raises(ConfigError) as e:
        load(_write(tmp_path, _variant("port = 8000", 'port = "${SHOP_SECRET}"')))
    assert "s3cr3t-value" not in str(e.value)


def test_url_template_requires_items_placeholder(tmp_path):
    with pytest.raises(ConfigError, match="url_template"):
        load(_write(tmp_path, _variant("cart/add?{items}", "cart/add")))


# --- envelope ----------------------------------------------------------------


def _product() -> Product:
    return make_product("1", "Молоко", "89.90")  # optional fields left None


def test_ok_text_is_compact_and_equals_structured():
    res = ok(_product())
    text = res.content[0].text
    assert "\n" not in text and '": ' not in text and '", ' not in text
    assert json.loads(text) == res.structured_content


def test_ok_structured_content_validates_against_output_model():
    # exclude_none drops None fields; the model must still accept the result.
    res = ok(_product())
    TypeAdapter(Product).validate_python(res.structured_content)
    assert res.structured_content["price"]["current"]["amount"] == 89.9  # number, not "89.90"


def _page(n: int) -> ProductPage:
    items = [ProductSummary(id=str(i), name="Товар " * 20) for i in range(n)]
    meta = PageMeta(page=1, limit=n, total=n, pages=1, has_more=False)
    return ProductPage(meta=meta, items=items)


def test_ok_truncates_to_budget():
    full = ok(_page(50))
    budget = len(full.content[0].text.encode()) // 3
    res = ok(_page(50), max_bytes=budget)
    assert len(res.content[0].text.encode()) <= budget
    meta = res.structured_content["meta"]
    assert meta["truncated"] and meta["has_more"]
    assert 0 < len(res.structured_content["items"]) < 50
    TypeAdapter(ProductPage).validate_python(res.structured_content)


def test_ok_within_budget_is_untouched():
    res = ok(_page(2), max_bytes=100_000)
    assert res.structured_content["meta"]["truncated"] is False


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_fail_is_error_with_localized_message(locale):
    res = fail(NotFound(), Texts(locale))
    body = json.loads(res.content[0].text)
    assert res.is_error and res.structured_content is None
    assert body["error"]["code"] == "not_found" and body["error"]["retryable"] is False
    assert bool(CYRILLIC.search(body["error"]["message"])) == (locale == "ru")


def test_fail_override_and_details():
    texts = Texts("en", {"error.invalid_input": "Check the ids."})
    res = fail(InvalidInput(details={"missing": ["42"]}), texts)
    err = json.loads(res.content[0].text)["error"]
    assert err == {
        "code": "invalid_input",
        "message": "Check the ids.",
        "retryable": False,
        "details": {"missing": ["42"]},
    }


def test_fail_verbatim_message_and_retryable():
    err = json.loads(fail(Unavailable("feed timeout"), Texts("ru")).content[0].text)["error"]
    assert err["message"] == "feed timeout" and err["retryable"] is True
    assert ShopError(code="x").code == "x"


# --- i18n --------------------------------------------------------------------


def test_every_builtin_text_has_both_languages():
    texts = dict(i18n.builtin())
    assert {"error.not_found", "error.internal_error"} <= set(texts)
    for key, text in texts.items():
        assert CYRILLIC.search(text.ru), f"{key}: ru has no Cyrillic"
        assert not CYRILLIC.search(text.en), f"{key}: en contains Cyrillic"


def test_l10n_requires_both_languages():
    with pytest.raises(ValueError):
        L10n(ru="Текст", en="")


def test_texts_unknown_key():
    with pytest.raises(KeyError):
        Texts("ru").get("no.such.key")


# --- tool spec against the real SDK ------------------------------------------


async def _search(q: str, limit: int = 10) -> Annotated[CallToolResult, Product]:
    return ok(make_product("1", q))


SPEC = ToolSpec(
    suffix="products_search",
    requires=frozenset({Capability.SEARCH}),
    scopes=(),
    annotations=ToolAnnotations(read_only_hint=True),
    output_model=Product,
    fn=_search,
    title=L10n(ru="Поиск товаров", en="Product search"),
    description=L10n(ru="Ищет товары в каталоге.", en="Searches the catalog."),
    params={"q": L10n(ru="Запрос", en="Query")},
)


def test_spec_text_keys():
    assert [k for k, _ in SPEC.texts()] == [
        "tool.products_search.title",
        "tool.products_search.description",
        "tool.products_search.param.q",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_spec_registers_in_mcpserver_with_localized_schema(locale):
    texts = Texts(locale)
    docs = {n: texts.get(f"tool.{SPEC.suffix}.param.{n}", t) for n, t in SPEC.params.items()}
    server = MCPServer(name="t")
    server.add_tool(
        with_param_docs(SPEC.fn, docs),
        name=f"demo_{SPEC.suffix}",
        title=SPEC.title.get(locale),
        description=SPEC.description.get(locale),
        annotations=SPEC.annotations,
    )
    [tool] = await server.list_tools()
    assert tool.input_schema["properties"]["q"]["description"] == SPEC.params["q"].get(locale)
    assert tool.input_schema["required"] == ["q"]  # limit has a default: not required
    assert tool.output_schema["properties"]["id"]["type"] == "string"
    assert tool.annotations.read_only_hint is True
    res = await server.call_tool("demo_products_search", {"q": "молоко"})
    assert res.structured_content["name"] == "молоко" and not res.is_error

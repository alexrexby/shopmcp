"""Cart strategies and the cart_link_create tool (called directly: the SDK validation
that turns JSON into `LineIn` is covered in test_app)."""

from decimal import Decimal
from types import SimpleNamespace as LineIn  # the tool reads only .id and .quantity
from urllib.parse import parse_qs, urlsplit

import pytest
from fakes import FakeAdapter, cart_strategy, make_product
from test_tools_catalog import call
from test_tools_spec import cfg

from shopmcp.capabilities import Capability
from shopmcp.cart import strategies
from shopmcp.cart.refs import CartRef
from shopmcp.cart.strategies import MAX_URL_LENGTH, UrlTemplateStrategy
from shopmcp.config import ConfigError, UrlTemplateCart
from shopmcp.domain.models import CartLine
from shopmcp.errors import InvalidInput
from shopmcp.i18n import Texts

TEMPLATE = UrlTemplateCart(
    strategy="url_template",
    url_template="https://shop.example/cart/add?{items}",
    item_template="id[]={id}&qty[]={qty}",
)


def lines(*pairs: tuple[str, str]) -> list[CartLine]:
    return [CartLine(product_id=i, quantity=Decimal(q)) for i, q in pairs]


def refs(products: dict) -> dict[str, CartRef]:
    return {i: CartRef(product_id=i) for i in products}


@pytest.mark.anyio
async def test_url_template_builds_one_url():
    products = {p.id: p for p in [make_product("1", "Молоко"), make_product("a/b", "Сыр")]}
    res = await UrlTemplateStrategy(TEMPLATE, Texts("ru")).handoff(
        lines(("1", "2.000"), ("a/b", "0.5")), products, refs(products)
    )
    assert res.kind == "url_template"
    url = urlsplit(str(res.url))
    assert (url.netloc, url.path) == ("shop.example", "/cart/add")
    assert parse_qs(url.query) == {"id[]": ["1", "a/b"], "qty[]": ["2", "0.5"]}
    assert [link.name for link in res.links] == ["Молоко", "Сыр"]


@pytest.mark.anyio
@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_url_template_too_long_degrades_to_link_list(locale):
    ids = [f"{i:04d}" + "x" * 96 for i in range(30)]
    products = {i: make_product(i, f"Товар {i}") for i in ids}
    strategy = UrlTemplateStrategy(TEMPLATE, Texts(locale))
    assert len(strategy.url(lines(*[(i, "1") for i in ids]), products, refs(products))) > (
        MAX_URL_LENGTH
    )
    res = await strategy.handoff(lines(*[(i, "1") for i in ids]), products, refs(products))
    assert res.kind == "link_list" and res.url is None and len(res.links) == 30
    assert res.note and (res.note.isascii() == (locale == "en"))


@pytest.mark.anyio
async def test_link_list_has_product_links():
    products = {"1": make_product("1", "Молоко")}
    res = await strategies.LinkListStrategy().handoff(lines(("1", "3")), products, refs(products))
    assert res.url is None and str(res.links[0].url) == "https://shop.example/p/1"


@pytest.mark.anyio
async def test_share_link_asks_the_adapter():
    adapter = FakeAdapter()
    strategy = cart_strategy(cfg(cart={"strategy": "share_link"}), adapter)
    products = {"1": adapter.products["1"]}
    res = await strategy.handoff(lines(("1", "2")), products, refs(products))
    assert res.kind == "share_link" and str(res.url) == "https://shop.example/?cart=1:2"


def test_share_link_without_capability_is_config_error():
    adapter = FakeAdapter({Capability.SEARCH, Capability.DETAILS})
    with pytest.raises(ConfigError, match="share_link"):
        cart_strategy(cfg(cart={"strategy": "share_link"}), adapter)


@pytest.mark.anyio
async def test_tool_uses_configured_template():
    res = await call(FakeAdapter(), "cart_link_create", lines=[LineIn(id="1", quantity=2)])
    assert res["kind"] == "url_template"
    assert res["url"] == "https://demo.example/cart/add?id[]=1&qty[]=2"


@pytest.mark.anyio
async def test_tool_rejects_unknown_ids_with_their_list():
    with pytest.raises(InvalidInput) as e:
        await call(
            FakeAdapter(),
            "cart_link_create",
            lines=[LineIn(id="1", quantity=1), LineIn(id="__nope__", quantity=1)],
        )
    assert e.value.details == {"missing": ["__nope__"]}


@pytest.mark.anyio
async def test_tool_link_list_strategy():
    c = cfg(cart={"strategy": "link_list"})
    res = await call(FakeAdapter(), "cart_link_create", c, lines=[LineIn(id="2", quantity=1)])
    assert (
        res["kind"] == "link_list" and "url" not in res and res["links"][0]["name"] == "Творог 9%"
    )

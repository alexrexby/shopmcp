"""CLI, scaffold template and stub adapters (stage 1D)."""

import inspect
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from fakes import FakeAdapter, make_product

from shopmcp import cli, detect
from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterContext, ShopAdapter
from shopmcp.config import AdapterConfig, load
from shopmcp.conformance.runner import bound_socket
from shopmcp.engines import PRESETS, Engine

STUB_KINDS = ["bitrix", "insales", "http_json"]
FEED = "https://shop.example/feed.xml"
SITE = "https://shop.example"


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def offline_probe(monkeypatch) -> list[str]:
    """Scaffold without --engine probes the site: here it gets canned evidence, no network."""
    asked: list[str] = []
    evidence = detect.Evidence(site=SITE, home=detect.Fetch(url=SITE, error="offline test"))

    def fake(site: str) -> detect.Evidence:
        asked.append(site)
        return evidence

    monkeypatch.setattr(detect, "probe", fake)
    return asked


def answer(monkeypatch, home: detect.Fetch, **probes: detect.Fetch) -> None:
    ev = detect.Evidence(site=SITE, home=home, probes={Engine(k): v for k, v in probes.items()})
    monkeypatch.setattr(detect, "probe", lambda site: ev)


def scaffold(*extra: str) -> int:
    return cli.main(["scaffold", "my-shop", "--feed", FEED, "--site", SITE, *extra])


# --- scaffold -----------------------------------------------------------------


def test_scaffold_creates_loadable_config(workdir):
    assert scaffold() == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert (cfg.id, cfg.prefix, cfg.locale) == ("my-shop", "myshop", "ru")
    assert cfg.adapter.kind == "yml" and cfg.adapter.model_extra["feed"] == FEED
    assert str(cfg.site).rstrip("/") == SITE
    assert cfg.cart.strategy == "link_list"
    assert cfg.texts == {}
    assert cfg.conformance.sample_query == cli.SAMPLE_QUERY_PLACEHOLDER


def test_scaffold_locale_and_prefix(workdir):
    assert scaffold("--locale", "en", "--prefix", "shop") == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert (cfg.locale, cfg.prefix) == ("en", "shop")


def test_scaffold_template_has_texts_table_and_both_languages(workdir):
    assert scaffold() == 0
    text = (workdir / "stores/my-shop/store.toml").read_text("utf-8")
    assert "\n[texts]\n" in text and "\nlocale = " in text
    assert "Edit every line" in text and "Отредактируйте" in text
    assert "${VAR}" in text  # the escaped `$${VAR}` reached the file as a literal


def test_scaffold_does_not_overwrite(workdir, capsys, offline_probe):
    path = workdir / "stores/my-shop/store.toml"
    path.parent.mkdir(parents=True)
    path.write_text("keep me", "utf-8")
    assert scaffold() == 1
    assert path.read_text("utf-8") == "keep me"
    assert "already exists" in capsys.readouterr().err
    assert offline_probe == []  # review L3: refused before the site is probed


@pytest.mark.parametrize("store_id", ["../evil", "Bad", "a"])
def test_scaffold_rejects_bad_id_before_touching_disk(workdir, store_id, capsys):
    assert cli.main(["scaffold", store_id, "--feed", FEED, "--site", SITE]) == 1
    assert "id:" in capsys.readouterr().err
    assert not (workdir / "stores").exists()
    assert not (workdir.parent / "evil").exists()


def test_scaffold_escapes_values(workdir):
    feed = 'https://shop.example/feed.xml?a="b"\\c'
    assert cli.main(["scaffold", "my-shop", "--feed", feed, "--site", SITE]) == 0
    assert load(workdir / "stores/my-shop/store.toml").adapter.model_extra["feed"] == feed


def test_scaffold_bad_site_is_an_error(workdir, capsys):
    assert cli.main(["scaffold", "my-shop", "--feed", FEED, "--site", "not a url"]) == 1
    assert "site:" in capsys.readouterr().err


# --- scaffold with an engine (plan-engines.md §2.7) -------------------------------------------


@pytest.mark.parametrize("engine", list(Engine))
def test_scaffold_engine_preset_loads(workdir, engine, monkeypatch, offline_probe, capsys):
    monkeypatch.setenv("SHOPMCP_CART_SECRET", "s" * 32)
    assert scaffold("--engine", engine.value) == 0
    assert offline_probe == []  # --engine skips detection
    path = workdir / "stores/my-shop/store.toml"
    cfg = load(path)
    preset = PRESETS[engine]
    assert cfg.cart.strategy == preset.cart["strategy"]
    assert cfg.adapter.kind == (preset.adapter or {"kind": "feed"})["kind"]
    text = path.read_text("utf-8")
    assert not any(f"{{{slot}}}" in text for slot in ("host", "base", "cart_path", "feed"))
    assert preset.notes.en.split()[0] in text and preset.notes.ru.split()[0] in text
    out = capsys.readouterr().out
    beta = preset.cart["strategy"] == "form_post"
    assert ("BETA" in text and "БЕТА" in text and "BETA" in out) == beta
    if preset.adapter is None or "feed" in preset.adapter:
        assert cfg.adapter.model_extra["feed"] == FEED and "adapter.feed" not in out


def test_scaffold_without_feed_marks_it(workdir, monkeypatch, capsys):
    monkeypatch.setenv("SHOPMCP_CART_SECRET", "s" * 32)
    assert cli.main(["scaffold", "my-shop", "--site", SITE, "--engine", "insales"]) == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert cfg.adapter.model_extra["feed"] == cli.FEED_PLACEHOLDER
    assert cfg.cart.action.host == "shop.example"
    out = capsys.readouterr().out
    assert "adapter.feed" in out and "cart.secret" in out and "SHOPMCP_CART_SECRET" in out


def test_scaffold_secret_stays_an_env_reference(workdir):
    assert scaffold("--engine", "shop_script") == 0
    text = (workdir / "stores/my-shop/store.toml").read_text("utf-8")
    assert 'secret = "${SHOPMCP_CART_SECRET}"' in text and "0" * 32 not in text


def test_scaffold_feedless_adapter_says_feed_is_unused(workdir, capsys):
    assert scaffold("--engine", "woocommerce") == 0
    assert "--feed unused" in capsys.readouterr().out


def test_scaffold_detects_the_engine(workdir, monkeypatch, capsys):
    answer(
        monkeypatch, detect.Fetch(url=SITE + "/", status=200, headers=(("powered-by", "Shopify"),))
    )
    assert scaffold() == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert cfg.cart.strategy == "url_template"
    assert cfg.cart.url_template == "https://shop.example/cart/{items}?storefront=true"
    out = capsys.readouterr().out
    assert "Shopify (detected)" in out and "powered-by: Shopify header" in out


def test_scaffold_headless_bitrix_drops_add_to_cart_links(workdir, monkeypatch, capsys):
    home = detect.Fetch(
        url=SITE + "/",
        status=200,
        headers=(("x-powered-cms", "Bitrix Site Manager"),),
        body='<a href="/catalog/a/">',
    )
    answer(monkeypatch, home, bitrix=detect.Fetch(status=200, body="/_next/static/x.js"))
    assert scaffold() == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert cfg.cart.strategy == "link_list" and cfg.cart.line_template is None
    assert "Next.js" in capsys.readouterr().out


def test_scaffold_undetermined_writes_the_generic_config(workdir, monkeypatch, capsys):
    answer(monkeypatch, detect.Fetch(url=SITE + "/", status=403, body="blocked"))
    assert scaffold() == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert (cfg.adapter.kind, cfg.cart.strategy) == ("yml", "link_list")
    out = capsys.readouterr().out
    assert "not determined" in out and "antibot" in out and "--engine" in out


def test_scaffold_ambiguous_lists_candidates(workdir, monkeypatch, capsys):
    home = detect.Fetch(url=SITE + "/", status=200, headers=(("x-tilda-server", "1"),))
    answer(
        monkeypatch,
        home,
        woocommerce=detect.Fetch(
            status=200,
            headers=(("content-type", "application/json"), ("x-wp-total", "1")),
            body="[]",
        ),
    )
    assert scaffold() == 0
    out = capsys.readouterr().out
    assert "candidates: tilda, woocommerce" in out
    assert load(workdir / "stores/my-shop/store.toml").cart.strategy == "link_list"


def test_scaffold_rejects_bad_input_before_probing(workdir, offline_probe):
    assert cli.main(["scaffold", "Bad", "--site", SITE]) == 1
    assert cli.main(["scaffold", "my-shop", "--site", "not a url"]) == 1
    assert offline_probe == []


# --- check (stage 2) ------------------------------------------------------------


def test_check_needs_a_target(capsys):
    assert cli.main(["check"]) == 2
    assert "store.toml, --url" in capsys.readouterr().err


def test_check_of_a_remote_feed_needs_online(workdir, capsys):
    path = workdir / "store.toml"
    path.write_text(cli.render_store_toml("cli-test", feed=FEED, site=SITE), "utf-8")
    assert cli.main(["check", str(path)]) == 2
    assert "--online" in capsys.readouterr().err


# --- stub adapters ----------------------------------------------------------------

CTX = AdapterContext(data_dir=Path("."), http_timeout=5, store_id="t")
PROTOCOL_METHODS = [
    n for n, v in vars(ShopAdapter).items() if callable(v) and not n.startswith("_")
]


@pytest.mark.parametrize("kind", STUB_KINDS)
def test_stub_adapter_declares_nothing(kind):
    adapter = registry.create(AdapterConfig(kind=kind), CTX)
    assert isinstance(adapter, ShopAdapter)
    assert adapter.capabilities() == frozenset()
    for name in PROTOCOL_METHODS:
        got, want = getattr(adapter, name), getattr(ShopAdapter, name)
        assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(want)


# --- index and serve (call into 1A/1B through their contracts) ------------------------


def _config_with_kind(workdir: Path, kind: str) -> Path:
    path = workdir / "store.toml"
    text = cli.render_store_toml("cli-test", feed=FEED, site=SITE, prefix="pp")
    path.write_text(text.replace('kind = "yml"', f'kind = "{kind}"'), "utf-8")
    return path


def test_index_starts_and_closes_the_adapter(workdir, capsys):
    fake = FakeAdapter()
    registry.register("fakecli", lambda cfg, ctx: fake)
    assert cli.main(["index", str(_config_with_kind(workdir, "fakecli"))]) == 0
    assert fake.started and fake.closed
    assert json.loads(capsys.readouterr().out) == {"products": len(fake.products)}


@pytest.mark.parametrize("kind", STUB_KINDS)
def test_stub_store_serves_only_store_info(workdir, kind):
    import uvicorn

    from shopmcp.server.app import build_app

    sock = bound_socket()  # not a port number bound later: that races with other runs
    server = uvicorn.Server(
        uvicorn.Config(build_app(load(_config_with_kind(workdir, kind))), log_level="warning")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        deadline = time.monotonic() + 30
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as r:
            assert r.status == 200
        req = urllib.request.Request(
            f"{base}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            tools = {t["name"] for t in json.loads(r.read())["result"]["tools"]}
        assert tools == {"pp_store_info"}  # no DETAILS, so no cart that could never work
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_woocommerce_in_a_subdirectory_keeps_the_path_in_cart_links(workdir):
    # Review L2: WordPress at /shop/ has its cart at /shop/cart/, not at the root.
    args = ["scaffold", "my-shop", "--feed", FEED, "--engine", "woocommerce"]
    assert cli.main([*args, "--site", SITE + "/shop/"]) == 0
    text = (workdir / "stores/my-shop/store.toml").read_text("utf-8")
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert cfg.cart.line_template == (
        "https://shop.example/shop/cart/?add-to-cart={id}&quantity={qty}"
    )
    assert "https://shop.example/shop/checkout-link/?products={items}" in text  # alt_cart


def test_shopify_scaffold_writes_the_variant_rule_for_the_owner_to_check(workdir, capsys):
    # Review M4: the cart link needs the variant id, the app's g:id is shopify_US_<p>_<v>.
    assert scaffold("--engine", "shopify") == 0
    path = workdir / "stores/my-shop/store.toml"
    cfg = load(path)
    adapter = registry.create(
        cfg.adapter, AdapterContext(data_dir=workdir, http_timeout=5, store_id="my-shop")
    )
    product = make_product("shopify_US_8123_4456", "Mug")
    assert adapter.cart_ref(product) == {"product_id": "8123", "variant_id": "4456"}
    assert adapter.cart_ref(make_product("SKU-9", "Tee")) == {"product_id": "SKU-9"}
    out = capsys.readouterr().out
    assert "adapter.cart_ref" in out and "g:id" in out
    assert "\n[adapter.cart_ref]\npattern = " in path.read_text("utf-8")

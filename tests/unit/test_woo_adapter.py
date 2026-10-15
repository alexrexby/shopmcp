"""WooCommerce adapter: the Store API snapshot in the feed index, config, conformance."""

from pathlib import Path

import anyio
import pytest
from woo_shop import FAULTS, SITE, Response, WooShop

from shopmcp import cli
from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterContext, SearchQuery
from shopmcp.adapters.woocommerce import WooAdapter, create, store_api
from shopmcp.adapters.woocommerce.adapter import CAPABILITIES
from shopmcp.adapters.yml.adapter import FEED_SECONDS
from shopmcp.capabilities import Capability
from shopmcp.cart.refs import CartRef
from shopmcp.config import AdapterConfig, ConfigError, load
from shopmcp.conformance import runner
from shopmcp.conformance.runner import check_config
from shopmcp.errors import Unavailable

STORE_TOML = """
id = "woo-demo"
prefix = "woo"
brand = "Магазин посуды"
site = "https://shop.example"
locale = "ru"
instructions = "Посуда, текстиль и чай. Цены в рублях."

[adapter]
kind = "woocommerce"
site = "https://shop.example"
refresh_minutes = 0

[cart]
strategy = "link_list"
line_template = "https://shop.example/cart/?add-to-cart={id}&quantity={qty}"

[server]
public_url = "http://localhost:8000/mcp"
allowed_hosts = ["localhost:*", "127.0.0.1:*"]

[conformance]
sample_query = "футболка"
"""


@pytest.fixture
def shop(monkeypatch):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(store_api, "sleep", lambda seconds: None)
    return WooShop().install(monkeypatch)


def ctx(tmp_path: Path) -> AdapterContext:
    return AdapterContext(data_dir=tmp_path / "data", http_timeout=5, store_id="woo-demo")


@pytest.fixture
async def adapter(shop, tmp_path):
    a = WooAdapter(SITE, ctx(tmp_path), refresh_minutes=0)
    await a.start()
    yield a
    await a.close()


@pytest.mark.anyio
async def test_search_details_and_cart_keys(adapter):
    found, total, _ = await adapter.search(SearchQuery(q="футболка"))
    assert total == 2 and {p.id for p in found} == {"201", "202"}
    [bottle] = await adapter.get_products(["101"])
    assert bottle.vendor == "Аква & Ко"
    refs = await adapter.cart_refs(["201", "101"])
    # `{id}` in a cart template is the variation: what ?add-to-cart= takes.
    assert refs["201"] == CartRef(product_id="103", variant_id="201")
    assert refs["101"] == CartRef(product_id="101")


@pytest.mark.anyio
async def test_filters_categories_and_promotions(adapter):
    stocked, _, _ = await adapter.search(SearchQuery(in_stock=True, limit=50))
    assert {p.id for p in stocked} == {"101", "107", "201"}
    in_dishes, _, _ = await adapter.search(SearchQuery(category_id="10", limit=50))
    assert {p.id for p in in_dishes} == {"101"}
    roots = await adapter.categories(None)
    assert {c.id for c in roots} == {"10", "12", "13"}
    promoted, _ = await adapter.promotions(1, 10, "discount")
    assert [p.id for p in promoted] == ["101", "201"]


@pytest.mark.anyio
async def test_health_counts_the_snapshot(adapter):
    health = await adapter.health()
    assert health["products"] == 5 and health["stale"] is False
    assert health["feed_offers"] == 10 and health["skipped"] == 5
    assert adapter.capabilities() == CAPABILITIES
    assert Capability.BARCODE not in CAPABILITIES


@pytest.mark.anyio
async def test_first_build_failure_is_fatal_and_says_why(shop, tmp_path, caplog):
    shop.faults[("products", 1)] = FAULTS["500"]
    a = WooAdapter(SITE, ctx(tmp_path), refresh_minutes=0)
    with pytest.raises(Unavailable):
        await a.start()
    assert (await a.health())["last_error"].endswith(
        "/wp-json/wc/store/v1/products page 1: HTTP 500"
    )


@pytest.mark.anyio
async def test_failed_refresh_keeps_the_previous_snapshot(shop, tmp_path):
    a = WooAdapter(SITE, ctx(tmp_path), refresh_minutes=60)
    a._interval = 0.01
    await a.start()
    try:
        shop.faults[("categories", 1)] = FAULTS["html"]
        for _ in range(200):
            if a._failures:
                break
            await anyio.sleep(0.01)
        health = await a.health()
        assert health["failed_refreshes"] >= 1 and "text/html" in health["last_error"]
        assert [p.id for p in await a.get_products(["107"])] == ["107"]
    finally:
        await a.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("cut", "error"),
    [
        ("page 2", "products page 2: 0 items, X-WP-TotalPages is 4"),
        ("all", "the catalog is empty"),
    ],
)
async def test_a_cut_listing_keeps_the_previous_snapshot(shop, tmp_path, monkeypatch, cut, error):
    # Review H1: a short 200 page replaced the whole index, health green.
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    a = WooAdapter(SITE, ctx(tmp_path), refresh_minutes=60)
    a._interval = 0.01
    await a.start()
    try:
        if cut == "all":
            shop.listings["products"] = []
        else:
            shop.faults[("products", 2)] = lambda url: Response(b"[]")
        for _ in range(200):
            if a._failures:
                break
            await anyio.sleep(0.01)
        health = await a.health()
        assert health["failed_refreshes"] >= 1 and error in health["last_error"]
        assert health["products"] == 5
        assert {p.id for p in await a.get_products(["101", "107", "201"])} == {"101", "107", "201"}
    finally:
        await a.close()


def test_online_check_waits_for_a_whole_catalog_read():
    # Review L4: 600 s against a read budget of 1800 s.
    assert runner.ONLINE_START_TIMEOUT > FEED_SECONDS


# --- config -------------------------------------------------------------------


def make(tmp_path: Path, **extra) -> WooAdapter:
    return create(AdapterConfig(kind="woocommerce", **extra), ctx(tmp_path))


def test_config(tmp_path):
    a = make(
        tmp_path,
        site="https://shop.example/shop/",
        refresh_minutes=30,
        max_pages=5,
        max_minutes=90,
        max_feed_mb=10,
    )
    assert a.site == "https://shop.example/shop" and a.max_pages == 5
    assert a.refresh_minutes == 30
    assert a.max_seconds == 90 * 60 and a.max_bytes == 10 * 2**20
    default = make(tmp_path, site=SITE)
    assert default.max_seconds == FEED_SECONDS and default.max_bytes == 200 * 2**20


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({}, "adapter.site"),
        ({"site": "http://shop.example"}, "adapter.site"),
        ({"site": "https://shop.example/?key=1"}, "adapter.site"),
        ({"site": "https://user:pw@shop.example"}, "adapter.site"),
        ({"site": "shop.example"}, "adapter.site"),
        ({"site": SITE, "feed": "x.xml"}, "unknown keys"),
        ({"site": SITE, "max_pages": 0}, "max_pages"),
        ({"site": SITE, "max_minutes": 0}, "max_minutes"),
        ({"site": SITE, "max_minutes": True}, "max_minutes"),
        ({"site": SITE, "max_feed_mb": 0}, "max_feed_mb"),
        ({"site": SITE, "refresh_minutes": -1}, "refresh_minutes"),
        ({"site": SITE, "refresh_minutes": 60, "stale_minutes": 60}, "stale_minutes"),
    ],
)
def test_bad_config(tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        make(tmp_path, **extra)


def test_registry_finds_the_kind(tmp_path):
    a = registry.create(AdapterConfig(kind="woocommerce", site=SITE), ctx(tmp_path))
    assert isinstance(a, WooAdapter)


def test_scaffolded_config_builds_the_adapter(tmp_path, monkeypatch):
    # Before this adapter the scaffold for --engine woocommerce gave a config that
    # `shopmcp index` refused: "No module named shopmcp.adapters.woocommerce".
    monkeypatch.chdir(tmp_path)
    args = ["scaffold", "my-shop", "--engine", "woocommerce", "--site", SITE + "/shop/"]
    assert cli.main(args) == 0
    cfg = load(tmp_path / "stores/my-shop/store.toml")
    a = registry.create(cfg.adapter, ctx(tmp_path))
    assert isinstance(a, WooAdapter) and a.site == "https://shop.example/shop"


def test_index_command_builds_the_snapshot(shop, tmp_path, monkeypatch, capsys):
    (tmp_path / "store.toml").write_text(STORE_TOML, "utf-8")
    monkeypatch.setenv(cli.DATA_DIR_ENV, str(tmp_path / "data"))
    assert cli.main(["index", str(tmp_path / "store.toml")]) == 0
    assert '"products": 5' in capsys.readouterr().out


def test_conformance_on_the_recorded_shop(shop, tmp_path):
    path = tmp_path / "store.toml"
    path.write_text(STORE_TOML, "utf-8")
    results = check_config(load(path), tmp_path)
    assert {r.id: r.detail for r in results if r.status == "fail"} == {}
    passed = {r.id for r in results if r.status == "ok"}
    assert {f"C{i}" for i in range(1, 12)} <= passed

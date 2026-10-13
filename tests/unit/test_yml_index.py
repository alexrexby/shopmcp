"""YML adapter over the demo feed: search, filters, lookups and the index swap."""

import os
import shutil
import sqlite3
import threading
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest

from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterContext, SearchQuery, ShopAdapter
from shopmcp.adapters.yml import index
from shopmcp.adapters.yml.adapter import CAPABILITIES, YmlAdapter, create
from shopmcp.adapters.yml.feed import FeedStats, open_feed, parse
from shopmcp.capabilities import Capability
from shopmcp.config import AdapterConfig, ConfigError
from shopmcp.errors import Unavailable

ROOT = Path(__file__).resolve().parents[2]
FEED = ROOT / "stores/demo/feed.xml"
BROKEN_OFFERS = 1
ALL = SearchQuery(limit=100)


def ctx(tmp_path: Path) -> AdapterContext:
    return AdapterContext(data_dir=tmp_path / "data", http_timeout=5, store_id="demo")


@pytest.fixture
async def adapter(tmp_path):
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=0)
    await a.start()
    yield a
    await a.close()


async def ids(a: YmlAdapter, **kw) -> list[str]:
    products, _, _ = await a.search(SearchQuery(limit=100, **kw))
    return [p.id for p in products]


def raw_offers() -> list[ET.Element]:
    return list(ET.parse(FEED).iter("offer"))


# --- indexing -----------------------------------------------------------------


@pytest.mark.anyio
async def test_index_holds_every_valid_offer(adapter):
    health = await adapter.health()
    _, total, _ = await adapter.search(ALL)
    # Second sign: the raw feed text, minus the offers known to be broken.
    expected = FEED.read_text(encoding="utf-8").count("<offer ") - BROKEN_OFFERS
    assert health["products"] == total == expected == 39
    assert (health["feed_offers"], health["skipped"]) == (40, 1)
    assert health["feed_date"] == "2026-10-13T09:00:00+03:00"
    assert health["categories"] == 6 and health["last_error"] is None


def test_adapter_is_registered_and_declares_capabilities(tmp_path):
    a = registry.create(AdapterConfig(kind="yml", feed=str(FEED)), ctx(tmp_path))
    assert isinstance(a, YmlAdapter) and isinstance(a, ShopAdapter)
    assert a.capabilities() == CAPABILITIES
    assert Capability.PICKUP_POINTS not in CAPABILITIES and Capability.ORDERS not in CAPABILITIES


@pytest.mark.parametrize(
    "extra, match",
    [
        ({}, "adapter.feed"),
        ({"feed": "f.xml", "fead": "x"}, "unknown keys"),
        ({"feed": "f.xml", "refresh_minutes": -1}, "refresh_minutes"),
        ({"feed": "f.xml", "max_feed_mb": 0}, "max_feed_mb"),
    ],
)
def test_create_rejects_bad_config(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        create(AdapterConfig(kind="yml", **extra), ctx(tmp_path))


# --- search -------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_finds_word_forms(adapter):
    found = await ids(adapter, q="молока")
    names = {p.id: p.name for p in await adapter.get_products(found)}
    milk = {o.get("id") for o in raw_offers() if (o.findtext("name") or "").startswith("Молоко")}
    assert milk == {"1", "2", "3"} and milk <= set(found)
    assert all(names[i].startswith("Молоко") for i in found[:3])  # name weight ranks them first


@pytest.mark.anyio
async def test_search_tvorozhny_does_not_reach_tvorog(adapter):
    # Actual behavior, pinned: snowball stems творожный -> творожн and творог -> творог,
    # so the adjective finds only the "творожная" product, not "Творог 9%".
    assert await ids(adapter, q="творожный") == ["10"]
    assert {"8", "9"} <= set(await ids(adapter, q="творог"))


@pytest.mark.anyio
async def test_search_folds_yo_and_falls_back_to_any_word(adapter):
    assert set(await ids(adapter, q="мёд")) == set(await ids(adapter, q="мед")) == {"15", "27"}
    # No product has both words: AND finds nothing, the OR retry finds the milk.
    assert "1" in await ids(adapter, q="молоко хлеб")
    assert await ids(adapter, q="!!!") == []


@pytest.mark.anyio
async def test_in_stock_excludes_the_unavailable(adapter):
    unavailable = {o.get("id") for o in raw_offers() if o.get("available") == "false"}
    every, in_stock = set(await ids(adapter)), set(await ids(adapter, in_stock=True))
    assert unavailable == {"6", "13", "28"}
    assert every - in_stock == unavailable


@pytest.mark.anyio
async def test_category_filter_includes_subtree(adapter):
    roots = {c.id: c for c in await adapter.categories(None)}
    children = {c.id for c in await adapter.categories("1")}
    found = await ids(adapter, category_id="1")
    assert children == {"11", "12"}
    assert len(found) == roots["1"].product_count == 16
    assert "16" in found and "1" in found  # own product and a child's
    assert await ids(adapter, category_id="11", q="творог") == []


@pytest.mark.anyio
async def test_price_filter_sort_and_paging(adapter):
    products, _, _ = await adapter.search(
        SearchQuery(price_min=Decimal(50), price_max=Decimal(100), sort="price_asc", limit=100)
    )
    prices = [p.price.current.amount for p in products]
    assert prices == sorted(prices) and all(50 <= x <= 100 for x in prices)
    p1, total, facets = await adapter.search(SearchQuery(limit=10, page=1))
    p4, total4, _ = await adapter.search(SearchQuery(limit=10, page=4))
    assert total == total4 == 39 and len(p1) == 10 and len(p4) == 9
    assert not {p.id for p in p1} & {p.id for p in p4}
    assert 1 <= len(facets.categories) <= 10
    assert sum(f.count for f in facets.categories) == 39


@pytest.mark.anyio
async def test_promotions_are_exactly_old_price_above_price(adapter):
    expected = {
        o.get("id")
        for o in raw_offers()
        if o.findtext("oldprice") and Decimal(o.findtext("oldprice")) > Decimal(o.findtext("price"))
    }
    products, total = await adapter.promotions(1, 100, "discount")
    assert {p.id for p in products} == expected and total == len(expected) >= 5
    assert "12" not in expected  # oldprice == price in the feed: a negative control
    discounts = [p.price.discount_percent for p in products]
    assert discounts == sorted(discounts, reverse=True)


# --- lookups ------------------------------------------------------------------


@pytest.mark.anyio
async def test_lookups(adapter):
    assert (await adapter.by_barcode("4600000000017")).id == "1"
    assert await adapter.by_barcode("0000000000000") is None
    assert [p.id for p in await adapter.get_products(["25", "nope", "1", "25"])] == ["25", "1"]
    analogs = await adapter.analogs("8", 5)
    assert "8" not in {p.id for p in analogs} and "9" in {p.id for p in analogs}
    assert all(p.category_id == "12" for p in analogs)
    assert await adapter.analogs("nope", 5) == []


@pytest.mark.anyio
async def test_no_markup_reaches_the_core(adapter):
    every = await adapter.get_products([str(i) for i in range(1, 41)])
    dumped = " ".join(p.model_dump_json() for p in every)
    assert len(every) == 39
    assert "&nbsp;" not in dumped and "<" not in dumped and "\\u00a0" not in dumped


# --- rebuild and failure modes ------------------------------------------------


def build_from(feed: Path, path: Path) -> index.BuildStats:
    stats = FeedStats()
    with open_feed(str(feed), timeout=1, max_bytes=10**7) as f:
        return index.build(path, parse(f, stats), feed_date=lambda: stats.date)


def test_rebuild_under_an_open_reader(tmp_path):
    path = tmp_path / "demo.sqlite"
    build_from(FEED, path)
    reader = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    cursor = reader.execute("SELECT id FROM products ORDER BY position")
    first = cursor.fetchmany(5)
    errors: list[BaseException] = []

    def query_loop() -> None:
        try:
            for _ in range(50):
                assert index.Index(path).search(SearchQuery(q="молоко"))[1] > 0
        except BaseException as e:  # noqa: BLE001 - surfaced by the assert below
            errors.append(e)

    t = threading.Thread(target=query_loop)
    t.start()
    for _ in range(5):
        build_from(FEED, path)
    t.join()
    assert not errors
    assert len(first) + len(cursor.fetchall()) == 39  # the old file stays readable
    reader.close()
    assert index.Index(path).search(ALL)[1] == 39
    assert not path.with_name(path.name + ".tmp").exists()


@pytest.mark.anyio
async def test_failed_rebuild_keeps_serving_the_old_index(tmp_path):
    feed = tmp_path / "feed.xml"
    shutil.copy(FEED, feed)
    a = YmlAdapter(str(feed), ctx(tmp_path), refresh_minutes=0)
    await a.start()
    feed.write_text("<yml_catalog><shop>", encoding="utf-8")
    b = YmlAdapter(str(feed), ctx(tmp_path), refresh_minutes=0)
    await b.start()  # existing index survives a broken feed
    assert (await b.search(ALL))[1] == 39
    assert "invalid XML" in (await b.health())["last_error"]


@pytest.mark.anyio
async def test_first_start_without_feed_fails(tmp_path):
    a = YmlAdapter(str(tmp_path / "missing.xml"), ctx(tmp_path), refresh_minutes=0)
    with pytest.raises(Unavailable):
        await a.start()
    with pytest.raises(Unavailable):
        await a.search(ALL)


@pytest.mark.anyio
@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
async def test_unwritable_data_dir_falls_back_to_temp(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        a = YmlAdapter(str(FEED), AdapterContext(locked, 5, "demo"), refresh_minutes=0)
        await a.start()
        assert (await a.search(ALL))[1] == 39
        assert "not writable" in (await a.health())["warning"]
    finally:
        locked.chmod(0o700)


@pytest.mark.anyio
async def test_close_stops_the_refresher(tmp_path):
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=1)
    await a.start()
    task = a._refresher
    assert task is not None and not task.done()
    await a.close()
    assert task.cancelled() and a._refresher is None

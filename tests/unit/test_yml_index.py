"""YML adapter over the demo feed: search, filters, lookups and the index swap."""

import gzip
import http.client
import json
import os
import shutil
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from decimal import Decimal
from pathlib import Path

import anyio
import anyio.lowlevel
import pytest

from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterBase, AdapterContext, SearchQuery, ShopAdapter
from shopmcp.adapters.yml import index
from shopmcp.adapters.yml.adapter import CAPABILITIES, STALE_FAILURES, YmlAdapter, create
from shopmcp.adapters.yml.feed import FeedStats, open_feed, parse, redact
from shopmcp.adapters.yml.stem import variants
from shopmcp.capabilities import Capability
from shopmcp.cart.refs import CartRef
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
        ({"feed": "f.xml", "refresh_minutes": 60, "stale_minutes": 60}, "stale_minutes"),
        ({"feed": "f.xml", "refresh_minutes": 0, "stale_minutes": True}, "stale_minutes"),
        ({"feed": "f.xml", "format": "csv"}, "adapter.format"),
        ({"feed": "f.xml", "cart_ref": "x"}, "must be a table"),
        ({"feed": "f.xml", "cart_ref": {"patern": "x"}}, "unknown keys"),
        ({"feed": "f.xml", "cart_ref": {"pattern": "("}}, "cart_ref.pattern"),
        ({"feed": "f.xml", "cart_ref": {"pattern": r"(?P<id>\d+)"}}, "named groups"),
        (
            {"feed": "f.xml", "cart_ref": {"pattern": r"(?P<product_id>\d+)-(?P<sku>\d+)"}},
            "named groups",
        ),
        ({"feed": "f.xml", "cart_ref": {"options": {"Size": 1}}}, "cart_ref.options"),
        ({"feed": "f.xml", "cart_ref": {"options": ["size"]}}, "cart_ref.options"),
    ],
)
def test_create_rejects_bad_config(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        create(AdapterConfig(kind="yml", **extra), ctx(tmp_path))


def test_kind_feed_is_the_same_adapter_with_format_and_cart_ref(tmp_path):
    a = registry.create(
        AdapterConfig(
            kind="feed",
            feed=str(FEED),
            format="yml",
            cart_ref={"pattern": r"(?P<product_id>\d+)", "options": {"Fat": "Жирность"}},
        ),
        ctx(tmp_path),
    )
    assert isinstance(a, YmlAdapter) and a.format == "yml"
    assert a.cart_ref.pattern is not None and a.cart_ref.options == {"Fat": "Жирность"}
    with pytest.raises(ConfigError, match="kind 'feed'"):
        create(AdapterConfig(kind="feed"), ctx(tmp_path))


# --- cart refs ----------------------------------------------------------------

GROUPED = """<yml_catalog><shop><offers>
<offer id="10-1" group_id="10"><url>https://s.example/10-1</url><price>1</price><name>Футболка S</name>
<param name="Размер">S</param><param name="Цвет">белый</param></offer>
<offer id="20"><url>https://s.example/20</url><price>1</price><name>Кружка</name></offer>
<offer id="Кр-1"><url>https://s.example/k</url><price>1</price><name>Кириллица</name></offer>
</offers></shop></yml_catalog>"""


async def grouped(tmp_path: Path, **cart_ref) -> YmlAdapter:
    feed = tmp_path / "grouped.xml"
    feed.write_text(GROUPED, encoding="utf-8")
    extra = {"cart_ref": cart_ref} if cart_ref else {}
    a = create(
        AdapterConfig(kind="feed", feed=str(feed), refresh_minutes=0, **extra), ctx(tmp_path)
    )
    await a.start()
    return a


@pytest.mark.anyio
async def test_cart_refs_default_group_is_the_product(tmp_path):
    a = await grouped(tmp_path)
    try:
        assert await a.cart_refs(["10-1", "20", "nope"]) == {
            "10-1": CartRef(product_id="10", variant_id="10-1"),
            "20": CartRef(product_id="20"),
            "nope": CartRef(product_id="nope"),  # not indexed: the AdapterBase default
        }
        assert await a.cart_refs([]) == {}
    finally:
        await a.close()


@pytest.mark.anyio
async def test_cart_refs_pattern_and_options(tmp_path):
    a = await grouped(
        tmp_path,
        pattern=r"(?P<product_id>\d+)-(?P<variant_id>\d+)",
        options={"Size": "размер", "Color": "Цвет", "Fit": "Посадка"},
    )
    try:
        refs = await a.cart_refs(["10-1", "20"])
        assert refs["10-1"] == CartRef(
            product_id="10", variant_id="1", options={"Size": "S", "Color": "белый"}
        )
        # The pattern does not match "20": the group rule applies (no group: the id itself).
        assert refs["20"] == CartRef(product_id="20")
    finally:
        await a.close()


async def outcome(refs) -> object:
    try:
        return await refs
    except Exception as e:  # noqa: BLE001 - the type is what is compared
        return type(e)


@pytest.mark.anyio
async def test_cart_ref_is_validated_on_read_like_the_adapter_default(tmp_path):
    # Stage A deviation 4: the index keeps the raw fields and CartRef validates them on
    # read, so an id like "Кр-1" fares exactly as with AdapterBase.cart_refs, whatever the
    # id rule of cart/refs.py is now, and a changed rule needs no index rebuild.
    a = await grouped(tmp_path)
    try:
        ours = await outcome(a.cart_refs(["Кр-1"]))
        assert ours == await outcome(AdapterBase().cart_refs(["Кр-1"]))
        with sqlite3.connect(a._index_path()) as db:
            (raw,) = db.execute("SELECT cart_ref_json FROM products WHERE id = 'Кр-1'").fetchone()
        assert json.loads(raw) == {"product_id": "Кр-1"}
    finally:
        await a.close()


@pytest.mark.anyio
async def test_format_that_contradicts_the_feed_is_an_error(tmp_path):
    a = create(
        AdapterConfig(kind="feed", feed=str(FEED), format="gmc", refresh_minutes=0), ctx(tmp_path)
    )
    with pytest.raises(Unavailable):
        await a.start()
    assert "adapter.format says gmc" in a._last_error


@pytest.mark.anyio
async def test_unknown_feed_root_is_an_error(tmp_path):
    feed = tmp_path / "page.xml"
    feed.write_text("<html><body/></html>")
    a = create(AdapterConfig(kind="feed", feed=str(feed), refresh_minutes=0), ctx(tmp_path))
    with pytest.raises(Unavailable):
        await a.start()
    assert "unknown feed root" in a._last_error


# --- search -------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_finds_word_forms(adapter):
    found = await ids(adapter, q="молока")
    names = {p.id: p.name for p in await adapter.get_products(found)}
    milk = {o.get("id") for o in raw_offers() if (o.findtext("name") or "").startswith("Молоко")}
    assert milk == {"1", "2", "3"} and milk <= set(found)
    assert all(names[i].startswith("Молоко") for i in found[:3])  # name weight ranks them first


@pytest.mark.anyio
async def test_search_adjective_reaches_its_noun(adapter):
    # Snowball stems творожный -> творожн and творог -> творог; the query expansion
    # (творожн | творож | творог) bridges them. It used to find only "творожная" (id 10).
    assert {"8", "9", "10"} <= set(await ids(adapter, q="творожный"))
    assert {"1", "2", "3"} <= set(await ids(adapter, q="молочный"))  # молочн -> молок


@pytest.mark.anyio
async def test_search_expansion_keeps_noun_queries_exact(adapter):
    # Precision control: before the expansion "молоко" gave exactly these, in this order.
    assert await ids(adapter, q="молоко") == ["3", "2", "1", "6", "7", "4", "5"]


def test_variants_only_for_relative_adjectives():
    assert variants("творожн") == ["творожн", "творож", "творог", "твороз"]
    assert variants("орехов") == ["орехов", "орех"]
    assert variants("молок") == ["молок"]  # noun
    assert variants("лимон") == ["лимон"]  # -н after a vowel: a noun, not an adjective


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
        return index.build(path, parse(f, stats), feed_meta=lambda: {"feed_date": stats.date or ""})


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
    assert not list(tmp_path.glob("demo.sqlite.*tmp*"))  # every build removed its temp file


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


# --- background refresh: the loop body itself ---------------------------------------


def refreshing(feed: Path, tmp_path: Path) -> YmlAdapter:
    a = YmlAdapter(str(feed), ctx(tmp_path), refresh_minutes=1)
    a._interval = 0.01  # seconds: run the loop body now, not in a minute
    return a


async def until(probe, timeout: float = 5) -> None:
    with anyio.fail_after(timeout):
        while not await probe():
            await anyio.sleep(0.01)


@pytest.mark.anyio
async def test_refresh_picks_up_the_new_feed(tmp_path):
    feed = tmp_path / "feed.xml"
    shutil.copy(FEED, feed)
    a = refreshing(feed, tmp_path)
    await a.start()
    try:
        assert await ids(a, q="пингвин") == []
        text = FEED.read_text(encoding="utf-8")
        feed.write_text(text.replace("Молоко 3,2%", "Пингвин 3,2%", 1), encoding="utf-8")

        async def renamed() -> bool:
            return bool(await ids(a, q="пингвин"))

        await until(renamed)
    finally:
        await a.close()


@pytest.mark.anyio
async def test_broken_feed_keeps_the_old_index_and_the_loop(tmp_path):
    feed = tmp_path / "feed.xml"
    shutil.copy(FEED, feed)
    a = refreshing(feed, tmp_path)
    await a.start()
    try:
        feed.write_text("<yml_catalog><shop>", encoding="utf-8")

        async def failed() -> bool:
            return "invalid XML" in ((await a.health())["last_error"] or "")

        await until(failed)
        assert (await a.search(ALL))[1] == 39
        assert a._refresher is not None and not a._refresher.done()
    finally:
        await a.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        zlib.error("Error -3 while decompressing data: invalid code lengths set"),
        http.client.IncompleteRead(b"partial", 100),
        ValueError("unexpected"),
    ],
    ids=["zlib", "incomplete-read", "unexpected"],
)
async def test_refresh_survives_any_error_and_runs_again(tmp_path, error):
    # Review P1: anything but FeedError/OSError/sqlite3.Error used to end the task silently.
    a = refreshing(FEED, tmp_path)
    await a.start()
    seen: list[str | None] = []  # last_error as the next cycle starts

    def flaky(path: Path) -> None:
        seen.append(a._last_error)
        if len(seen) == 1:
            raise error

    a._rebuild = flaky  # type: ignore[method-assign]
    try:

        async def ran_again() -> bool:
            return len(seen) >= 2

        await until(ran_again)
        assert seen[0] is None and seen[1]  # the failure showed up before the next cycle
        assert a._refresher is not None and not a._refresher.done()
    finally:
        await a.close()


def corrupt_gz(tmp_path: Path) -> Path:
    data = bytearray(gzip.compress(FEED.read_bytes()))
    data[12] ^= 0xFF  # inside the first deflate block header: zlib.error, not OSError
    path = tmp_path / "feed.xml.gz"
    path.write_bytes(data)
    return path


@pytest.mark.anyio
async def test_start_on_a_corrupt_gz_serves_the_previous_index(tmp_path):
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=0)
    await a.start()
    b = YmlAdapter(str(corrupt_gz(tmp_path)), ctx(tmp_path), refresh_minutes=0)
    await b.start()
    assert (await b.search(ALL))[1] == 39
    assert "decompress" in (await b.health())["last_error"]


@pytest.mark.anyio
async def test_previous_index_keeps_its_feed_counts(tmp_path):
    # Review P7: "feed_offers: 0" next to 39 products read as "the feed is empty".
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=0)
    await a.start()
    b = YmlAdapter(str(tmp_path / "missing.xml"), ctx(tmp_path), refresh_minutes=0)
    await b.start()
    health = await b.health()
    assert (health["products"], health["feed_offers"], health["skipped"]) == (39, 40, 1)


@pytest.mark.parametrize("refresh", [0, 1])
@pytest.mark.anyio
async def test_index_of_another_schema_is_not_served_nor_fatal(tmp_path, refresh):
    # Review P8: after an upgrade an old index must not be served as "the previous one",
    # and a feed that is down at that moment must not keep the server down: it starts
    # with /healthz at 503 and builds the index in the background once the feed is back.
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=0)
    await a.start()
    with sqlite3.connect(a._index_path()) as db:
        db.execute("UPDATE meta SET value = '1' WHERE key = 'schema'")
    feed = tmp_path / "feed.xml"  # not there yet
    b = YmlAdapter(str(feed), ctx(tmp_path), refresh_minutes=refresh)
    b._retry = 0.01
    b._interval = 3600  # once built, no refresh during the test
    await b.start()
    try:
        health = await b.health()
        assert health["stale"] is True and "none" in health["index"]
        assert health["last_error"] and health["failed_refreshes"] >= 1
        with pytest.raises(Unavailable):
            await b.search(ALL)
        shutil.copy(FEED, feed)

        async def served() -> bool:
            return b.index is not None

        await until(served)
        assert (await b.search(ALL))[1] == 39
        health = await b.health()
        assert health["stale"] is False and "index" not in health
        assert health["failed_refreshes"] == 0 and health["last_error"] is None
    finally:
        await b.close()


def test_two_builders_on_one_data_dir(tmp_path):
    # Review P5: a fixed "<file>.tmp" let one builder unlink the file of the other.
    path = tmp_path / "demo.sqlite"
    errors: list[BaseException] = []

    def builder() -> None:
        try:
            for _ in range(5):
                build_from(FEED, path)
        except BaseException as e:  # noqa: BLE001 - surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=builder) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert index.Index(path).search(ALL)[1] == 39
    assert not list(tmp_path.glob("demo.sqlite.*tmp*"))


def test_sweep_removes_only_stale_temp_files(tmp_path):
    path = tmp_path / "demo.sqlite"
    old, fresh = tmp_path / "demo.sqlite.a1.tmp", tmp_path / "demo.sqlite.b2.tmp"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (0, 0))
    index.sweep(path, older_than=3600)
    assert not old.exists() and fresh.exists()


# --- health: a frozen index is not healthy (review P2) --------------------------------


def age_index(a: YmlAdapter, seconds: float) -> None:
    with sqlite3.connect(a._index_path()) as db:
        db.execute(
            "UPDATE meta SET value = ? WHERE key = 'built_at'", (str(time.time() - seconds),)
        )


@pytest.mark.anyio
async def test_old_index_is_stale(tmp_path):
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=10, stale_minutes=30)
    a._interval = 3600  # no refresh during the test
    await a.start()
    try:
        assert (await a.health())["stale"] is False
        age_index(a, 29 * 60)
        assert (await a.health())["stale"] is False
        age_index(a, 31 * 60)
        assert (await a.health())["stale"] is True
    finally:
        await a.close()


@pytest.mark.anyio
async def test_default_stale_threshold_is_three_refresh_intervals(tmp_path):
    assert YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=20).stale_seconds == 3600


@pytest.mark.anyio
async def test_no_refresh_is_never_stale(tmp_path):
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=0)
    await a.start()
    age_index(a, 10**7)
    assert (await a.health())["stale"] is False


@pytest.mark.anyio
async def test_failures_in_a_row_make_the_index_stale(tmp_path):
    feed = tmp_path / "feed.xml"
    shutil.copy(FEED, feed)
    a = refreshing(feed, tmp_path)
    await a.start()
    try:
        feed.write_text("<yml_catalog><shop>", encoding="utf-8")

        async def stale() -> bool:
            return (await a.health())["stale"]

        await until(stale)
        assert (await a.health())["failed_refreshes"] >= STALE_FAILURES
        shutil.copy(FEED, feed)

        async def recovered() -> bool:
            return not (await a.health())["stale"]

        await until(recovered)
        assert (await a.health())["failed_refreshes"] == 0
    finally:
        await a.close()


@pytest.mark.anyio
async def test_dead_refresher_is_stale_logged_and_does_not_fail_close(tmp_path, caplog):
    a = refreshing(FEED, tmp_path)

    async def dies(path: Path) -> None:
        raise RuntimeError("bug")

    a._refresh_loop = dies  # type: ignore[method-assign]
    await a.start()

    async def dead() -> bool:
        return a._refresher is not None and a._refresher.done()

    await until(dead)
    await anyio.lowlevel.checkpoint()  # done callbacks run on the next loop step
    health = await a.health()
    assert health["stale"] is True and "RuntimeError" in health["last_error"]
    assert "refresh task died" in caplog.text
    await a.close()


@pytest.mark.anyio
async def test_feed_secret_never_reaches_the_log(tmp_path, caplog):
    # Security Н1: `feed = "https://...?key=${FEED_KEY}"` must not print the key.
    feed = "https://user:pw@127.0.0.1:9/export/yml.php?key=S3CRET#frag"
    a = YmlAdapter(feed, ctx(tmp_path), refresh_minutes=0)
    with caplog.at_level("DEBUG"), pytest.raises(Unavailable):
        await a.start()
    assert "127.0.0.1:9/export/yml.php" in caplog.text
    assert "S3CRET" not in caplog.text and "pw" not in caplog.text


@pytest.mark.parametrize(
    "source, shown",
    [
        ("https://shop.ru/yml.php?key=x", "https://shop.ru/yml.php"),
        ("https://u:p@[::1]:8443/f.xml", "https://[::1]:8443/f.xml"),
        ("stores/demo/feed.xml", "stores/demo/feed.xml"),
    ],
)
def test_redact(source, shown):
    assert redact(source) == shown

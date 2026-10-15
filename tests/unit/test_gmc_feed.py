"""Google Merchant parser: mapping, variants and the cart key, YML parity, hostile XML."""

import gzip
import io
import re
import time
import tracemalloc
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shopmcp.adapters.base import AdapterContext, SearchQuery
from shopmcp.adapters.yml import gmc
from shopmcp.adapters.yml.adapter import YmlAdapter
from shopmcp.adapters.yml.feed import MAX_OFFER_NODES, FeedError, FeedStats, open_feed, parse, sniff
from shopmcp.cart.refs import CartRef
from shopmcp.domain.models import Category, Product

ROOT = Path(__file__).resolve().parents[2]
FEED = ROOT / "stores/demo/feed-gmc.xml"
BROKEN_ITEMS = 1  # X999 has no g:price (only a shipping price), on purpose
G = "{http://base.google.com/ns/1.0}"
MARKUP = re.compile(r"&[a-z]+;|&#\d+;|<[a-zA-Z/]|\xa0")
RSS = '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>{}</channel></rss>'


def read(raw: bytes) -> tuple[list[Category], list[Product], FeedStats]:
    stats = FeedStats()
    items = list(gmc.parse(io.BytesIO(raw), stats))
    return (
        [i for i in items if isinstance(i, Category)],
        [i for i in items if isinstance(i, Product)],
        stats,
    )


def item(body: str, gid: str = "1") -> str:
    return f"<item><g:id>{gid}</g:id>{body}</item>"


GOOD = "<title>Годный</title><link>https://s.example/2</link><g:price>5 RUB</g:price>"


def ctx(tmp_path: Path) -> AdapterContext:
    return AdapterContext(data_dir=tmp_path / "data", http_timeout=5, store_id="demo")


# --- demo feed ----------------------------------------------------------------


def test_demo_feed_item_count_matches_independent_counts():
    categories, products, stats = read(FEED.read_bytes())
    raw = FEED.read_text(encoding="utf-8")
    # Second sign, sharing no code with the parser: the raw text minus known broken items.
    assert len(products) == raw.count("<item>") - BROKEN_ITEMS == 16
    # Third: a whole-tree stdlib parse, items with an id, a link and their own g:price
    # (not the one inside g:shipping).
    complete = [
        i
        for i in ET.parse(FEED).iter("item")
        if i.findtext(G + "id") and i.findtext("link") and i.find(G + "price") is not None
    ]
    assert len(products) == len(complete)
    assert (stats.offers, stats.skipped) == (17, 1)
    assert "X999" not in {p.id for p in products}
    # 3 levels of product_type: every level is a category, each the parent of the next.
    by_id = {c.id: c for c in categories}
    assert len(by_id) == len(categories) == 17
    for c in categories:
        assert c.parent_id is None if len(c.path) == 1 else by_id[c.parent_id].path == c.path[:-1]


def test_demo_feed_mapping():
    categories, products, _ = read(FEED.read_bytes())
    by_id = {p.id: p for p in products}
    tee = by_id["T100-W-S"]
    assert (tee.name, str(tee.url)) == (
        "Футболка базовая, белая, S",
        "https://demo.example/p/t100-w-s",
    )
    assert (tee.price.current.amount, tee.price.current.currency) == (1290, "RUB")
    assert tee.price.old is None and tee.vendor == "Северная нить"
    assert tee.barcodes == ["4600000001007"]
    assert [str(u) for u in tee.pictures] == [
        "https://demo.example/img/t100-w-s.jpg",
        "https://demo.example/img/t100-w-s-2.jpg",
    ]
    assert [(x.name, x.value) for x in tee.params] == [("color", "белый"), ("size", "S")]
    assert tee.category_path == ["Одежда", "Мужская", "Футболки"]
    assert tee.category_id == gmc.category_id(tee.category_path)
    assert tee.description == "Хлопок 100%. Прямой крой."
    # sale_price is the current price, price becomes the old one.
    sale = by_id["T100-B-M"].price
    assert (sale.current.amount, sale.old.amount, sale.discount_percent) == (990, 1290, 23)
    # Every availability value of the spec.
    assert [by_id[i].available for i in ("SK400", "SC500", "D300-S", "H200-G-L")] == [
        True,
        False,
        False,
        False,
    ]
    assert by_id["SC500"].description == "Вязка «косы»."
    assert {c.id for c in categories} >= {tee.category_id}


def test_demo_feed_has_no_markup_in_any_string():
    categories, products, _ = read(FEED.read_bytes())
    texts = " ".join(m.model_dump_json() for m in [*categories, *products])
    assert texts and not MARKUP.search(texts)
    assert MARKUP.search(FEED.read_text(encoding="utf-8"))  # positive control


def test_variants_share_group_id():
    _, products, _ = read(FEED.read_bytes())
    groups: dict[str | None, list[str]] = {}
    for p in products:
        groups.setdefault(p.group_id, []).append(p.id)
    assert groups["T100"] == ["T100-W-S", "T100-W-M", "T100-W-L", "T100-B-M"]
    assert groups["K600"] == ["K600-40", "K600-42", "K600-44"]
    assert groups[None] == ["SK400", "SC500", "C700", "B800", "J900"]


@pytest.mark.anyio
async def test_adapter_reads_gmc_and_gives_cart_refs(tmp_path):
    a = YmlAdapter(str(FEED), ctx(tmp_path), refresh_minutes=0)  # format "auto"
    await a.start()
    try:
        _, total, _ = await a.search(SearchQuery(limit=100))
        health = await a.health()
        assert total == health["products"] == 16
        assert (health["feed_offers"], health["skipped"], health["categories"]) == (17, 1, 17)
        refs = await a.cart_refs(["T100-W-M", "SK400"])
        assert refs == {
            "T100-W-M": CartRef(product_id="T100", variant_id="T100-W-M"),
            "SK400": CartRef(product_id="SK400"),
        }
        assert refs["T100-W-M"].id == "T100-W-M"
        (variant,) = await a.get_products(["K600-42"])
        assert variant.group_id == "K600"
    finally:
        await a.close()


# --- YML parity ---------------------------------------------------------------

YML_TWIN = """<yml_catalog date="2026-10-15"><shop>
<categories><category id="1">Одежда</category><category id="2" parentId="1">Футболки</category>
</categories><offers>
<offer id="T1-M" group_id="T1" available="true"><url>https://s.example/t1-m</url>
<price>990</price><oldprice>1290</oldprice><currencyId>RUR</currencyId><categoryId>2</categoryId>
<picture>https://s.example/t1.jpg</picture><name>Футболка, M</name><vendor>Нить</vendor>
<barcode>4600000001014</barcode><description>&lt;p&gt;Хлопок&lt;/p&gt;</description>
<param name="color">белый</param><param name="size">M</param></offer>
<offer id="S2" available="false"><url>https://s.example/s2</url><price>590</price>
<currencyId>RUB</currencyId><categoryId>1</categoryId><name>Носки</name></offer>
</offers></shop></yml_catalog>"""

GMC_TWIN = RSS.format(
    item(
        "<g:item_group_id>T1</g:item_group_id><title>Футболка, M</title>"
        "<link>https://s.example/t1-m</link><g:price>1290.00 RUB</g:price>"
        "<g:sale_price>990.00 RUB</g:sale_price><g:availability>in_stock</g:availability>"
        "<g:product_type>Одежда &gt; Футболки</g:product_type>"
        "<g:image_link>https://s.example/t1.jpg</g:image_link><g:brand>Нить</g:brand>"
        "<g:gtin>4600000001014</g:gtin><description>&lt;p&gt;Хлопок&lt;/p&gt;</description>"
        "<g:color>белый</g:color><g:size>M</g:size>",
        "T1-M",
    )
    + item(
        "<title>Носки</title><link>https://s.example/s2</link><g:price>590 RUB</g:price>"
        "<g:availability>out_of_stock</g:availability><g:product_type>Одежда</g:product_type>",
        "S2",
    )
)


def test_gmc_and_yml_of_the_same_goods_give_the_same_products():
    stats = FeedStats()
    from_yml = [i for i in parse(io.BytesIO(YML_TWIN.encode()), stats) if isinstance(i, Product)]
    _, from_gmc, _ = read(GMC_TWIN.encode())
    assert len(from_yml) == len(from_gmc) == 2
    for y, g in zip(from_yml, from_gmc, strict=True):
        assert y.model_dump(exclude={"category_id"}) == g.model_dump(exclude={"category_id"})


# --- formats ------------------------------------------------------------------


def test_atom_with_g_namespace_is_read():
    raw = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:g="http://base.google.com/ns/1.0">'
        "<title>Магазин</title><entry><g:id>A1</g:id><title>Кружка</title>"
        '<link rel="alternate" href="https://s.example/a1"/><summary>Фарфор</summary>'
        "<g:price>450 RUB</g:price></entry></feed>"
    )
    _, (p,), _ = read(raw.encode())
    assert (p.id, p.name, str(p.url), p.description) == (
        "A1",
        "Кружка",
        "https://s.example/a1",
        "Фарфор",
    )


def test_shopify_atom_without_g_is_refused_not_read_as_empty():
    raw = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:s="http://jadedpixel.com/-/spec/shopify">'
        "<entry><id>https://s.example/products/1</id><title>Mug</title>"
        "<s:variant><s:price currency='USD'>10.00</s:price></s:variant></entry></feed>"
    )
    with pytest.raises(FeedError, match="not a Google Merchant feed"):
        read(raw.encode())


@pytest.mark.parametrize(
    "raw, fmt",
    [
        (FEED.read_bytes(), "gmc"),
        ((ROOT / "stores/demo/feed.xml").read_bytes(), "yml"),
        (b'<?xml version="1.0"?><!-- x --><feed xmlns="http://www.w3.org/2005/Atom"/>', "gmc"),
        (b"<html><body/></html>", None),
    ],
)
def test_sniff_tells_formats_apart_and_replays_the_bytes(raw, fmt):
    found, stream = sniff(io.BytesIO(raw))
    assert found == fmt
    assert stream.read() == raw


@pytest.mark.parametrize(
    "price, expected",
    [
        ("1299.00 RUB", ("RUB", "1299.00")),
        ("RUB 10", ("RUB", "10")),
        ("10,5 rur", ("RUB", "10.5")),
        ("10", None),  # no currency: not a Merchant price
        ("RUB 10 USD", None),
        ("-5 RUB", None),
        ("abc RUB", None),
    ],
)
def test_price(price, expected):
    got = gmc._price(price)
    assert (got and (got[0], str(got[1]))) == expected


@pytest.mark.parametrize(
    "extra, current, old",
    [
        ("<g:sale_price>1500 RUB</g:sale_price>", 1000, None),  # not below price
        ("<g:sale_price>500 USD</g:sale_price>", 1000, None),  # another currency
        ("<g:sale_price>broken</g:sale_price>", 1000, None),
    ],
)
def test_sale_price_that_is_no_discount_is_ignored(extra, current, old):
    body = f"<title>T</title><link>https://s.example/1</link><g:price>1000 RUB</g:price>{extra}"
    _, (p,), _ = read(RSS.format(item(body)).encode())
    assert p.price.current.amount == current and p.price.old is old


@pytest.mark.parametrize(
    "body",
    [
        "<link>https://s.example/1</link><g:price>10 RUB</g:price>",  # no title
        "<title>T</title><g:price>10 RUB</g:price>",  # no link
        "<title>T</title><link>https://s.example/1</link>",  # no price
        "<title>T</title><link>ftp://s.example/1</link><g:price>10 RUB</g:price>",
        "<title>T</title><link>https://s.example/1</link><g:price>10</g:price>",
    ],
)
def test_invalid_item_is_skipped_and_counted(body):
    _, products, stats = read(RSS.format(item(body) + item(GOOD, "2")).encode())
    assert [p.id for p in products] == ["2"]
    assert (stats.offers, stats.skipped) == (2, 1)


def test_item_without_g_id_is_skipped():
    raw = RSS.format(f"<item>{GOOD}</item>" + item(GOOD, "2"))
    _, products, stats = read(raw.encode())
    assert [p.id for p in products] == ["2"] and stats.skipped == 1


def test_g_fields_win_over_plain_ones():
    body = (
        "<title>RSS</title><g:title>G</g:title><link>https://s.example/rss</link>"
        "<g:link>https://s.example/g</g:link><g:price>1 RUB</g:price>"
    )
    _, (p,), _ = read(RSS.format(item(body)).encode())
    assert (p.name, str(p.url)) == ("G", "https://s.example/g")


# --- hostile input ------------------------------------------------------------

LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;">]>
<rss xmlns:g="http://base.google.com/ns/1.0"><channel><item><g:id>1</g:id>
<title>&lol2;</title></item></channel></rss>
"""


def test_entity_declarations_are_refused():
    with pytest.raises(FeedError, match="DTD"):
        read(LAUGHS)
    with pytest.raises(FeedError, match="DTD"):
        sniff(io.BytesIO(LAUGHS))


def test_invalid_xml_is_a_feed_error():
    with pytest.raises(FeedError, match="invalid XML"):
        read(b'<rss xmlns:g="http://base.google.com/ns/1.0"><channel><item>')


def test_gzip_bomb_is_stopped_after_decompression(tmp_path):
    bomb = tmp_path / "gmc.xml.gz"
    bomb.write_bytes(
        gzip.compress(RSS.format("<x/>" * 500_000 + item(GOOD)).encode()),
    )
    assert bomb.stat().st_size < 100_000
    with (
        pytest.raises(FeedError, match="unpacked"),
        open_feed(str(bomb), timeout=1, max_bytes=100_000) as f,
    ):
        list(gmc.parse(f, FeedStats()))


def peak_bytes(raw: bytes) -> tuple[int, FeedStats]:
    stats = FeedStats()
    tracemalloc.start()
    try:
        list(gmc.parse(io.BytesIO(raw), stats))
        return tracemalloc.get_traced_memory()[1], stats
    finally:
        tracemalloc.stop()


def test_memory_does_not_grow_with_junk_outside_items():
    # Security Н4, the same guarantee as the YML reader: junk in <channel> is dropped.
    def junk_feed(n: int) -> bytes:
        return RSS.format("<x/>" * n + item(GOOD)).encode()

    small, _ = peak_bytes(junk_feed(30_000))
    big, stats = peak_bytes(junk_feed(300_000))
    assert stats.offers == 1 and stats.skipped == 0
    assert big < 8 * 1024 * 1024 and big < small * 1.5, (small, big)


def test_item_with_too_many_children_is_skipped_quickly():
    children = "<g:color>c</g:color>" * (MAX_OFFER_NODES * 5)
    raw = RSS.format(item(GOOD + children, "1") + item(GOOD, "2")).encode()
    started = time.monotonic()
    peak, stats = peak_bytes(raw)
    assert time.monotonic() - started < 10
    assert (stats.offers, stats.skipped) == (2, 1)
    assert peak < 16 * 1024 * 1024, peak

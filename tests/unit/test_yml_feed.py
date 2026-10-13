"""YML parser: mapping, cleanup of source text, tolerance to bad offers, hostile XML."""

import gzip
import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shopmcp.adapters.yml.feed import FeedError, FeedStats, open_feed, parse
from shopmcp.adapters.yml.stem import normalize, stems
from shopmcp.domain.models import Category, Product

ROOT = Path(__file__).resolve().parents[2]
FEED = ROOT / "stores/demo/feed.xml"
BROKEN_OFFERS = 1  # offer 40 has no <price>, on purpose
MARKUP = re.compile(r"&[a-z]+;|&#\d+;|<[a-zA-Z/]|\xa0")


def read(raw: bytes) -> tuple[list[Category], list[Product], FeedStats]:
    stats = FeedStats()
    items = list(parse(io.BytesIO(raw), stats))
    return (
        [i for i in items if isinstance(i, Category)],
        [i for i in items if isinstance(i, Product)],
        stats,
    )


def offer(body: str, oid: str = "1") -> bytes:
    return (
        f'<yml_catalog date="2026-10-13"><shop><offers><offer id="{oid}">{body}</offer>'
        "</offers></shop></yml_catalog>"
    ).encode()


def strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


# --- demo feed ----------------------------------------------------------------


def test_demo_feed_product_count_matches_independent_counts():
    categories, products, stats = read(FEED.read_bytes())
    raw = FEED.read_text(encoding="utf-8")
    # Second sign, sharing no code with the parser: the raw text minus known broken offers.
    assert len(products) == raw.count("<offer ") - BROKEN_OFFERS == 39
    # Third: a whole-tree stdlib parse, offers that carry the required id, url and price.
    tree = ET.parse(FEED)
    complete = [
        o for o in tree.iter("offer") if o.get("id") and o.findtext("url") and o.findtext("price")
    ]
    assert len(products) == len(complete)
    assert (stats.offers, stats.skipped, stats.date) == (40, 1, "2026-10-13T09:00:00+03:00")
    assert len(categories) == 6
    assert "40" not in {p.id for p in products}


def test_demo_feed_mapping():
    categories, products, _ = read(FEED.read_bytes())
    by_id = {p.id: p for p in products}
    milk = by_id["1"]
    assert milk.price.current.currency == "RUB"  # RUR is an alias
    assert milk.barcodes == ["4600000000017"]
    assert milk.category_path == ["Молочные продукты", "Молоко и сливки"]
    assert next((x.name, x.value, x.unit) for x in milk.params) == ("Жирность", "3,2", "%")
    assert str(milk.pictures[0]) == "https://demo.example/img/1.jpg"
    assert by_id["14"].name == "Сыр Северная нива Гауда 45%, 250 г"  # vendor.model, no <name>
    assert by_id["11"].name == "Сырок глазированный ванильный, 40 г"
    assert by_id["25"].name == "Мука пшеничная высший сорт, 2 кг"
    assert by_id["2"].description == "Хранится до 6 месяцев без холодильника."
    assert by_id["2"].price.discount_percent == 20
    assert by_id["12"].price.old is None  # oldprice == price is not a discount
    assert by_id["6"].available is False
    paths = {c.id: c.path for c in categories}
    assert paths["21"] == ["Бакалея", "Крупы и макароны"]


def test_demo_feed_has_no_markup_in_any_string():
    categories, products, _ = read(FEED.read_bytes())
    texts = strings([m.model_dump(mode="json") for m in [*categories, *products]])
    assert texts and not [t for t in texts if MARKUP.search(t)]
    # Positive control: the raw feed does carry the markup that got cleaned.
    assert MARKUP.search(FEED.read_text(encoding="utf-8"))


# --- tolerance ----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "<price>10</price><name>Без ссылки</name>",
        "<url>https://s.example/1</url><name>Без цены</name>",
        "<url>https://s.example/1</url><price>abc</price><name>Цена не число</name>",
        "<url>https://s.example/1</url><price>-1</price><name>Цена меньше нуля</name>",
        "<url>ftp://s.example/1</url><price>10</price><name>Не http</name>",
        "<url>https://s.example/1</url><price>10</price>",
    ],
)
def test_invalid_offer_is_skipped_and_counted(body):
    good = "<url>https://s.example/2</url><price>5</price><name>Годный</name>"
    raw = offer(body).replace(
        b"</offers>", b'<offer id="2">' + good.encode() + b"</offer></offers>"
    )
    _, products, stats = read(raw)
    assert [p.id for p in products] == ["2"]
    assert (stats.offers, stats.skipped) == (2, 1)


def test_bad_picture_and_barcode_are_dropped_not_the_offer():
    _, products, _ = read(
        offer(
            "<url>https://s.example/1</url><price>1,5</price><name>Тест</name>"
            "<picture>not a url</picture><picture>https://s.example/i.jpg</picture>"
            "<barcode>12ab</barcode><barcode>46000000000178</barcode>"
        )
    )
    (p,) = products
    assert [str(u) for u in p.pictures] == ["https://s.example/i.jpg"]
    assert p.barcodes == ["46000000000178"]
    assert str(p.price.current.amount) == "1.5"
    assert p.available is None


# --- hostile input ------------------------------------------------------------

LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;">]>
<yml_catalog><shop><offers><offer id="1"><name>&lol2;</name></offer></offers></shop></yml_catalog>
"""


def test_entity_declarations_are_refused():
    with pytest.raises(FeedError, match="DTD"):
        read(LAUGHS)


def test_invalid_xml_is_a_feed_error():
    with pytest.raises(FeedError, match="invalid XML"):
        read(b"<yml_catalog><shop><offers><offer id='1'>")


def test_open_feed_reads_gzip_and_caps_size(tmp_path):
    gz = tmp_path / "feed.xml.gz"
    gz.write_bytes(gzip.compress(FEED.read_bytes()))
    with open_feed(str(gz), timeout=1, max_bytes=10**6) as f:
        assert len(list(parse(f, FeedStats()))) == 6 + 39
    with (
        pytest.raises(FeedError, match="exceeds"),
        open_feed(str(gz), timeout=1, max_bytes=1000) as f,
    ):
        list(parse(f, FeedStats()))
    # A small archive that unpacks past the cap is stopped after decompression.
    bomb = tmp_path / "bomb.xml.gz"
    bomb.write_bytes(gzip.compress(b"<a>" + b" " * 2_000_000 + b"</a>"))
    assert bomb.stat().st_size < 100_000
    with (
        pytest.raises(FeedError, match="unpacked"),
        open_feed(str(bomb), timeout=1, max_bytes=100_000) as f,
    ):
        list(parse(f, FeedStats()))


@pytest.mark.parametrize("source", ["file:///etc/passwd", "http://demo.example/feed.xml"])
def test_open_feed_accepts_only_paths_and_https(source):
    with pytest.raises(FeedError, match="scheme"), open_feed(source, timeout=1, max_bytes=1):
        pass


def test_open_feed_missing_file():
    with (
        pytest.raises(FeedError, match="cannot open"),
        open_feed("/nonexistent.xml", timeout=1, max_bytes=1),
    ):
        pass


# --- normalization ------------------------------------------------------------


def test_stems_join_word_forms_and_fold_yo():
    assert normalize("Мёд ЁЖ") == "мед еж"
    assert stems("Молоко молока") == ["молок", "молок"]
    assert stems("мёд") == stems("мед")
    # Snowball keeps the suffix alternation г/ж: творожный does not reduce to творог.
    assert stems("творог творожный") == ["творог", "творожн"]
    assert stems("!!!") == []

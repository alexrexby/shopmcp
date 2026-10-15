"""Streaming Google Merchant reader (`g:`, RSS 2.0 or Atom): feed bytes -> Category | Product.

The same pipeline and the same guarantees as the YML reader (`feed.py`): items are mapped
one by one and dropped from the tree, everything outside an item is dropped as soon as it
closes, an item over `MAX_OFFER_NODES` is skipped, a DTD with entities is refused.

GMC has no category tree: `g:product_type` ("A > B > C") becomes categories with
synthetic ids (`sha1(path)[:12]`), a parent per path prefix. Shopify's Atom (`s:`
namespace, no `g:`) is not a Merchant feed and is refused as a whole.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal
from typing import BinaryIO

from pydantic import ValidationError

from shopmcp.adapters.yml.feed import (
    _BARCODE,
    _CHUNK,
    _CURRENCY_ALIASES,
    _INTERNAL_DTD,
    _PROLOG,
    _READ_ERRORS,
    ATOM,
    MAX_OFFER_NODES,
    FeedError,
    FeedStats,
    _decimal,
    _prune,
    _url,
)
from shopmcp.domain.models import Category, Money, Param, Price, Product
from shopmcp.domain.text import clean_text

G_NS = "http://base.google.com/ns/1.0"
G = "{" + G_NS + "}"
_ITEMS = frozenset({"item", f"{ATOM}entry"})
# "1299.00 RUB"; the currency first is seen too.
_PRICE = re.compile(r"^\s*(?:([A-Za-z]{3})\s+)?([0-9][0-9.,]*)\s*([A-Za-z]{3})?\s*$")
# Not in stock right now: YML's available="false" means the same "made to order".
_AVAILABILITY = {
    "in_stock": True,
    "out_of_stock": False,
    "preorder": False,
    "backorder": False,
}
PARAMS = ("color", "size", "material", "pattern", "gender", "age_group")
_PATH_SEP = ">"


def parse(stream: BinaryIO, stats: FeedStats) -> Iterator[Category | Product]:
    """Yield valid items in feed order, each preceded by the categories of its
    `g:product_type` that were not seen yet. Memory is bound by the largest item."""
    parser = ET.XMLPullParser(events=("start", "end", "start-ns"))
    stack: list[ET.Element] = []  # open elements, root first
    item: ET.Element | None = None  # the item being read
    item_nodes = 0
    google = False  # the g: namespace was declared before the first item
    categories: set[str] = set()
    head = True
    try:
        while True:
            try:
                chunk = stream.read(_CHUNK)
            except _READ_ERRORS as e:
                raise FeedError(f"cannot read feed: {e}") from e
            if head:
                if _INTERNAL_DTD.search(chunk[:_PROLOG]):
                    raise FeedError("feed declares a DTD with entities, refused")
                head = False
            if chunk:
                parser.feed(chunk)
            else:
                parser.close()
            for event, payload in parser.read_events():
                if event == "start-ns":
                    google = google or payload[1] == G_NS  # type: ignore[index]
                    continue
                elem: ET.Element = payload  # type: ignore[assignment]
                if event == "start":
                    stack.append(elem)
                    if item is not None:
                        item_nodes += 1
                    elif elem.tag in _ITEMS:
                        if not google:
                            raise FeedError(
                                "not a Google Merchant feed: no g: namespace "
                                f"({G_NS}); Shopify's s: Atom is not supported"
                            )
                        item, item_nodes = elem, 0
                    continue
                stack.pop()
                if item is not None and elem is not item:
                    if item_nodes > MAX_OFFER_NODES:
                        elem.clear()
                    continue
                if elem is item:
                    stats.offers += 1
                    found = None if item_nodes > MAX_OFFER_NODES else _item(elem)
                    item = None
                    if found is None:
                        stats.skipped += 1
                    else:
                        new, product = found
                        for c in new:
                            if c.id not in categories:
                                categories.add(c.id)
                                yield c
                        yield product
                elem.clear()  # closed and read; _prune() detaches the empty shell
            _prune(stack, item if item_nodes <= MAX_OFFER_NODES else None)
            if not chunk:
                return
    except ET.ParseError as e:
        raise FeedError(f"invalid XML: {e}") from e


def category_id(path: list[str]) -> str:
    return hashlib.sha1(" > ".join(path).encode(), usedforsecurity=False).hexdigest()[:12]


def _categories(product_type: str | None) -> list[Category]:
    """`A > B > C` -> A, A > B, A > B > C, each the parent of the next."""
    names = [n for n in (clean_text(x) for x in (product_type or "").split(_PATH_SEP)) if n]
    out: list[Category] = []
    for depth in range(1, len(names) + 1):
        path = names[:depth]
        out.append(
            Category(
                id=category_id(path),
                name=path[-1],
                parent_id=out[-1].id if out else None,
                path=path,
            )
        )
    return out


def _price(value: str | None) -> tuple[str, Decimal] | None:
    """(currency, amount) of "1299.00 RUB"; None without an ISO code or a valid number."""
    m = _PRICE.match(value or "")
    if m is None or bool(m.group(1)) == bool(m.group(3)):
        return None
    amount = _decimal(m.group(2))
    if amount is None:
        return None
    currency = (m.group(1) or m.group(3)).upper()
    return _CURRENCY_ALIASES.get(currency, currency), amount


def _item(elem: ET.Element) -> tuple[list[Category], Product] | None:
    def raw(name: str, *plain: str) -> str | None:
        """`g:name` first, then the plain RSS / Atom element of the same meaning."""
        value = elem.findtext(G + name)
        for tag in plain:
            if value is not None and value.strip():
                break
            value = elem.findtext(tag)
        return value

    def text(name: str, *plain: str) -> str | None:
        return clean_text(raw(name, *plain))

    link = raw("link", "link")
    if not (link and link.strip()):
        atom = [x for x in elem.iterfind(f"{ATOM}link") if x.get("rel", "alternate") == "alternate"]
        link = atom[0].get("href") if atom else None
    gid = text("id")
    url = _url(link)
    name = text("title", "title", f"{ATOM}title")
    regular = _price(elem.findtext(G + "price"))
    if not (gid and url and name and regular):
        return None
    currency, amount = regular
    current: Decimal = amount
    old: Decimal | None = None  # sale_price below price: price becomes the old one
    sale = _price(elem.findtext(G + "sale_price"))
    if sale is not None and sale[0] == currency and sale[1] < amount:
        current, old = sale[1], amount
    discount = None
    if old is not None:
        discount = int(((old - current) * 100 / old).to_integral_value(ROUND_HALF_UP))

    availability = (text("availability") or "").lower().replace(" ", "_")
    barcodes: list[str] = []
    for b in elem.iterfind(G + "gtin"):
        code = clean_text(b.text)
        if code and _BARCODE.match(code) and code not in barcodes:
            barcodes.append(code)
    params = []
    for pname in PARAMS:
        value = text(pname)
        if value:
            params.append(Param(name=pname, value=value))
    pictures = [elem.findtext(G + "image_link")]
    pictures += [x.text for x in elem.iterfind(G + "additional_image_link")]
    categories = _categories(elem.findtext(G + "product_type"))

    try:
        product = Product(
            id=gid,
            name=name,
            url=url,
            price=Price(
                current=Money(amount=current, currency=currency),
                old=Money(amount=old, currency=currency) if old is not None else None,
                discount_percent=discount,
            ),
            available=_AVAILABILITY.get(availability),
            category_id=categories[-1].id if categories else None,
            category_path=categories[-1].path if categories else [],
            vendor=text("brand"),
            barcodes=barcodes,
            pictures=[u for u in map(_url, pictures) if u],
            description=text("description", "description", f"{ATOM}summary"),
            params=params,
            group_id=text("item_group_id"),
        )
    except ValidationError:
        return None
    return categories, product

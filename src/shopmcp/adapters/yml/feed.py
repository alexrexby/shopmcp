"""Streaming YML (Yandex Market Language) reader: feed bytes -> Category | Product.

Memory stays flat on feeds of hundreds of MB: offers are parsed one by one and
dropped from the tree right after mapping. An invalid offer is counted and skipped;
it never stops the whole feed.
"""

import gzip
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO

from pydantic import HttpUrl, ValidationError

from shopmcp.domain.models import Category, Money, Param, Price, Product
from shopmcp.domain.text import clean_text

_CHUNK = 64 * 1024
_PROLOG = 4096
# Stdlib expat never resolves external entities and caps entity expansion (>=2.4.1);
# a feed has no business declaring entities at all, so an internal DTD is refused.
_INTERNAL_DTD = re.compile(rb"<!DOCTYPE[^>]*\[|<!ENTITY")
_BARCODE = re.compile(r"^\d{8,14}$")
_CURRENCY_ALIASES = {"RUR": "RUB"}


class FeedError(Exception):
    """The feed cannot be read at all (transport, size, XML, DTD)."""


@dataclass
class FeedStats:
    offers: int = 0  # offers seen, valid or not
    skipped: int = 0  # offers dropped: no id/url/price or failed validation
    date: str | None = None  # yml_catalog@date


class _Capped:
    """Read-through wrapper that fails once more than `limit` bytes went through."""

    def __init__(self, raw: BinaryIO, limit: int, what: str) -> None:
        self._raw, self._limit, self._what, self._seen = raw, limit, what, 0

    def read(self, n: int = -1) -> bytes:
        data = self._raw.read(n)
        self._seen += len(data)
        if self._seen > self._limit:
            raise FeedError(f"{self._what} exceeds {self._limit} bytes")
        return data


@contextmanager
def open_feed(source: str, *, timeout: float, max_bytes: int) -> Iterator[BinaryIO]:
    """Open a local path or an https URL; `.gz` is decompressed, both sizes are capped."""
    scheme = urllib.parse.urlsplit(source).scheme.lower()
    gz = urllib.parse.urlsplit(source).path.endswith(".gz")
    with ExitStack() as stack:
        try:
            if scheme == "https":
                req = urllib.request.Request(source, headers={"User-Agent": "shopmcp-feed/1"})
                raw = stack.enter_context(urllib.request.urlopen(req, timeout=timeout))
                gz = gz or raw.headers.get_content_type() in (
                    "application/gzip",
                    "application/x-gzip",
                )
            elif scheme == "":
                raw = stack.enter_context(Path(source).open("rb"))
            else:
                raise FeedError(f"unsupported feed source scheme {scheme!r}: use a path or https")
        except OSError as e:
            raise FeedError(f"cannot open feed: {e}") from e
        stream: BinaryIO = _Capped(raw, max_bytes, "feed")  # type: ignore[assignment]
        if gz:
            # The cap applies after decompression too: a small archive can unpack huge.
            unpacked = gzip.GzipFile(fileobj=stream)  # type: ignore[arg-type]
            stream = _Capped(unpacked, max_bytes, "unpacked feed")  # type: ignore[assignment]
        yield stream


def parse(stream: BinaryIO, stats: FeedStats) -> Iterator[Category | Product]:
    """Yield categories (once `<categories>` closes) and valid offers in feed order."""
    parser = ET.XMLPullParser(events=("start", "end"))
    offers: ET.Element | None = None
    raw_categories: dict[str, tuple[str, str | None]] = {}  # id -> (name, parent_id)
    paths: dict[str, list[str]] = {}
    head = True
    try:
        while True:
            try:
                chunk = stream.read(_CHUNK)
            except (OSError, EOFError) as e:  # gzip errors are OSError/EOFError
                raise FeedError(f"cannot read feed: {e}") from e
            if head:
                if _INTERNAL_DTD.search(chunk[:_PROLOG]):
                    raise FeedError("feed declares a DTD with entities, refused")
                head = False
            if chunk:
                parser.feed(chunk)
            else:
                parser.close()
            for event, elem in parser.read_events():
                tag = elem.tag
                if event == "start":
                    if tag == "offers":
                        offers = elem
                    elif tag == "yml_catalog":
                        stats.date = elem.get("date")
                elif tag == "category":
                    cid = clean_text(elem.get("id"))
                    name = clean_text(elem.text)
                    if cid and name:
                        raw_categories[cid] = (name, clean_text(elem.get("parentId")))
                elif tag == "categories":
                    paths = _paths(raw_categories)
                    for cid, (name, parent) in raw_categories.items():
                        yield Category(id=cid, name=name, parent_id=parent, path=paths[cid])
                    elem.clear()
                elif tag == "offer":
                    stats.offers += 1
                    product = _offer(elem, paths)
                    if product is None:
                        stats.skipped += 1
                    else:
                        yield product
                    # Drop finished offers from the tree; the parent keeps no children.
                    (offers if offers is not None else elem).clear()
            if not chunk:
                return
    except ET.ParseError as e:
        raise FeedError(f"invalid XML: {e}") from e


def _paths(raw: dict[str, tuple[str, str | None]]) -> dict[str, list[str]]:
    """Category id -> names from the root down; cycles and dangling parents are cut."""
    paths: dict[str, list[str]] = {}
    for cid in raw:
        chain: list[str] = []
        seen: set[str] = set()
        cur: str | None = cid
        while cur is not None and cur in raw and cur not in seen:
            seen.add(cur)
            name, cur = raw[cur]
            chain.append(name)
        paths[cid] = chain[::-1]
    return paths


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(value.strip().replace(",", "."))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


def _url(value: str | None) -> HttpUrl | None:
    if not value or not value.strip():
        return None
    try:
        return HttpUrl(value.strip())
    except ValidationError:
        return None


def _offer(elem: ET.Element, paths: dict[str, list[str]]) -> Product | None:
    def text(tag: str) -> str | None:
        return clean_text(elem.findtext(tag))

    oid = clean_text(elem.get("id"))
    url = _url(elem.findtext("url"))
    current = _decimal(elem.findtext("price"))
    # vendor.model offers may omit <name>: the name is typePrefix + vendor + model.
    name = text("name") or clean_text(
        " ".join(filter(None, (text("typePrefix"), text("vendor"), text("model"))))
    )
    if not (oid and url and current is not None and name):
        return None

    currency = (text("currencyId") or "RUB").upper()
    currency = _CURRENCY_ALIASES.get(currency, currency)
    old = _decimal(elem.findtext("oldprice"))
    discount = None
    if old is not None and old > current:
        discount = int(((old - current) * 100 / old).to_integral_value(ROUND_HALF_UP))
    else:
        old = None  # an "old" price not above the current one is not a discount

    available = {"true": True, "false": False}.get((elem.get("available") or "").lower())
    barcodes: list[str] = []
    for b in elem.iterfind("barcode"):
        code = clean_text(b.text)
        if code and _BARCODE.match(code) and code not in barcodes:
            barcodes.append(code)
    params = []
    for p in elem.iterfind("param"):
        pname, value = clean_text(p.get("name")), clean_text(p.text)
        if pname and value:
            params.append(Param(name=pname, value=value, unit=clean_text(p.get("unit"))))
    category_id = text("categoryId")

    try:
        return Product(
            id=oid,
            name=name,
            url=url,
            price=Price(
                current=Money(amount=current, currency=currency),
                old=Money(amount=old, currency=currency) if old is not None else None,
                discount_percent=discount,
            ),
            available=available,
            category_id=category_id,
            category_path=paths.get(category_id, []) if category_id else [],
            vendor=text("vendor"),
            barcodes=barcodes,
            pictures=[u for u in map(_url, (p.text for p in elem.iterfind("picture"))) if u],
            description=text("description"),
            params=params,
        )
    except ValidationError:
        return None

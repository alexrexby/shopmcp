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
MAX_OFFER_NODES = 10_000  # elements inside one <offer>; more is not a product, it is an attack
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


def redact(source: str) -> str:
    """The feed source fit for logs: a URL loses userinfo, query and fragment, where
    a `${ENV}` secret usually sits (`...yml.php?key=...`); a path stays as it is."""
    parts = urllib.parse.urlsplit(source)
    if not parts.scheme:
        return source
    host = parts.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


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
    """Yield categories (once `<categories>` closes) and valid offers in feed order.

    Every element is dropped from the tree as soon as it closes, unless it is inside an
    `<offer>` still being read: memory is bound by the largest offer, not by the feed.
    An offer with more than `MAX_OFFER_NODES` descendants is skipped as invalid.
    """
    parser = ET.XMLPullParser(events=("start", "end"))
    stack: list[ET.Element] = []  # open elements, root first
    offer: ET.Element | None = None  # the offer being read
    offer_nodes = 0
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
                    stack.append(elem)
                    if offer is not None:
                        offer_nodes += 1
                    elif tag == "offer":
                        offer, offer_nodes = elem, 0
                    elif tag == "yml_catalog":
                        stats.date = elem.get("date")
                    continue
                stack.pop()
                if offer is not None and elem is not offer:
                    # Kept for _offer() unless the offer is already too big to be valid.
                    if offer_nodes > MAX_OFFER_NODES:
                        elem.clear()
                    continue
                if tag == "category":
                    cid = clean_text(elem.get("id"))
                    name = clean_text(elem.text)
                    if cid and name:
                        raw_categories[cid] = (name, clean_text(elem.get("parentId")))
                elif tag == "categories":
                    paths = _paths(raw_categories)
                    for cid, (name, parent_id) in raw_categories.items():
                        yield Category(id=cid, name=name, parent_id=parent_id, path=paths[cid])
                elif tag == "offer":
                    stats.offers += 1
                    product = None if offer_nodes > MAX_OFFER_NODES else _offer(elem, paths)
                    offer = None
                    if product is None:
                        stats.skipped += 1
                    else:
                        yield product
                elem.clear()  # closed and read; _prune() detaches the empty shell
            _prune(stack, offer if offer_nodes <= MAX_OFFER_NODES else None)
            if not chunk:
                return
    except ET.ParseError as e:
        raise FeedError(f"invalid XML: {e}") from e


def _prune(stack: list[ET.Element], keep: ET.Element | None) -> None:
    """Detach every closed child of the open elements, down to `keep` (an offer still
    being read keeps its subtree for _offer). Events arrive a chunk at a time, so a parent
    may hold many closed children by now, but only its last child can still be open."""
    for depth, elem in enumerate(stack):
        if elem is keep:
            return
        open_child = stack[depth + 1] if depth + 1 < len(stack) else None
        if open_child is not None and len(elem) and elem[-1] is open_child:
            del elem[:-1]
        else:
            del elem[:]


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

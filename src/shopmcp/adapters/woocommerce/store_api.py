"""WooCommerce Store API reader: the keyless public catalog -> Category | Product.

Checked live with GET on a WooCommerce shop, 2026-10-15: `?type=variation` lists every
variation with its `parent`, `variation` ("Graphic: Blue"), own permalink, prices, stock
and images, but no categories (they stay on the parent); the default listing has no
variations, and `include[]=<variation id>` does not return one. `per_page` is 1..100
(101 is a 400); a page past the end is 200 `[]`; product listings carry `X-WP-Total` and
`X-WP-TotalPages` (so does `/products/categories` once `per_page` is given), while their
`Link: rel=next` writes `&` as `#038;`, so pages are counted, never followed. Prices are strings in minor units: `"1899"` with
`currency_minor_unit: 2` is 18.99, `"115"` with 0 is 115.

The catalog becomes what a feed gives: one Product per purchasable unit. A product with
variations is not one, its variations are: named after it, in its categories, with
`group_id` = its id. Other products are taken as they are, except `grouped` (a set of
links), `external` (sold elsewhere, no cart here) and a `variable` one left without
variations.

Only GET, no key, no cookie. Memory: one page (capped in bytes) plus a short summary of
each variable product, kept until its variations are read.
"""

import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from email.message import Message
from typing import Any

from pydantic import ValidationError

from shopmcp.adapters.yml.feed import (
    FeedError,
    FeedStats,
    _paths,
    _url,
    redact,
)
from shopmcp.domain.models import Category, Money, Param, Price, Product
from shopmcp.domain.text import clean_text

API_PATH = "/wp-json/wc/store/v1"
PER_PAGE = 100  # the Store API maximum
MAX_PAGE_BYTES = 16 * 1024 * 1024  # one JSON page; 100 products are well under 2 MB
REQUEST_INTERVAL = 1.0  # seconds between two requests: a catalog read must not load the shop
USER_AGENT = "shopmcp-woo/1"
MAX_MINOR_UNIT = 6
MAX_DESCRIPTION = 3000  # characters; a variable product's text is kept until its variations
MAX_PICTURES = 10
_CHUNK = 64 * 1024
_MINOR = re.compile(r"^\d{1,18}$")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
# A set of links, a product sold elsewhere, and a variable product with no variation
# on sale (seen live: price 0, not purchasable) are not purchasable units.
_SKIPPED_TYPES = frozenset({"grouped", "external", "variable"})


@dataclass
class _Parent:
    """What variations take from their variable product."""

    name: str
    category_ids: list[str]
    description: str | None
    vendor: str | None
    pictures: list[str]
    params: list[Param]  # attributes that are not variation axes
    terms: dict[str, dict[str, str]]  # casefolded attribute name -> term slug -> term name
    axes: dict[int, list[tuple[str, str]]] = field(default_factory=dict)  # variation id


class _Reader:
    def __init__(self, site: str, *, timeout: float, max_seconds: float) -> None:
        self.api = site.rstrip("/") + API_PATH
        self.label = f"store api {redact(self.api)}"  # for errors and logs: no query
        self.timeout = timeout
        self.max_seconds = max_seconds
        self.deadline = time.monotonic() + max_seconds
        self.requests = 0

    def get(self, path: str, query: dict[str, Any]) -> tuple[Any, Message]:
        """One GET: the decoded JSON and the response headers. Paced by REQUEST_INTERVAL."""
        if self.requests:
            time.sleep(REQUEST_INTERVAL)
        self.requests += 1
        url = f"{self.api}{path}?{urllib.parse.urlencode(query)}"
        what = f"{self.label}{path}" + (f" page {query['page']}" if "page" in query else "")
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get_content_type()
                headers = resp.headers
                body = self._read(resp, what) if ctype == "application/json" else b""
        except urllib.error.HTTPError as e:
            e.close()
            raise FeedError(f"{what}: HTTP {e.code}") from e
        except (http.client.HTTPException, EOFError) as e:
            # Class only: InvalidURL quotes the URL. IncompleteRead: the body was cut.
            raise FeedError(f"{what}: {type(e).__name__}") from e
        except OSError as e:
            raise FeedError(f"{what}: {e}") from e
        if ctype != "application/json":
            # A WAF or a login wall answers 200 with an HTML page.
            raise FeedError(f"{what}: expected application/json, got {ctype}")
        try:
            return json.loads(body), headers
        except ValueError as e:
            raise FeedError(f"{what}: invalid JSON") from e

    def _read(self, resp: Any, what: str) -> bytes:
        """The body, capped in bytes and bounded by the deadline of the whole read: the
        socket timeout is per operation, a host trickling bytes would hold it for hours."""
        parts, size = [], 0
        while True:
            if time.monotonic() > self.deadline:
                raise FeedError(f"catalog download exceeded {self.max_seconds:g} s")
            chunk = resp.read(_CHUNK)
            if not chunk:
                return b"".join(parts)
            size += len(chunk)
            if size > MAX_PAGE_BYTES:
                raise FeedError(f"{what}: exceeds {MAX_PAGE_BYTES} bytes")
            parts.append(chunk)

    def pages(self, path: str, query: dict[str, Any], max_pages: int) -> Iterator[list[Any]]:
        """Non-empty pages in id order until a short or empty page, or `X-WP-TotalPages`."""
        total: int | None = None
        page = 1
        while True:
            data, headers = self.get(path, {**query, "per_page": PER_PAGE, "page": page})
            if not isinstance(data, list):
                raise FeedError(f"{self.label}{path} page {page}: expected a JSON list")
            if total is None:
                total = _int(headers.get("X-WP-TotalPages"))
                if total is not None and total > max_pages:
                    raise FeedError(
                        f"{self.label}{path}: {total} pages of {PER_PAGE},"
                        f" adapter.max_pages is {max_pages}"
                    )
            if data:
                yield data
            if not data or len(data) < PER_PAGE or (total is not None and page >= total):
                return
            if page >= max_pages:
                raise FeedError(f"{self.label}{path}: more than adapter.max_pages = {max_pages}")
            page += 1


def read(
    site: str,
    stats: FeedStats,
    *,
    timeout: float,
    max_pages: int,
    max_seconds: float,
) -> Iterator[Category | Product]:
    """Yield the categories, then every purchasable product of the shop at `site`."""
    reader = _Reader(site, timeout=timeout, max_seconds=max_seconds)
    paths = yield from _categories(reader, max_pages)
    parents: dict[int, _Parent] = {}
    order = {"orderby": "id", "order": "asc"}  # stable pages while the shop is edited
    for page in reader.pages("/products", order, max_pages):
        for raw in page:
            if not isinstance(raw, dict):
                stats.offers += 1
                stats.skipped += 1
                continue
            if raw.get("variations"):
                parent = _parent(raw)
                pid = raw.get("id")
                if parent is not None and isinstance(pid, int):
                    parents[pid] = parent
                continue
            stats.offers += 1
            product = None if raw.get("type") in _SKIPPED_TYPES else _product(raw, None, paths)
            if product is None:
                stats.skipped += 1
            else:
                yield product
    if not parents:
        return
    for page in reader.pages("/products", {"type": "variation", **order}, max_pages):
        for raw in page:
            stats.offers += 1
            parent = parents.get(raw.get("parent")) if isinstance(raw, dict) else None
            product = _product(raw, parent, paths) if parent is not None else None
            if product is None:
                stats.skipped += 1  # invalid, or its product is hidden from the catalog
            else:
                yield product


def _categories(reader: _Reader, max_pages: int) -> Iterator[Category]:
    """Yield the categories; return id -> names from the root down."""
    raw: dict[str, tuple[str, str | None]] = {}
    for page in reader.pages("/products/categories", {}, max_pages):
        for c in page:
            if not isinstance(c, dict) or not _positive(c.get("id")):
                continue
            name = clean_text(_str(c.get("name")))
            if name:
                parent = c.get("parent")
                raw[str(c["id"])] = (name, str(parent) if _positive(parent) else None)
    paths = _paths(raw)
    for cid, (name, parent_id) in raw.items():
        yield Category(id=cid, name=name, parent_id=parent_id, path=paths[cid])
    return paths


def _parent(raw: dict[str, Any]) -> _Parent | None:
    name = clean_text(_str(raw.get("name")))
    if not name:
        return None
    terms: dict[str, dict[str, str]] = {}
    params = []
    for a in _list(raw.get("attributes")):
        if not isinstance(a, dict) or not (aname := clean_text(_str(a.get("name")))):
            continue
        slugs = {
            _str(t.get("slug")) or "": clean_text(_str(t.get("name"))) or ""
            for t in _list(a.get("terms"))
            if isinstance(t, dict)
        }
        terms[aname.casefold()] = slugs
        if not a.get("has_variations"):
            params += _attribute(aname, a)
    axes = {}
    for v in _list(raw.get("variations")):
        if isinstance(v, dict) and _positive(v.get("id")):
            axes[v["id"]] = [
                (n, _str(x.get("value")) or "")
                for x in _list(v.get("attributes"))
                if isinstance(x, dict) and (n := clean_text(_str(x.get("name"))))
            ]
    return _Parent(
        name=name,
        category_ids=_category_ids(raw),
        description=_description(raw),
        vendor=_vendor(raw),
        pictures=_pictures(raw),
        params=params,
        terms=terms,
        axes=axes,
    )


def _product(raw: Any, parent: _Parent | None, paths: dict[str, list[str]]) -> Product | None:
    if not isinstance(raw, dict) or not _positive(raw.get("id")):
        return None
    price = _price(raw.get("prices"))
    url = _url(html.unescape(_str(raw.get("permalink")) or ""))
    name = clean_text(_str(raw.get("name"))) or (parent.name if parent else None)
    if price is None or url is None or not name:
        return None
    if parent is None:
        category_ids, params = _category_ids(raw), _params(raw)
        description, vendor, pictures = _description(raw), _vendor(raw), _pictures(raw)
    else:
        if axis := clean_text(_str(raw.get("variation"))):
            name = f"{name} — {axis}"
        category_ids = _category_ids(raw) or parent.category_ids
        params = parent.params + [
            Param(name=n, value=v)
            for n, slug in parent.axes.get(raw["id"], [])
            if (v := parent.terms.get(n.casefold(), {}).get(slug) or clean_text(slug))
        ]
        description = _description(raw) or parent.description
        vendor = _vendor(raw) or parent.vendor
        pictures = _pictures(raw) or parent.pictures
    category_id = max(category_ids, key=lambda c: len(paths.get(c, [])), default=None)
    current, old, currency = price
    discount = None
    if old is not None:
        discount = int(((old - current) * 100 / old).to_integral_value(ROUND_HALF_UP))
    stock = raw.get("is_in_stock")
    try:
        return Product(
            id=str(raw["id"]),
            name=name,
            url=url,
            price=Price(
                current=Money(amount=current, currency=currency),
                old=Money(amount=old, currency=currency) if old is not None else None,
                discount_percent=discount,
            ),
            available=stock if isinstance(stock, bool) else None,
            category_id=category_id,
            category_path=paths.get(category_id, []) if category_id else [],
            vendor=vendor,
            pictures=[u for u in map(_url, pictures) if u],
            description=description,
            params=params,
            group_id=str(raw["parent"]) if parent is not None else None,
        )
    except ValidationError:
        return None


def _price(raw: Any) -> tuple[Decimal, Decimal | None, str] | None:
    """(current, old, currency); old only when it is above the current price."""
    if not isinstance(raw, dict):
        return None
    unit, code = raw.get("currency_minor_unit"), raw.get("currency_code")
    if not isinstance(unit, int) or isinstance(unit, bool) or not 0 <= unit <= MAX_MINOR_UNIT:
        return None
    if not isinstance(code, str) or not _CURRENCY.match(code):
        return None
    current = _amount(raw.get("price"), unit)
    if current is None:
        return None
    regular = _amount(raw.get("regular_price"), unit)
    old = regular if regular is not None and regular > current else None
    return current, old, code.upper()


def _amount(value: Any, unit: int) -> Decimal | None:
    """`"1899"`, 2 -> 18.99: the Store API sends integers of minor units as strings."""
    if not isinstance(value, str) or not _MINOR.match(value):
        return None
    return Decimal(value).scaleb(-unit)


def _params(raw: dict[str, Any]) -> list[Param]:
    params = []
    for a in _list(raw.get("attributes")):
        if isinstance(a, dict) and (name := clean_text(_str(a.get("name")))):
            params += _attribute(name, a)
    return params


def _attribute(name: str, a: dict[str, Any]) -> list[Param]:
    values = [
        v
        for t in _list(a.get("terms"))
        if isinstance(t, dict) and (v := clean_text(_str(t.get("name"))))
    ]
    return [Param(name=name, value=", ".join(values))] if values else []


def _category_ids(raw: dict[str, Any]) -> list[str]:
    return [
        str(c["id"])
        for c in _list(raw.get("categories"))
        if isinstance(c, dict) and _positive(c.get("id"))
    ]


def _description(raw: dict[str, Any]) -> str | None:
    text = clean_text(_str(raw.get("description"))) or clean_text(
        _str(raw.get("short_description"))
    )
    return text[:MAX_DESCRIPTION] if text else None


def _vendor(raw: dict[str, Any]) -> str | None:
    brands = [b for b in _list(raw.get("brands")) if isinstance(b, dict)]
    return clean_text(_str(brands[0].get("name"))) if brands else None


def _pictures(raw: dict[str, Any]) -> list[str]:
    srcs = [
        html.unescape(s)
        for i in _list(raw.get("images"))
        if isinstance(i, dict) and (s := _str(i.get("src")))
    ]
    return srcs[:MAX_PICTURES]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None

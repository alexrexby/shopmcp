"""Which store engine runs a site: `probe()` collects evidence, `detect()` judges it.

`detect()` is a pure function over `Evidence`, so every rule is tested on synthetic
responses. `probe()` is the only network part: GET only, no cookies, at most
`MAX_REQUESTS` requests (the home page, the always-cheap Store API and GraphQL probes,
and a confirmation probe for each engine the home page hints at).

Signs come from the live reconnaissance of 14.10.2026 (plan-engines.md §2.7). A sign is
DECISIVE (only this engine answers so) or a HINT (seen on this engine, but a theme, a
CDN or a plugin elsewhere could show it too). Two engines with decisive signs give
candidates and no choice. A page closed by an antibot gives no engine and says so: the
engine is unknown, not absent.
"""

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shopmcp import __version__
from shopmcp.engines import Engine

MAX_REQUESTS = 8
TIMEOUT = 10.0  # seconds, per request
MAX_BODY = 512 * 1024
USER_AGENT = f"shopmcp-scaffold/{__version__}"
MAX_REDIRECTS = 3
MAGENTO_QUERY = "{storeConfig{store_code}}"

# Statuses an antibot or a closed site answers the home page with.
_BLOCKED_STATUSES = frozenset({401, 403, 429, 503})
# Challenge pages served with 200. Narrow on purpose: a shop page may carry a reCAPTCHA form.
_CHALLENGE = re.compile(
    r"<title>\s*Just a moment|captcha-delivery\.com|Checking your browser|DDoS-Guard</title>",
    re.IGNORECASE,
)


class Weight(StrEnum):
    DECISIVE = "decisive"
    HINT = "hint"


@dataclass(frozen=True)
class Fetch:
    """One GET as it came back; `status` is None when there was no HTTP response."""

    url: str = ""
    status: int | None = None
    headers: tuple[tuple[str, str], ...] = ()  # (lowercased name, value); repeats kept
    body: str = ""
    error: str | None = None

    def header(self, name: str) -> str:
        return ", ".join(v for k, v in self.headers if k == name.lower())

    def cookies(self) -> list[str]:
        """Cookie names the response sets."""
        return [v.split("=", 1)[0].strip() for k, v in self.headers if k == "set-cookie"]

    def json(self) -> Any:
        if "json" not in self.header("content-type"):
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None


@dataclass(frozen=True)
class Evidence:
    site: str
    home: Fetch
    probes: Mapping[Engine, Fetch] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    engine: Engine
    weight: Weight
    what: str


@dataclass(frozen=True)
class Detection:
    engine: Engine | None
    candidates: tuple[Engine, ...] = ()  # set when the engine is ambiguous
    signals: tuple[Signal, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str | None = None  # why `engine` is None
    headless: bool = False  # a separate JS storefront: the engine's GET cart links fail
    ecwid_store_id: str | None = None


# --- signs on the home page ----------------------------------------------------------

_HTML_HINTS: tuple[tuple[Engine, str, str], ...] = (
    (
        Engine.BITRIX,
        r"/bitrix/(?:js|templates|cache|components)/|BX\.message\(",
        "/bitrix/ in HTML",
    ),
    (Engine.INSALES, r"cdn\.insales-shop\.ru|insales_counter", "cdn.insales-shop.ru in HTML"),
    (Engine.OPENCART, r"catalog/view/(?:theme|javascript)/", "catalog/view/theme/ in HTML"),
    (Engine.TILDA, r"tildacdn\.com|data-tilda-page-id", "tildacdn.com in HTML"),
    (Engine.SHOP_SCRIPT, r"/wa-data/public/shop/|/wa-apps/shop/", "/wa-data/public/shop/ in HTML"),
    (Engine.SHOPIFY, r"cdn\.shopify\.com|Shopify\.shop\b", "cdn.shopify.com in HTML"),
    (Engine.WOOCOMMERCE, r"\bwoocommerce(?:-js|-page|-cart)?\b", "woocommerce classes in HTML"),
    (Engine.MAGENTO, r"text/x-magento-init|data-mage-init", "text/x-magento-init in HTML"),
    (Engine.PRESTASHOP, r"\bvar prestashop\s*=|/modules/ps_", "var prestashop in HTML"),
)
_ECWID_SCRIPT = re.compile(r"app\.ecwid\.com/script\.js\?(\d+)")
_PRESTASHOP_COOKIE = re.compile(r"^PrestaShop-[0-9a-f]{32}$")
_TILDA_STORE = re.compile(r"\bt-store\b|\bt706\b|t_store_init\(|\btcart\b")
_PRESTASHOP_17 = re.compile(r"\bvar prestashop\s*=")
_NEXT = re.compile(r"/_next/static/")
# A catalog page linked from the home page: a headless Bitrix may keep a classic home page
# and render the catalog with Next.js (seen live, 15.10.2026).
_CATALOG_LINK = re.compile(r"""href=["'](/catalog/[^"'?#\s]+)""")
# WordPress names its REST base in a `Link` header and in a <link> tag.
_WP_API = re.compile(r'<([^>]+/wp-json/?)>\s*;\s*rel="https://api\.w\.org/"')
_WP_API_HTML = re.compile(r"""<link[^>]+rel=["']https://api\.w\.org/["'][^>]*href=["']([^"']+)""")


def _home_signals(home: Fetch) -> list[Signal]:
    found = []

    def add(engine: Engine, weight: Weight, what: str) -> None:
        found.append(Signal(engine, weight, what))

    if "bitrix" in home.header("x-powered-cms").lower():
        add(Engine.BITRIX, Weight.DECISIVE, "X-Powered-CMS: Bitrix header")
    if home.header("x-tilda-server"):
        add(Engine.TILDA, Weight.DECISIVE, "x-tilda-server header")
    if "shopify" in home.header("powered-by").lower():
        add(Engine.SHOPIFY, Weight.DECISIVE, "powered-by: Shopify header")
    cookies = home.cookies()
    if any(_PRESTASHOP_COOKIE.match(c) for c in cookies):
        add(Engine.PRESTASHOP, Weight.DECISIVE, "PrestaShop-<md5> cookie")
    if "OCSESSID" in cookies:
        add(Engine.OPENCART, Weight.HINT, "OCSESSID cookie")
    if "first_current_location" in cookies:
        add(Engine.INSALES, Weight.HINT, "first_current_location cookie")
    if "shop_cart" in cookies:
        add(Engine.SHOP_SCRIPT, Weight.HINT, "shop_cart cookie")
    if m := _ECWID_SCRIPT.search(home.body):
        add(Engine.ECWID, Weight.DECISIVE, f"app.ecwid.com/script.js?{m.group(1)}")
    host = urllib.parse.urlsplit(home.url).hostname or ""
    if host.endswith(".company.site"):
        add(Engine.ECWID, Weight.HINT, "*.company.site host")
    for engine, pattern, what in _HTML_HINTS:
        if re.search(pattern, home.body):
            add(engine, Weight.HINT, what)
    return found


# --- confirmation probes -----------------------------------------------------------------

# Engines whose decisive sign needs a request of its own; Woo and Magento are probed always.
_ALWAYS = (Engine.WOOCOMMERCE, Engine.MAGENTO)
_ON_HINT = (Engine.INSALES, Engine.OPENCART, Engine.SHOP_SCRIPT)


def _wp_api(home: Fetch) -> str | None:
    m = _WP_API.search(home.header("link")) or _WP_API_HTML.search(home.body)
    return m.group(1) if m else None


def _wp_api_base(home: Fetch, origin: str) -> str:
    """WordPress may live in a subdirectory: its `rel=api.w.org` link names the REST base.
    Only a base on the site's own host is taken."""
    if api := _wp_api(home):
        base = urllib.parse.urljoin(origin + "/", api)
        if urllib.parse.urlsplit(base).netloc == urllib.parse.urlsplit(origin).netloc:
            return base.rstrip("/")
    return origin + "/wp-json"


def probe_urls(home: Fetch, origin: str) -> dict[Engine, str]:
    """The confirmation requests `probe()` makes after the home page, by engine."""
    hinted = {s.engine for s in _home_signals(home)}
    urls = {
        Engine.WOOCOMMERCE: _wp_api_base(home, origin) + "/wc/store/v1/products?per_page=1",
        Engine.MAGENTO: f"{origin}/graphql?query={urllib.parse.quote(MAGENTO_QUERY)}",
    }
    paths = {
        Engine.INSALES: "/cart_items.json",
        Engine.OPENCART: "/index.php?route=api/cart/products",
        Engine.SHOP_SCRIPT: "/api.php/shop.cart.getList",
    }
    for engine in _ON_HINT:
        if engine in hinted:
            urls[engine] = origin + paths[engine]
    if Engine.BITRIX in hinted and (m := _CATALOG_LINK.search(home.body)):
        urls[Engine.BITRIX] = origin + m.group(1)
    return urls


def _probe_signal(engine: Engine, r: Fetch) -> Signal | None:
    data = r.json()
    if engine is Engine.WOOCOMMERCE:
        if r.status == 200 and isinstance(data, list) and r.header("x-wp-total"):
            return Signal(engine, Weight.DECISIVE, "Store API /wc/store/v1/products answers")
    elif engine is Engine.MAGENTO:
        inner = data.get("data") if isinstance(data, dict) else None
        if (
            r.status == 200
            and isinstance(inner, dict)
            and isinstance(inner.get("storeConfig"), dict)
        ):
            return Signal(engine, Weight.DECISIVE, "/graphql answers storeConfig")
    elif engine is Engine.INSALES:
        if isinstance(data, dict) and "order_lines" in data:
            return Signal(engine, Weight.DECISIVE, "/cart_items.json has order_lines")
    elif engine is Engine.OPENCART:
        if data is None and (start := r.body.find('{"error":')) >= 0:
            # PHP notices printed before the JSON turn it into text/html (seen live).
            try:
                data = json.loads(r.body[start:])
            except ValueError:
                data = None
        if isinstance(data, dict) and "error" in data:
            return Signal(engine, Weight.DECISIVE, "route=api/cart/products answers a JSON error")
    elif (
        engine is Engine.SHOP_SCRIPT
        and isinstance(data, dict)
        and data.get("error") == "token_required"
    ):
        return Signal(engine, Weight.DECISIVE, "/api.php answers token_required")
    return None


# --- judgement -----------------------------------------------------------------------------


def _blocked(home: Fetch) -> str | None:
    if home.status is None:
        return f"site unreachable ({home.error or 'no response'}); set the engine with --engine"
    challenge = 200 <= home.status < 300 and _CHALLENGE.search(home.body)
    if home.status in _BLOCKED_STATUSES or challenge:
        return (
            f"home page closed by an antibot or access rule (HTTP {home.status});"
            " the engine is unknown, set it with --engine"
        )
    if home.status >= 400:
        return f"home page answered HTTP {home.status}; set the engine with --engine"
    return None


def detect(evidence: Evidence) -> Detection:
    home = evidence.home
    reason = _blocked(home)
    if reason:
        return Detection(engine=None, reason=reason)

    signals = _home_signals(home)
    for engine, r in evidence.probes.items():
        if s := _probe_signal(engine, r):
            signals.append(s)
    decisive = list(dict.fromkeys(s.engine for s in signals if s.weight is Weight.DECISIVE))
    hinted = list(dict.fromkeys(s.engine for s in signals))
    warnings = []

    site_host = urllib.parse.urlsplit(evidence.site).hostname
    home_host = urllib.parse.urlsplit(home.url).hostname
    if home.url and site_host and home_host and site_host != home_host:
        warnings.append(f"{site_host} redirects to {home_host}: consider --site with that host")

    engine: Engine | None = None
    candidates: tuple[Engine, ...] = ()
    reason = None
    if len(decisive) == 1:
        engine = decisive[0]
    elif len(decisive) > 1:
        candidates = tuple(decisive)
        reason = "decisive signs of several engines; choose one with --engine"
    elif len(hinted) == 1:
        engine = hinted[0]
        warnings.append(f"{engine.value}: weak signs only, no decisive one; check the engine")
    elif len(hinted) > 1:
        candidates = tuple(hinted)
        reason = "weak signs of several engines; choose one with --engine"
    else:
        reason = "no engine signs found; set the engine with --engine"
        if _wp_api(home):
            warnings.append("WordPress without a WooCommerce Store API")
        # Ecwid is often injected by a site builder after load: curl does not see it.
        warnings.append("an Ecwid widget loaded by a script is invisible here; check a browser")

    headless = False
    catalog = evidence.probes.get(Engine.BITRIX, Fetch())
    if engine is Engine.BITRIX and (_NEXT.search(home.body) or _NEXT.search(catalog.body)):
        headless = True
        warnings.append("Bitrix behind a Next.js storefront: add-to-cart links do not work")
    if engine is Engine.TILDA and not _TILDA_STORE.search(home.body):
        warnings.append("Tilda without a Store block on the home page: check it sells online")
    if engine is Engine.PRESTASHOP and not _PRESTASHOP_17.search(home.body):
        warnings.append("PrestaShop without `var prestashop`: 1.6 or a custom theme")
    m = _ECWID_SCRIPT.search(home.body)
    return Detection(
        engine=engine,
        candidates=candidates,
        signals=tuple(signals),
        warnings=tuple(warnings),
        reason=reason,
        headless=headless,
        ecwid_store_id=m.group(1) if m and engine is Engine.ECWID else None,
    )


# --- network -------------------------------------------------------------------------------


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS


_OPENER = urllib.request.build_opener(_Redirects())  # no HTTPCookieProcessor: no cookies


def fetch(url: str) -> Fetch:
    """One GET; HTTP errors are evidence too, so they come back as a `Fetch`."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        r = _OPENER.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        r = e
    except (OSError, ValueError, http.client.HTTPException) as e:
        return Fetch(url=url, error=f"{type(e).__name__}: {getattr(e, 'reason', e)}")
    with r:
        try:
            raw = r.read(MAX_BODY) if r.fp is not None else b""
        except (OSError, http.client.HTTPException) as e:
            return Fetch(url=url, error=f"{type(e).__name__}: {e}")
        charset = r.headers.get_content_charset() or "utf-8"
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")
        headers = tuple((k.lower(), v) for k, v in r.headers.items())
        return Fetch(url=r.geturl(), status=r.status, headers=headers, body=body)


def probe(site: str, get: Callable[[str], Fetch] = fetch) -> Evidence:
    """GET the home page, then the confirmation probes; nothing more after a closed page."""
    parts = urllib.parse.urlsplit(site)
    home = get(urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", "")))
    if _blocked(home):
        return Evidence(site=site, home=home)
    final = urllib.parse.urlsplit(home.url or site)
    origin = f"{final.scheme}://{final.netloc}"
    urls = list(probe_urls(home, origin).items())[: MAX_REQUESTS - 1]
    return Evidence(site=site, home=home, probes={engine: get(url) for engine, url in urls})

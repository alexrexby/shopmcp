"""The form_post trampoline over the assembled app: page, headers, escaping, bad tokens.

The carts are the InSales (single) and Shop-Script (chain) presets of engines.py on the
demo host. What the browser would submit is read back with html.parser, as a browser
reads it, so an escaping mistake shows up as a wrong field, not as a string mismatch.
"""

import base64
import hashlib
import json
import re
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
from fakes import FakeAdapter, make_product
from test_app import auth_cfg, serve
from test_tools_spec import cfg

from shopmcp.cart import form_post, strategies
from shopmcp.cart.refs import CartRef
from shopmcp.cart.token import sign
from shopmcp.config import ConfigError, FormPostCart, StoreConfig
from shopmcp.domain.models import CartHandoff, CartLine
from shopmcp.engines import PRESETS, Engine
from shopmcp.i18n import Texts
from shopmcp.server.app import build_app
from shopmcp.server.trampoline import CHAIN_SCRIPT, SINGLE_SCRIPT, STYLE, script_json

HOST = "demo.example"  # the demo store's site: action and finish_url must stay on it
SECRET = "trampoline-secret-0123456789-abcdef"
REFS = {
    "2002": CartRef(product_id="1001", variant_id="2002"),
    "3003": CartRef(product_id="3003"),
}
PRODUCTS = {"2002": make_product("2002", "Кофе, вариант"), "3003": make_product("3003", "Чай")}
LINES = [
    CartLine(product_id="2002", quantity=Decimal("2.000")),
    CartLine(product_id="3003", quantity=Decimal(1)),
]


def preset_form(engine: Engine) -> dict[str, Any]:
    cart = PRESETS[engine].alt_cart
    assert cart is not None and cart["strategy"] == "form_post"
    raw = json.dumps(cart).replace("{host}", HOST)
    return {**json.loads(raw), "secret": SECRET}


INSALES = preset_form(Engine.INSALES)  # single: variant_ids[{id}] = {qty}
SHOP_SCRIPT = preset_form(Engine.SHOP_SCRIPT)  # chain: product_id, sku_id, quantity


def store(cart: dict[str, Any], **changes: object) -> StoreConfig:
    return cfg(cart=cart, **changes)


async def link(c: StoreConfig, lines=LINES, products=PRODUCTS, refs=REFS) -> CartHandoff:
    strategy = strategies.create(
        c.cart,
        FakeAdapter(),
        Texts("ru"),
        site=c.site,
        public_url=c.server.public_url,
        store_id=c.id,
    )
    return await strategy.handoff(lines, products, refs)


async def get(c: StoreConfig, path: str) -> httpx2.Response:
    async with serve(build_app(c, adapter=FakeAdapter())) as http:
        return await http.get(path)


async def page(c: StoreConfig, **kw: Any) -> httpx2.Response:
    res = await link(c, **kw)
    assert res.kind == "form_post" and res.url is not None
    return await get(c, urlsplit(str(res.url)).path)


class Page(HTMLParser):
    """Forms, scripts and links as a browser sees them (attribute values unescaped)."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self.styles: list[str] = []
        self.links: list[str | None] = []
        self._in: str | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"action": a.get("action"), "method": a.get("method"), "fields": []})
        elif tag == "input":
            self.forms[-1]["fields"].append((a["name"], a["value"]))
        elif tag == "a":
            self.links.append(a.get("href"))
        elif tag in ("script", "style"):
            self._in = tag
            if tag == "script":
                self.scripts.append((a, ""))

    def handle_data(self, data: str) -> None:
        if self._in == "script":
            attrs, text = self.scripts[-1]
            self.scripts[-1] = (attrs, text + data)
        elif self._in == "style":
            self.styles.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._in:
            self._in = None


def sha256(source: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode() + "'"


def csp(res: httpx2.Response) -> dict[str, str]:
    parts = (p.strip().partition(" ") for p in res.headers["content-security-policy"].split(";"))
    return {name: value for name, _, value in parts}


def chain_data(p: Page) -> dict[str, Any]:
    [(_, text)] = [s for s in p.scripts if s[0].get("type") == "application/json"]
    return json.loads(text)


# --- the link -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_link_is_the_trampoline_on_the_server_origin_with_the_token_expiry():
    c = store(INSALES, server__public_url="https://mcp.demo.example/shop/mcp")
    res = await link(c)
    url = urlsplit(str(res.url))
    assert (url.scheme, url.netloc) == ("https", "mcp.demo.example")
    assert url.path.startswith(form_post.PATH) and res.expires_at is not None
    assert [str(x.url) for x in res.links] == [
        "https://shop.example/p/2002",
        "https://shop.example/p/3003",
    ]


def test_a_placeholder_a_form_cannot_fill_fails_the_start():
    cart = {**INSALES, "fields": [["variant_ids[{id}]", "{qty}"], ["back", "{url}"]]}
    with pytest.raises(ConfigError, match="url"):
        build_app(store(cart), adapter=FakeAdapter())


# --- single (InSales) -----------------------------------------------------------------


@pytest.mark.anyio
async def test_single_posts_every_line_in_one_form():
    p = Page((await page(store(INSALES))).text)
    assert p.forms == [
        {
            "action": f"https://{HOST}/cart_items",
            "method": "post",
            "fields": [("variant_ids[2002]", "2"), ("variant_ids[3003]", "1")],
        }
    ]


@pytest.mark.anyio
async def test_single_headers():
    res = await page(store(INSALES))
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    p = Page(res.text)
    [(_, script)] = p.scripts
    assert script == SINGLE_SCRIPT and p.styles == [STYLE]
    policy = csp(res)
    # The page's own script and style are what the hashes allow; nothing inline else.
    assert policy == {
        "default-src": "'none'",
        "script-src": sha256(script),
        "style-src": sha256(STYLE),
        "form-action": f"https://{HOST}",
        "base-uri": "'none'",
        "frame-ancestors": "'none'",
    }
    assert res.headers["referrer-policy"] == "no-referrer"
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-robots-tag"] == "noindex"
    assert res.headers["x-content-type-options"] == "nosniff"


# --- chain (Shop-Script) --------------------------------------------------------------


@pytest.mark.anyio
async def test_chain_json_has_a_form_per_line_and_empty_fields_left_out():
    res = await page(store(SHOP_SCRIPT))
    p = Page(res.text)
    data = chain_data(p)
    forms = [
        [["product_id", "1001"], ["sku_id", "2002"], ["quantity", "2"]],
        [["product_id", "3003"], ["quantity", "1"]],  # no variant: sku_id is not sent
    ]
    assert data["forms"] == forms
    assert data["action"] == f"https://{HOST}/cart/add/"
    assert data["finish"] == f"https://{HOST}/cart/"
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", data["jti"])
    # Without JavaScript: the same forms as buttons, and a link to the cart.
    assert [f["fields"] for f in p.forms] == [[tuple(x) for x in f] for f in forms]
    assert {f["action"] for f in p.forms} == {f"https://{HOST}/cart/add/"}
    assert p.links == [f"https://{HOST}/cart/"]


@pytest.mark.anyio
async def test_chain_headers():
    res = await page(store(SHOP_SCRIPT))
    [script] = [text for attrs, text in Page(res.text).scripts if "type" not in attrs]
    assert script == CHAIN_SCRIPT
    policy = csp(res)
    assert policy["script-src"] == sha256(CHAIN_SCRIPT) and "unsafe" not in policy["script-src"]
    # The store redirects the POST back here: form-action covers that redirect too.
    assert policy["form-action"] == f"https://{HOST} 'self'"
    assert policy["base-uri"] == policy["frame-ancestors"] == "'none'"
    # The store sends the browser back to the Referer: the full URL of this page.
    assert res.headers["referrer-policy"] == "unsafe-url"
    assert res.headers["cache-control"] == "no-store"


def test_chain_script_builds_forms_without_markup():
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in CHAIN_SCRIPT
    assert "createElement" in CHAIN_SCRIPT and ".value = pair[1]" in CHAIN_SCRIPT


# --- escaping: ids are any printable text ---------------------------------------------

HOSTILE = [
    '"><script>alert(1)</script>',
    "' onfocus=alert(1) x='",
    "</script><script>alert(1)</script><!--",
    "a&amp;b <i>",
    "Кофе/зерно 1кг",  # a form value goes as is: the browser encodes it, not us
]


@pytest.mark.anyio
@pytest.mark.parametrize("bad", HOSTILE)
async def test_hostile_id_stays_a_value_in_a_single_form(bad):
    refs = {"2002": CartRef(product_id=bad)}
    res = await page(store(INSALES), lines=LINES[:1], refs=refs)
    p = Page(res.text)
    assert [s for _, s in p.scripts] == [SINGLE_SCRIPT]  # nothing else became a script
    assert p.forms[0]["fields"] == [(f"variant_ids[{bad}]", "2")]


@pytest.mark.anyio
@pytest.mark.parametrize("bad", HOSTILE)
async def test_hostile_id_stays_a_value_in_a_chain(bad):
    refs = {"2002": CartRef(product_id=bad, variant_id=bad)}
    res = await page(store(SHOP_SCRIPT), lines=LINES[:1], refs=refs)
    p = Page(res.text)
    assert len(p.scripts) == 2 and p.scripts[1][1] == CHAIN_SCRIPT
    assert chain_data(p)["forms"] == [[["product_id", bad], ["sku_id", bad], ["quantity", "2"]]]
    assert p.forms[0]["fields"] == [("product_id", bad), ("sku_id", bad), ("quantity", "2")]


def test_script_json_cannot_end_the_script_or_the_line():
    data = {"x": "</script><!--&  "}
    text = script_json(data)
    assert text.isascii() and not set("<>&") & set(text)
    assert " " not in text and json.loads(text) == data


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["a]b", "a[x]", "[0]"])
async def test_bracket_in_a_field_name_degrades_to_links(bad):
    refs = {"2002": CartRef(product_id=bad)}
    res = await link(store(INSALES), lines=LINES[:1], refs=refs)
    assert res.kind == "link_list" and res.url is None and res.note
    assert [x.product_id for x in res.links] == ["2002"]


@pytest.mark.anyio
async def test_bracket_in_a_field_value_is_just_a_value():
    refs = {"2002": CartRef(product_id="a]b")}  # Shop-Script: ids go into values only
    p = Page((await page(store(SHOP_SCRIPT), lines=LINES[:1], refs=refs)).text)
    assert chain_data(p)["forms"] == [[["product_id", "a]b"], ["quantity", "2"]]]


# --- bad tokens -----------------------------------------------------------------------


def token_for(c: StoreConfig, **kw: Any) -> str:
    args: dict[str, Any] = {
        "store": c.id,
        "secret": SECRET.encode(),
        "kid": form_post.KEY_ID,
        "ttl": 3600,
    }
    return sign([(REFS["2002"], Decimal(1))], **{**args, **kw})


def _tampered(c: StoreConfig) -> str:
    body, _, mac = token_for(c).partition(".")
    return f"{body}.{'A' if mac[0] != 'A' else 'B'}{mac[1:]}"


BAD_TOKENS = {
    "tampered": _tampered,
    "expired": lambda c: token_for(c, now=1_000_000, ttl=300),
    "other store": lambda c: token_for(c, store="other"),
    "other secret": lambda c: token_for(c, secret=b"x" * 32),
    "garbage": lambda c: "not-a-token",
    "too long": lambda c: "a" * 1900 + ".b",
}


@pytest.mark.anyio
@pytest.mark.parametrize("make", BAD_TOKENS.values(), ids=BAD_TOKENS.keys())
async def test_bad_token_is_400_html_with_a_link_to_the_site(make):
    c = store(INSALES)
    res = await get(c, form_post.PATH + make(c))
    assert res.status_code == 400 and res.headers["content-type"].startswith("text/html")
    p = Page(res.text)
    assert p.forms == [] and p.scripts == [] and p.links == [str(c.site)]
    assert csp(res)["script-src"] == "'none'" and csp(res)["form-action"] == "'none'"
    assert res.headers["cache-control"] == "no-store"
    assert "устарела" in res.text


@pytest.mark.anyio
async def test_good_token_of_the_same_shape_passes():
    # The 400s above are about the token, not the route: the same path with a valid one is 200.
    c = store(INSALES)
    assert (await get(c, form_post.PATH + token_for(c))).status_code == 200


# --- routing --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_trampoline_route_without_form_post():
    c = cfg()  # the demo store: url_template
    res = await get(c, form_post.PATH + token_for(c))
    assert res.status_code == 404


@pytest.mark.anyio
async def test_trampoline_is_outside_the_auth():
    # A customer's browser opens it: no bearer token, still the page.
    c = auth_cfg(cart=INSALES)
    assert (await get(c, form_post.PATH + token_for(c))).status_code == 200


def test_form_post_cart_is_what_the_preset_gives():
    assert isinstance(store(INSALES).cart, FormPostCart)
    assert isinstance(store(SHOP_SCRIPT).cart, FormPostCart)

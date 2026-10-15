"""GET cart strategies against the engine presets: golden links per engine.

The golden strings follow the formats seen working live on 14.10.2026 (plan-engines.md §0,
reconnaissance §3 per engine), with a made-up host. Two lines everywhere: a variant with
qty 2 and a plain product with qty 1.
"""

import base64
import json
import re
from decimal import Decimal
from types import SimpleNamespace as LineIn
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
from fakes import FakeAdapter, make_product
from pydantic import HttpUrl, TypeAdapter
from test_tools_catalog import call

from shopmcp.adapters.yml.feed import CartRefRule
from shopmcp.cart import strategies, token
from shopmcp.cart.refs import CartRef
from shopmcp.cart.strategies import MAX_URL_LENGTH
from shopmcp.cart.token import verify
from shopmcp.config import CartConfig, LinkListCart, ReceiverCart, UrlTemplateCart
from shopmcp.domain.models import CartLine
from shopmcp.engines import CART_SECRET, PRESETS, SHOPIFY_FEED_ID, Engine
from shopmcp.errors import InvalidInput
from shopmcp.i18n import Texts

HOST = "shop.example"
SECRET = "receiver-secret-0123456789-abcdefgh"

# Product.id (the feed id) -> what the cart is keyed by.
REFS = {
    "2002": CartRef(product_id="1001", variant_id="2002"),
    "3003": CartRef(product_id="3003"),
}
PRODUCTS = {"2002": make_product("2002", "Кофе, вариант"), "3003": make_product("3003", "Чай")}
LINES = [
    CartLine(product_id="2002", quantity=Decimal("2.000")),
    CartLine(product_id="3003", quantity=Decimal(1)),
]


def preset_cart(engine: Engine, *, alt: bool = False) -> CartConfig:
    preset = PRESETS[engine]
    raw = json.dumps(preset.alt_cart if alt else preset.cart)
    origin = f"https://{HOST}"
    raw = raw.replace("{origin}", origin).replace("{base}", origin).replace("{cart_path}", "panier")
    raw = raw.replace(CART_SECRET, SECRET)  # form_post: the ${ENV} reference is load()'s job
    return TypeAdapter(CartConfig).validate_python(json.loads(raw))


def strategy(cart: CartConfig) -> strategies.CartStrategy:
    return strategies.create(
        cart,
        FakeAdapter(),
        Texts("ru"),
        site=HttpUrl(f"https://{HOST}/"),
        public_url=HttpUrl("https://mcp.shop.example/mcp"),
        store_id="demo",
    )


def ecwid(payload: dict) -> str:
    # What encodeURIComponent(JSON.stringify(cart)) gives for these ASCII-only payloads.
    return quote(json.dumps(payload, separators=(",", ":")), safe="")


ECWID_PAYLOAD = {"products": [{"id": 1001, "quantity": 2}, {"id": 3003, "quantity": 1}]}

# (engine, alt_cart, kind, url, add_urls)
GOLDEN = [
    (
        Engine.SHOPIFY,
        False,
        "url_template",
        f"https://{HOST}/cart/2002:2,3003:1?storefront=true",
        [None, None],
    ),
    (
        Engine.WOOCOMMERCE,
        False,
        "link_list",
        None,
        [
            f"https://{HOST}/cart/?add-to-cart=2002&quantity=2",
            f"https://{HOST}/cart/?add-to-cart=3003&quantity=1",
        ],
    ),
    (
        Engine.WOOCOMMERCE,
        True,
        "url_template",
        f"https://{HOST}/checkout-link/?products=2002:2,3003:1",
        [None, None],
    ),
    (
        Engine.ECWID,
        False,
        "json_fragment",
        f"https://{HOST}/#!/~/cart/create=" + ecwid(ECWID_PAYLOAD),
        [None, None],
    ),
    (
        Engine.BITRIX,
        False,
        "link_list",
        None,
        [
            f"https://{HOST}/p/2002?action=ADD2BASKET&id=2002&quantity=2",
            f"https://{HOST}/p/3003?action=ADD2BASKET&id=3003&quantity=1",
        ],
    ),
    (
        Engine.PRESTASHOP,
        False,
        "link_list",
        None,
        [
            f"https://{HOST}/panier?add=1&id_product=1001&id_product_attribute=2002&qty=2",
            # No combination: the parameter is left out, as in the live link.
            f"https://{HOST}/panier?add=1&id_product=3003&qty=1",
        ],
    ),
    (Engine.OPENCART, False, "link_list", None, [None, None]),
    (Engine.MAGENTO, False, "link_list", None, [None, None]),
    (Engine.TILDA, False, "link_list", None, [None, None]),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "engine, alt, kind, url, add_urls",
    GOLDEN,
    ids=[f"{g[0]}{'-alt' if g[1] else ''}" for g in GOLDEN],
)
async def test_preset_golden_links(engine, alt, kind, url, add_urls):
    res = await strategy(preset_cart(engine, alt=alt)).handoff(LINES, PRODUCTS, REFS)
    assert res.kind == kind
    assert (str(res.url) if res.url else None) == url
    assert [str(link.url) for link in res.links] == [
        f"https://{HOST}/p/2002",
        f"https://{HOST}/p/3003",
    ]
    assert [str(link.add_url) if link.add_url else None for link in res.links] == add_urls
    assert [link.quantity for link in res.links] == [2, 1]


def test_golden_covers_every_get_preset():
    # form_post (InSales, Shop-Script alt): test_form_post_presets_link_to_the_trampoline
    # here, the page in test_trampoline.py; a link_list without line_template is plain
    # product page links, the same for every engine.
    covered = {(g[0], g[1]) for g in GOLDEN}
    for engine, preset in PRESETS.items():
        for alt, cart in ((False, preset.cart), (True, preset.alt_cart)):
            if cart is None or cart["strategy"] == "form_post":
                continue
            if cart["strategy"] == "link_list" and "line_template" not in cart:
                continue
            if engine is Engine.ECWID and alt:
                continue  # Wix: test_ecwid_on_wix_is_base64
            assert (engine, alt) in covered


@pytest.mark.anyio
async def test_ecwid_on_wix_is_base64():
    res = await strategy(preset_cart(Engine.ECWID, alt=True)).handoff(LINES, PRODUCTS, REFS)
    head, _, code = str(res.url).partition("/!/~/cart/create=")
    assert res.kind == "json_fragment" and head == f"https://{HOST}"
    # Ecwid docs: encodeURIComponent(btoa(json)), the standard base64 alphabet with padding.
    assert json.loads(base64.b64decode(unquote(code), validate=True)) == ECWID_PAYLOAD


@pytest.mark.anyio
async def test_ecwid_options_and_non_numeric_ids():
    refs = {
        "2002": CartRef(product_id="1001", variant_id="2002", options={"Размер": "L"}),
        "3003": CartRef(product_id="007"),
    }
    res = await strategy(preset_cart(Engine.ECWID)).handoff(LINES, PRODUCTS, refs)
    payload = json.loads(unquote(str(res.url).partition("cart/create=")[2]))
    assert payload == {
        "products": [
            {"id": 1001, "quantity": 2, "options": {"Размер": "L"}},
            {"id": "007", "quantity": 1},  # a leading zero would be lost as a number
        ]
    }


# --- Shopify: the variant id out of the app's g:id (review M4) -----------------------


@pytest.mark.anyio
async def test_shopify_feed_id_gives_the_variant_in_the_cart_link():
    # The scaffold's [adapter.cart_ref] for Shopify: feed id -> pattern -> numeric variant.
    rule = CartRefRule(pattern=re.compile(SHOPIFY_FEED_ID))
    products = {
        "shopify_US_8123_4456": make_product("shopify_US_8123_4456", "Mug").model_copy(
            update={"group_id": "shopify_US_8123"}
        ),
        "SKU-9": make_product("SKU-9", "Tee"),  # another id form: goes to the link as it is
    }
    refs = {i: CartRef.model_validate(rule(p)) for i, p in products.items()}
    assert refs["shopify_US_8123_4456"] == CartRef(product_id="8123", variant_id="4456")
    lines = [
        CartLine(product_id="shopify_US_8123_4456", quantity=Decimal(2)),
        CartLine(product_id="SKU-9", quantity=Decimal(1)),
    ]
    res = await strategy(preset_cart(Engine.SHOPIFY)).handoff(lines, products, refs)
    assert str(res.url) == f"https://{HOST}/cart/4456:2,SKU-9:1?storefront=true"


# --- empty parameters: dropped per line, kept in a joined query (review M3) -----------


@pytest.mark.anyio
async def test_item_template_keeps_empty_parameters_to_hold_array_positions():
    cart = UrlTemplateCart(
        strategy="url_template",
        url_template=f"https://{HOST}/cart/add?{{items}}",
        item_template="product[]={product_id}&variant[]={variant_id}&qty[]={qty}",
    )
    lines = [LINES[1], LINES[0]]  # no variant first: its empty variant[] must stay
    res = await strategy(cart).handoff(lines, PRODUCTS, REFS)
    query = parse_qs(urlsplit(str(res.url)).query, keep_blank_values=True)
    assert query == {"product[]": ["3003", "1001"], "variant[]": ["", "2002"], "qty[]": ["1", "2"]}


@pytest.mark.anyio
async def test_line_template_still_drops_an_empty_parameter():
    cart = LinkListCart(line_template=f"https://{HOST}/add?p={{product_id}}&v={{variant_id}}")
    res = await strategy(cart).handoff(LINES, PRODUCTS, REFS)
    assert [str(link.add_url) for link in res.links] == [
        f"https://{HOST}/add?p=1001&v=2002",
        f"https://{HOST}/add?p=3003",
    ]


# --- {url} as a query value (review L1) ---------------------------------------------


@pytest.mark.anyio
async def test_url_in_a_query_value_is_encoded():
    page = f"https://{HOST}/p/2002?color=red&size=M#reviews"
    products = {"2002": make_product("2002", "Кофе").model_copy(update={"url": HttpUrl(page)})}
    cart = LinkListCart(line_template=f"https://{HOST}/cart/add?id={{id}}&ret={{url}}")
    lines = [CartLine(product_id="2002", quantity=Decimal(1))]
    res = await strategy(cart).handoff(lines, products, REFS)
    add_url = urlsplit(str(res.links[0].add_url))
    assert add_url.fragment == ""
    assert parse_qs(add_url.query) == {"id": ["2002"], "ret": [page]}


def test_quantity_has_one_formatter_for_links_and_tokens():
    # Review L4: the link and the token wrote quantities with two copies of one formula.
    assert not hasattr(strategies, "_qty") and not hasattr(token, "_qty")
    assert token.qty_text(Decimal("2.000")) == "2" and token.qty_text(Decimal("2E+1")) == "20"


# --- {url} that already has a query (Bitrix card links) -------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "page, add_url",
    [
        (
            f"https://{HOST}/catalog/coffee/2002/",
            f"https://{HOST}/catalog/coffee/2002/?action=ADD2BASKET&id=2002&quantity=2",
        ),
        (
            f"https://{HOST}/catalog/coffee/?oid=2002#reviews",
            f"https://{HOST}/catalog/coffee/?oid=2002&action=ADD2BASKET&id=2002&quantity=2#reviews",
        ),
    ],
)
async def test_bitrix_add_url_joins_the_card_query(page, add_url):
    products = {"2002": make_product("2002", "Кофе").model_copy(update={"url": HttpUrl(page)})}
    lines = [CartLine(product_id="2002", quantity=Decimal(2))]
    res = await strategy(preset_cart(Engine.BITRIX)).handoff(lines, products, REFS)
    assert str(res.links[0].add_url) == add_url
    assert parse_qs(urlsplit(add_url).query)["action"] == ["ADD2BASKET"]


# --- ids outside ASCII ---------------------------------------------------------------

ODD = "Кофе/зерно 1кг"  # Cyrillic, a slash, a space


Q = "%D0%9A%D0%BE%D1%84%D0%B5%2F%D0%B7%D0%B5%D1%80%D0%BD%D0%BE%201%D0%BA%D0%B3"  # ODD, encoded
ODD_LINKS = {
    (Engine.SHOPIFY, False): f"https://{HOST}/cart/{Q}:3?storefront=true",
    (Engine.WOOCOMMERCE, False): f"https://{HOST}/cart/?add-to-cart={Q}&quantity=3",
    (Engine.WOOCOMMERCE, True): f"https://{HOST}/checkout-link/?products={Q}:3",
    (Engine.BITRIX, False): f"https://{HOST}/p/x?action=ADD2BASKET&id={Q}&quantity=3",
    (Engine.PRESTASHOP, False): f"https://{HOST}/panier?add=1&id_product={Q}"
    f"&id_product_attribute={Q}&qty=3",
}


@pytest.mark.anyio
@pytest.mark.parametrize("engine, alt", [*ODD_LINKS, (Engine.ECWID, False), (Engine.ECWID, True)])
async def test_any_printable_id_makes_a_valid_link(engine, alt):
    products = {ODD: make_product("x", "Кофе")}
    refs = {ODD: CartRef(product_id=ODD, variant_id=ODD if engine is Engine.PRESTASHOP else None)}
    lines = [CartLine(product_id=ODD, quantity=Decimal(3))]
    res = await strategy(preset_cart(engine, alt=alt)).handoff(lines, products, refs)
    link = str(res.url or res.links[0].add_url)
    if engine is Engine.ECWID:
        code = unquote(link.partition("cart/create=")[2])
        payload = json.loads(base64.b64decode(code) if alt else code)
        assert payload == {"products": [{"id": ODD, "quantity": 3}]}
        assert link.isascii() and " " not in link
    else:
        assert link == ODD_LINKS[engine, alt]
        assert unquote(Q) == ODD


@pytest.mark.anyio
async def test_receiver_carries_any_printable_id():
    cart = ReceiverCart(
        strategy="receiver",
        url_template=f"https://{HOST}/mcp/cart/?mcp_cart={{token}}",
        secret=SECRET,
    )
    refs = {ODD: CartRef(product_id=ODD, options={"Помол": "мелкий"})}
    res = await strategy(cart).handoff(
        [CartLine(product_id=ODD, quantity=Decimal(1))], {ODD: make_product("x", "Кофе")}, refs
    )
    token = parse_qs(urlsplit(str(res.url)).query)["mcp_cart"][0]
    payload = verify(token, store="demo", secrets={"1": SECRET.encode()})
    assert payload.lines == [(refs[ODD], Decimal(1))]


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\u202eb", "", "x" * 129])
def test_unusable_id_is_invalid_input_naming_it(bad):
    with pytest.raises(InvalidInput) as e:
        CartRef(product_id=bad)
    assert e.value.details is not None and e.value.details["invalid_cart_id"] == bad


@pytest.mark.anyio
async def test_tool_reports_a_control_character_id_as_invalid_input():
    adapter = FakeAdapter(products=[make_product("bad\x01id", "Кофе")])
    with pytest.raises(InvalidInput) as e:
        await call(adapter, "cart_link_create", lines=[LineIn(id="bad\x01id", quantity=1)])
    assert e.value.details == {"invalid_cart_id": "bad\x01id", "reason": "control_character"}


# --- receiver ------------------------------------------------------------------------


def receiver(**kw) -> ReceiverCart:
    return ReceiverCart(
        strategy="receiver",
        url_template=f"https://{HOST}/mcp/cart/?mcp_cart={{token}}",
        secret=SECRET,
        **kw,
    )


@pytest.mark.anyio
async def test_receiver_link_is_a_signed_token_with_its_expiry():
    res = await strategy(receiver(key_id="k2", ttl_minutes=30)).handoff(LINES, PRODUCTS, REFS)
    assert res.kind == "receiver" and len(res.links) == 2
    url = urlsplit(str(res.url))
    assert (url.netloc, url.path) == (HOST, "/mcp/cart/")
    payload = verify(
        parse_qs(url.query)["mcp_cart"][0], store="demo", secrets={"k2": SECRET.encode()}
    )
    assert payload.lines == [(REFS["2002"], Decimal(2)), (REFS["3003"], Decimal(1))]
    assert payload.exp - payload.iat == 30 * 60
    assert res.expires_at is not None and int(res.expires_at.timestamp()) == payload.exp


# --- form_post ------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("engine", [Engine.INSALES, Engine.SHOP_SCRIPT])
async def test_form_post_presets_link_to_the_trampoline(engine):
    res = await strategy(preset_cart(engine, alt=True)).handoff(LINES, PRODUCTS, REFS)
    url = urlsplit(str(res.url))
    assert res.kind == "form_post" and (url.scheme, url.netloc) == ("https", "mcp.shop.example")
    assert url.path.startswith("/cart/h/") and not url.query
    payload = verify(
        url.path.removeprefix("/cart/h/"), store="demo", secrets={"1": SECRET.encode()}
    )
    assert payload.lines == [(REFS["2002"], Decimal(2)), (REFS["3003"], Decimal(1))]
    assert res.expires_at is not None and int(res.expires_at.timestamp()) == payload.exp
    assert [link.add_url for link in res.links] == [None, None]


# --- degradation over MAX_URL_LENGTH ---------------------------------------------------

LONG = [f"{i:04d}" + "x" * 120 for i in range(20)]


def long_cart() -> tuple[list[CartLine], dict, dict]:
    lines = [CartLine(product_id=i, quantity=Decimal(1)) for i in LONG]
    return (
        lines,
        {i: make_product(f"p{n}", i) for n, i in enumerate(LONG)},
        {i: CartRef(product_id=i) for i in LONG},
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "cart",
    [
        preset_cart(Engine.SHOPIFY),
        preset_cart(Engine.WOOCOMMERCE, alt=True),
        preset_cart(Engine.ECWID),
        preset_cart(Engine.ECWID, alt=True),
        receiver(),  # the token itself is over its limit first
        preset_cart(Engine.INSALES, alt=True),
        preset_cart(Engine.SHOP_SCRIPT, alt=True),
    ],
    ids=[
        "shopify",
        "woo-checkout-link",
        "ecwid",
        "ecwid-wix",
        "receiver",
        "insales-form",
        "shop-script-form",
    ],
)
async def test_too_long_degrades_to_link_list(cart):
    lines, products, refs = long_cart()
    res = await strategy(cart).handoff(lines, products, refs)
    assert res.kind == "link_list" and res.url is None and len(res.links) == len(LONG)
    assert res.note and res.expires_at is None


@pytest.mark.anyio
async def test_receiver_url_over_limit_with_a_short_token_degrades_too():
    cart = receiver()
    cart = cart.model_copy(update={"url_template": f"https://{HOST}/{'m' * 1900}?t={{token}}"})
    lines, products, refs = long_cart()
    res = await strategy(cart).handoff(lines[:1], products, refs)
    assert res.kind == "link_list" and res.note
    assert len(f"https://{HOST}/{'m' * 1900}") < MAX_URL_LENGTH

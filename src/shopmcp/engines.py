"""Store engine presets: what `shopmcp scaffold` writes into `[cart]` and `[adapter]`.

Data only: detecting the engine lives in `detect.py`, rendering in the CLI. Strings may
carry scaffold-time placeholders, replaced before the config is written and never seen
at runtime: `{origin}` (scheme, host and port of `--site`: a store on http keeps http),
`{base}` (the origin plus the path of `--site`, for an engine installed in a
subdirectory), `{host}` (host and port), `{feed}` (feed URL), `{cart_path}`
(PrestaShop's localized cart path). `${NAME}` is an environment variable, as anywhere in store.toml.
Every preset is the default that needs no change on the store side; `cart_upgrade` says
what a module or a snippet on the store would give instead.

Sources: live reconnaissance of 14.10.2026 (plan-engines.md §0, §2.3). Marks there say
which formats were seen working and which are documentation only.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shopmcp.i18n import L10n

CART_SECRET = "${SHOPMCP_CART_SECRET}"  # signs form_post/receiver tokens: >= 32 bytes
# The g:id of the Google & YouTube app: shopify_<country>_<product>_<variant> (plan §2.4,
# not verified live). Another id does not match and goes to the cart link as it is.
SHOPIFY_FEED_ID = "^shopify_[A-Za-z]{2}_(?P<product_id>[0-9]+)_(?P<variant_id>[0-9]+)$"


class Engine(StrEnum):
    BITRIX = "bitrix"
    INSALES = "insales"
    WOOCOMMERCE = "woocommerce"
    OPENCART = "opencart"
    TILDA = "tilda"
    SHOP_SCRIPT = "shop_script"
    ECWID = "ecwid"
    SHOPIFY = "shopify"
    MAGENTO = "magento"
    PRESTASHOP = "prestashop"


@dataclass(frozen=True)
class Preset:
    engine: Engine
    name: str
    cart: Mapping[str, Any]  # the [cart] table
    adapter: Mapping[str, Any] | None  # the [adapter] table; None: no catalog adapter
    notes: L10n  # what to check or fill in by hand
    cart_upgrade: L10n | None = None  # what a module/snippet on the store side gives
    # A cart that needs a fact scaffold cannot see (an engine version, a platform).
    alt_cart: Mapping[str, Any] | None = None
    alt_cart_when: L10n | None = None
    replaces_cart: bool = False  # the handoff replaces the customer's cart, not adds to it
    edit: tuple[str, ...] = field(default=())  # config keys the owner must fill in
    # [adapter.cart_ref] written active, not as a commented example: the owner checks it.
    cart_ref: Mapping[str, Any] | None = None


def _feed(fmt: str) -> dict[str, Any]:
    return {"kind": "feed", "format": fmt, "feed": "{feed}", "refresh_minutes": 60}


# form_post is beta until the trampoline is verified in live browsers (checks V1/V2):
# scaffold writes it commented out, the working default is product page links.
_FORM_POST_WHEN = L10n(
    ru="Трамплин form_post (БЕТА): только после живой проверки V1 на вашем магазине.",
    en="The form_post trampoline (BETA): only after live check V1 on your store.",
)
# Check V1 (Chromium, 15.10.2026, a Shop-Script stand): Webasyst on https sets the cart
# cookie SameSite=None, the chain kept the cart from a subdomain and from another site; on
# http the cookie is Lax, and from another site the cart kept only the chain's last line.
_SHOP_SCRIPT_FORM_WHEN = L10n(
    ru="Трамплин form_post (БЕТА): проверен в Chromium — на https магазина цепочка "
    "дописывает корзину и с поддомена, и с другого сайта (кука SameSite=None); на http — "
    "только с поддомена магазина. Safari не проверен.",
    en="The form_post trampoline (BETA): checked in Chromium. On a store on https the "
    "chain adds to the cart from a subdomain and from another site (the cookie is "
    "SameSite=None); on http, only from a subdomain of the store. Safari not checked.",
)
_INSALES_FORM: Mapping[str, Any] = {
    "strategy": "form_post",
    "mode": "single",
    "action": "{origin}/cart_items",
    "fields": [["variant_ids[{id}]", "{qty}"]],
    "secret": CART_SECRET,
}
_SHOP_SCRIPT_FORM: Mapping[str, Any] = {
    "strategy": "form_post",
    "mode": "chain",
    "action": "{origin}/cart/add/",
    "fields": [
        ["product_id", "{product_id}"],
        ["sku_id", "{variant_id}"],
        ["quantity", "{qty}"],
    ],
    "finish_url": "{origin}/cart/",
    "secret": CART_SECRET,
}


PRESETS: Mapping[Engine, Preset] = {
    Engine.SHOPIFY: Preset(
        engine=Engine.SHOPIFY,
        name="Shopify",
        cart={
            "strategy": "url_template",
            "url_template": "{origin}/cart/{items}?storefront=true",
            "item_template": "{id}:{qty}",
            "item_separator": ",",
        },
        adapter=None,
        notes=L10n(
            ru="Shopify публикует собственный MCP магазина: адаптер каталога не нужен, "
            "здесь только ссылка на корзину. id в ссылке — variant_id: [adapter.cart_ref] "
            "pattern берёт его из g:id вида shopify_RU_<товар>_<вариант>. Проверьте g:id "
            "своего фида: id другого вида уйдёт в ссылку как есть.",
            en="Shopify publishes its own store MCP: no catalog adapter, only the cart "
            "link here. The id in the link is the variant_id: [adapter.cart_ref] pattern "
            "takes it from a g:id like shopify_US_<product>_<variant>. Check the g:id of "
            "your feed: an id of another form goes to the link as it is.",
        ),
        edit=("adapter.cart_ref",),
        cart_ref={"pattern": SHOPIFY_FEED_ID},
    ),
    Engine.WOOCOMMERCE: Preset(
        engine=Engine.WOOCOMMERCE,
        name="WooCommerce",
        cart={
            "strategy": "link_list",
            "line_template": "{base}/cart/?add-to-cart={id}&quantity={qty}",
        },
        adapter={"kind": "woocommerce", "site": "{base}"},
        notes=L10n(
            ru="Каталог — Store API без ключей. Ссылки-действия работают на любой версии "
            "WooCommerce; на версии 10+ одна ссылка ведёт сразу в оформление (см. alt_cart).",
            en="Catalog: the keyless Store API. Add-to-cart links work on any WooCommerce "
            "version; on 10+ one link goes straight to checkout (see alt_cart).",
        ),
        alt_cart={
            "strategy": "url_template",
            "url_template": "{base}/checkout-link/?products={items}",
            "item_template": "{id}:{qty}",
            "item_separator": ",",
        },
        alt_cart_when=L10n(
            ru="Владелец подтвердил WooCommerce 10 или новее.",
            en="The owner confirmed WooCommerce 10 or later.",
        ),
    ),
    Engine.ECWID: Preset(
        engine=Engine.ECWID,
        name="Ecwid",
        cart={
            "strategy": "json_fragment",
            "url_template": "{origin}/#!/~/cart/create={payload}",
            "payload_encoding": "uri",
        },
        adapter=_feed("gmc"),
        notes=L10n(
            ru="Фид — Google Merchant из настроек Ecwid. Опции товара для корзины задаются "
            "в [adapter.cart_ref] options (формат фида Ecwid не проверен).",
            en="Feed: the Google Merchant feed from the Ecwid settings. Product options for "
            "the cart go to [adapter.cart_ref] options (Ecwid feed format not verified).",
        ),
        alt_cart={
            "strategy": "json_fragment",
            "url_template": "{origin}/!/~/cart/create={payload}",
            "payload_encoding": "base64",
        },
        alt_cart_when=L10n(ru="Магазин Ecwid на сайте Wix.", en="An Ecwid store on a Wix site."),
        edit=("adapter.feed",),
    ),
    Engine.INSALES: Preset(
        engine=Engine.INSALES,
        name="InSales",
        cart={"strategy": "link_list"},
        adapter=_feed("yml"),
        notes=L10n(
            ru="InSales кладёт товары в корзину только POST-ом: пока ссылки ведут на "
            "карточки. Трамплин form_post — бета: с другого сайта InSales заменяет "
            "корзину покупателя, размещайте сервер на поддомене магазина.",
            en="InSales adds to the cart by POST only: for now the links lead to product "
            "pages. The form_post trampoline is beta: from another site InSales replaces "
            "the customer's cart, host the server on a subdomain of the store.",
        ),
        cart_upgrade=L10n(
            ru="Сниппет в теме магазина: корзина по ссылке без трамплина и без замены.",
            en="A snippet in the store theme: a cart link without the trampoline or the "
            "replacement.",
        ),
        alt_cart=_INSALES_FORM,
        alt_cart_when=_FORM_POST_WHEN,
        replaces_cart=True,
        edit=("adapter.feed",),
    ),
    Engine.SHOP_SCRIPT: Preset(
        engine=Engine.SHOP_SCRIPT,
        name="Shop-Script",
        cart={"strategy": "link_list"},
        adapter=_feed("yml"),
        notes=L10n(
            ru="Shop-Script кладёт товары в корзину только POST-ом: пока ссылки ведут на "
            "карточки. Трамплин form_post (позиции по одной, магазин возвращает на "
            "трамплин) прошёл проверку в Chromium, но остаётся бетой: Safari и настоящий "
            "HTTPS-магазин не проверены.",
            en="Shop-Script adds to the cart by POST only: for now the links lead to "
            "product pages. The form_post trampoline (one line at a time, the store sends "
            "it back) passed the check in Chromium but stays beta: Safari and a real store "
            "on HTTPS are not checked.",
        ),
        cart_upgrade=L10n(
            ru="Плагин Webasyst (receiver): одна ссылка, без трамплина.",
            en="A Webasyst plugin (receiver): one link, no trampoline.",
        ),
        alt_cart=_SHOP_SCRIPT_FORM,
        alt_cart_when=_SHOP_SCRIPT_FORM_WHEN,
        edit=("adapter.feed", "adapter.cart_ref"),
    ),
    Engine.BITRIX: Preset(
        engine=Engine.BITRIX,
        name="1C-Bitrix",
        cart={
            "strategy": "link_list",
            "line_template": "{url}?action=ADD2BASKET&id={id}&quantity={qty}",
        },
        adapter=_feed("yml"),
        notes=L10n(
            ru="Ссылка-действие кладёт одну позицию; id — торговое предложение. На "
            "headless-витрине ссылки-действия не работают: уберите line_template.",
            en="An add-to-cart link puts one line; the id is the trade offer. On a "
            "headless storefront these links do not work: remove line_template.",
        ),
        cart_upgrade=L10n(
            ru="Модуль Битрикса (receiver): вся корзина одной ссылкой.",
            en="A Bitrix module (receiver): the whole cart in one link.",
        ),
        edit=("adapter.feed",),
    ),
    Engine.PRESTASHOP: Preset(
        engine=Engine.PRESTASHOP,
        name="PrestaShop",
        cart={
            "strategy": "link_list",
            "line_template": "{origin}/{cart_path}?add=1&id_product={product_id}"
            "&id_product_attribute={variant_id}&qty={qty}",
        },
        adapter=_feed("gmc"),
        notes=L10n(
            ru="Путь корзины зависит от языка витрины (cart, panier…). id фида модуля "
            "бывает вида 12-34: разберите его в [adapter.cart_ref] pattern.",
            en="The cart path depends on the storefront language (cart, panier…). A feed "
            "module may give ids like 12-34: split them with [adapter.cart_ref] pattern.",
        ),
        cart_upgrade=L10n(
            ru="Модуль PrestaShop (receiver): вся корзина одной ссылкой.",
            en="A PrestaShop module (receiver): the whole cart in one link.",
        ),
        edit=("adapter.feed", "adapter.cart_ref", "cart.line_template"),
    ),
    Engine.OPENCART: Preset(
        engine=Engine.OPENCART,
        name="OpenCart",
        cart={"strategy": "link_list"},
        adapter=_feed("yml"),
        notes=L10n(
            ru="Корзина OpenCart пополняется только POST-запросом с её страниц: здесь "
            "ссылки на карточки. YML модуля бывает в windows-1251.",
            en="An OpenCart cart takes only a POST from its own pages: product page links "
            "here. A module's YML may be in windows-1251.",
        ),
        cart_upgrade=L10n(
            ru="OCMOD-модуль (receiver): вся корзина одной ссылкой.",
            en="An OCMOD module (receiver): the whole cart in one link.",
        ),
        edit=("adapter.feed",),
    ),
    Engine.MAGENTO: Preset(
        engine=Engine.MAGENTO,
        name="Magento 2",
        cart={"strategy": "link_list"},
        adapter=_feed("gmc"),
        notes=L10n(
            ru="Добавление в корзину требует form_key: здесь ссылки на карточки.",
            en="Adding to the cart needs a form_key: product page links here.",
        ),
        cart_upgrade=L10n(
            ru="Модуль Magento (receiver): вся корзина одной ссылкой.",
            en="A Magento module (receiver): the whole cart in one link.",
        ),
        edit=("adapter.feed",),
    ),
    Engine.TILDA: Preset(
        engine=Engine.TILDA,
        name="Tilda",
        cart={"strategy": "link_list"},
        adapter=_feed("yml"),
        notes=L10n(
            ru="Корзина Тильды живёт в браузере: здесь ссылки на карточки. YML — "
            "/tstore/yml/<hash>.yml из настроек магазина.",
            en="A Tilda cart lives in the browser: product page links here. The YML is "
            "/tstore/yml/<hash>.yml from the store settings.",
        ),
        cart_upgrade=L10n(
            ru="Сниппет в блоке T123: корзина одной ссылкой {origin}/#mcpcart=… "
            "(url_template, без подписи).",
            en="A snippet in a T123 block: the cart in one link {origin}/#mcpcart=… "
            "(url_template, unsigned).",
        ),
        edit=("adapter.feed",),
    ),
}

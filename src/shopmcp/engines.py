"""Store engine presets: what `shopmcp scaffold` writes into `[cart]` and `[adapter]`.

Data only: detecting the engine lives in `detect.py`, rendering in the CLI. Strings may
carry scaffold-time placeholders, replaced before the config is written and never seen
at runtime: `{host}` (store host), `{feed}` (feed URL), `{cart_path}` (PrestaShop's
localized cart path). `${NAME}` is an environment variable, as anywhere in store.toml.
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


def _feed(fmt: str) -> dict[str, Any]:
    return {"kind": "feed", "format": fmt, "feed": "{feed}", "refresh_minutes": 60}


PRESETS: Mapping[Engine, Preset] = {
    Engine.SHOPIFY: Preset(
        engine=Engine.SHOPIFY,
        name="Shopify",
        cart={
            "strategy": "url_template",
            "url_template": "https://{host}/cart/{items}?storefront=true",
            "item_template": "{id}:{qty}",
            "item_separator": ",",
        },
        adapter=None,
        notes=L10n(
            ru="Shopify публикует собственный MCP магазина: адаптер каталога не нужен, "
            "здесь только ссылка на корзину. id в ссылке — variant_id.",
            en="Shopify publishes its own store MCP: no catalog adapter, only the cart "
            "link here. The id in the link is the variant_id.",
        ),
    ),
    Engine.WOOCOMMERCE: Preset(
        engine=Engine.WOOCOMMERCE,
        name="WooCommerce",
        cart={
            "strategy": "link_list",
            "line_template": "https://{host}/cart/?add-to-cart={id}&quantity={qty}",
        },
        adapter={"kind": "woocommerce"},
        notes=L10n(
            ru="Каталог — Store API без ключей. Ссылки-действия работают на любой версии "
            "WooCommerce; на версии 10+ одна ссылка ведёт сразу в оформление (см. alt_cart).",
            en="Catalog: the keyless Store API. Add-to-cart links work on any WooCommerce "
            "version; on 10+ one link goes straight to checkout (see alt_cart).",
        ),
        alt_cart={
            "strategy": "url_template",
            "url_template": "https://{host}/checkout-link/?products={items}",
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
            "url_template": "https://{host}/#!/~/cart/create={payload}",
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
            "url_template": "https://{host}/!/~/cart/create={payload}",
            "payload_encoding": "base64",
        },
        alt_cart_when=L10n(ru="Магазин Ecwid на сайте Wix.", en="An Ecwid store on a Wix site."),
        edit=("adapter.feed",),
    ),
    Engine.INSALES: Preset(
        engine=Engine.INSALES,
        name="InSales",
        cart={
            "strategy": "form_post",
            "mode": "single",
            "action": "https://{host}/cart_items",
            "fields": [["variant_ids[{id}]", "{qty}"]],
            "secret": CART_SECRET,
        },
        adapter=_feed("yml"),
        notes=L10n(
            ru="Корзину кладёт страница-трамплин одним POST. С другого сайта InSales "
            "заменяет корзину покупателя: размещайте сервер на поддомене магазина. "
            "Бета до живой проверки V1.",
            en="A trampoline page fills the cart with one POST. From another site InSales "
            "replaces the customer's cart: host the server on a subdomain of the store. "
            "Beta until live check V1.",
        ),
        cart_upgrade=L10n(
            ru="Сниппет в теме магазина: корзина по ссылке без трамплина и без замены.",
            en="A snippet in the store theme: a cart link without the trampoline or the "
            "replacement.",
        ),
        replaces_cart=True,
        edit=("adapter.feed", "cart.secret"),
    ),
    Engine.SHOP_SCRIPT: Preset(
        engine=Engine.SHOP_SCRIPT,
        name="Shop-Script",
        cart={
            "strategy": "form_post",
            "mode": "chain",
            "action": "https://{host}/cart/add/",
            "fields": [
                ["product_id", "{product_id}"],
                ["sku_id", "{variant_id}"],
                ["quantity", "{qty}"],
            ],
            "finish_url": "https://{host}/cart/",
            "secret": CART_SECRET,
        },
        adapter=_feed("yml"),
        notes=L10n(
            ru="Страница-трамплин отправляет позиции по одной, магазин возвращает на неё; "
            "в конце — корзина. Бета до живой проверки V1/V2.",
            en="A trampoline page posts the lines one by one, the store sends it back; "
            "the cart page comes last. Beta until live checks V1/V2.",
        ),
        cart_upgrade=L10n(
            ru="Плагин Webasyst (receiver): одна ссылка, без трамплина.",
            en="A Webasyst plugin (receiver): one link, no trampoline.",
        ),
        edit=("adapter.feed", "adapter.cart_ref", "cart.secret"),
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
            "line_template": "https://{host}/{cart_path}?add=1&id_product={product_id}"
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
            ru="Сниппет в блоке T123: корзина одной ссылкой https://{host}/#mcpcart=… "
            "(url_template, без подписи).",
            en="A snippet in a T123 block: the cart in one link https://{host}/#mcpcart=… "
            "(url_template, unsigned).",
        ),
        edit=("adapter.feed",),
    ),
}

"""Cart handoff: the agent builds a list, the store gets a link, the human pays on the site.

A strategy is named by who writes the cart: the store's own GET handler (`url_template`,
`json_fragment`), our module on the store site (`receiver`), our trampoline page posting
to the store (`form_post`), the store API (`share_link`), or the human (`link_list`).
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from importlib import import_module
from typing import Literal, Protocol
from urllib.parse import quote

from pydantic import HttpUrl

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.cart.refs import CartRef
from shopmcp.config import CartConfig, ConfigError, UrlTemplateCart
from shopmcp.domain.models import CartHandoff, CartLine, CartLinkItem, Product
from shopmcp.i18n import Texts, define

CartKind = Literal[
    "share_link", "url_template", "json_fragment", "form_post", "receiver", "link_list"
]

MAX_URL_LENGTH = 2000  # longer cart URLs are cut by browsers and proxies

_URL_TOO_LONG = define(
    "cart.note.url_too_long",
    ru="Корзина слишком большая для одной ссылки: добавьте товары по ссылкам на карточки.",
    en="The cart is too large for one link: add the products from their page links.",
)


class CartStrategy(Protocol):
    kind: CartKind

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],  # Product.id -> cart key, from adapter.cart_refs()
    ) -> CartHandoff: ...


def _links(lines: Sequence[CartLine], products: Mapping[str, Product]) -> list[CartLinkItem]:
    return [
        CartLinkItem(
            product_id=line.product_id,
            name=products[line.product_id].name,
            quantity=line.quantity,
            url=products[line.product_id].url,
        )
        for line in lines
    ]


def _qty(quantity: Decimal) -> str:
    return format(quantity.normalize(), "f")  # Decimal("2.000") -> "2", not "2.000" or "2E+0"


class LinkListStrategy:
    """Product page links with quantities; works for any store."""

    kind: CartKind = "link_list"

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        return CartHandoff(kind=self.kind, links=_links(lines, products))


class UrlTemplateStrategy:
    """One URL that fills the cart, e.g. `https://shop.ru/cart/add?id[]=1&qty[]=2`.

    Placeholders are substituted with `str.replace`, not `str.format`, so a template may
    contain other braces. Over `MAX_URL_LENGTH` it degrades to a link list with a note.
    """

    kind: CartKind = "url_template"

    def __init__(self, cfg: UrlTemplateCart, texts: Texts) -> None:
        # Both carry their placeholders: UrlTemplateCart validates them.
        self._url = cfg.url_template
        self._item = cfg.item_template
        self._sep = cfg.item_separator
        self._texts = texts

    def url(self, lines: Sequence[CartLine]) -> str:
        items = self._sep.join(
            self._item.replace("{id}", quote(line.product_id, safe="")).replace(
                "{qty}", _qty(line.quantity)
            )
            for line in lines
        )
        return self._url.replace("{items}", items)

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        links = _links(lines, products)
        url = self.url(lines)
        if len(url) > MAX_URL_LENGTH:
            note = self._texts.get("cart.note.url_too_long", _URL_TOO_LONG)
            return CartHandoff(kind="link_list", links=links, note=note)
        return CartHandoff(kind=self.kind, url=url, links=links)


class ShareLinkStrategy:
    """The store creates a shared cart on its side and returns its link."""

    kind: CartKind = "share_link"

    def __init__(self, adapter: ShopAdapter) -> None:
        self._adapter = adapter

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        url = await self._adapter.create_share_link(lines)
        return CartHandoff(kind=self.kind, url=url, links=_links(lines, products))


def create(
    cfg: CartConfig,
    adapter: ShopAdapter,
    texts: Texts,
    *,
    site: HttpUrl,
    public_url: HttpUrl,
    store_id: str,
) -> CartStrategy:
    """The configured strategy. `site` is the store website, `public_url` this server's
    MCP endpoint (the trampoline lives on its origin), `store_id` goes into signed tokens."""
    if cfg.strategy == "share_link":
        if Capability.SHARE_LINK not in adapter.capabilities():
            raise ConfigError("cart.strategy share_link: the adapter has no share_link capability")
        return ShareLinkStrategy(adapter)
    if cfg.strategy == "url_template":
        return UrlTemplateStrategy(cfg, texts)
    if cfg.strategy == "form_post":
        # Imported only here: a store without the trampoline does not depend on it, and
        # removing the module breaks only the stores that use it (as adapters/registry).
        module = "shopmcp.cart.form_post"
        try:
            factory = import_module(module).create
        except (ImportError, AttributeError) as e:
            raise ConfigError(
                f"cart.strategy form_post: {module}.create is unavailable: {e}"
            ) from e
        strategy: CartStrategy = factory(
            cfg, texts, site=site, public_url=public_url, store_id=store_id
        )
        return strategy
    if cfg.strategy in ("json_fragment", "receiver"):
        raise ConfigError(f"cart.strategy {cfg.strategy}: not implemented in this version")
    return LinkListStrategy()

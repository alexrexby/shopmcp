"""Cart handoff: the agent builds a list, the store gets a link, the human pays on the site."""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Literal, Protocol
from urllib.parse import quote

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.config import CartConfig, ConfigError
from shopmcp.domain.models import CartHandoff, CartLine, CartLinkItem, Product
from shopmcp.i18n import Texts, define

CartKind = Literal["share_link", "url_template", "link_list"]

MAX_URL_LENGTH = 2000  # longer cart URLs are cut by browsers and proxies

_URL_TOO_LONG = define(
    "cart.note.url_too_long",
    ru="Корзина слишком большая для одной ссылки: добавьте товары по ссылкам на карточки.",
    en="The cart is too large for one link: add the products from their page links.",
)


class CartStrategy(Protocol):
    kind: CartKind

    async def handoff(
        self, lines: Sequence[CartLine], products: Mapping[str, Product]
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
        self, lines: Sequence[CartLine], products: Mapping[str, Product]
    ) -> CartHandoff:
        return CartHandoff(kind=self.kind, links=_links(lines, products))


class UrlTemplateStrategy:
    """One URL that fills the cart, e.g. `https://shop.ru/cart/add?id[]=1&qty[]=2`.

    Placeholders are substituted with `str.replace`, not `str.format`, so a template may
    contain other braces. Over `MAX_URL_LENGTH` it degrades to a link list with a note.
    """

    kind: CartKind = "url_template"

    def __init__(self, cfg: CartConfig, texts: Texts) -> None:
        # Both are present with their placeholders: CartConfig validates url_template carts.
        self._url = cfg.url_template or ""
        self._item = cfg.item_template or ""
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
        self, lines: Sequence[CartLine], products: Mapping[str, Product]
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
        self, lines: Sequence[CartLine], products: Mapping[str, Product]
    ) -> CartHandoff:
        url = await self._adapter.create_share_link(lines)
        return CartHandoff(kind=self.kind, url=url, links=_links(lines, products))


def create(cfg: CartConfig, adapter: ShopAdapter, texts: Texts) -> CartStrategy:
    if cfg.strategy == "share_link":
        if Capability.SHARE_LINK not in adapter.capabilities():
            raise ConfigError("cart.strategy share_link: the adapter has no share_link capability")
        return ShareLinkStrategy(adapter)
    if cfg.strategy == "url_template":
        return UrlTemplateStrategy(cfg, texts)
    return LinkListStrategy()

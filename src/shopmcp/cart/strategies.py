"""Cart handoff: the agent builds a list, the store gets a link, the human pays on the site.

A strategy is named by who writes the cart: the store's own GET handler (`url_template`,
`json_fragment`), our module on the store site (`receiver`), our trampoline page posting
to the store (`form_post`), the store API (`share_link`), or the human (`link_list`).

Templates share one set of placeholders, substituted in one pass (a value is never read as
a placeholder again; other braces stay): `{id}` (variant or product), `{product_id}`,
`{variant_id}` (empty without a variant), `{qty}`, `{url}` (product page), `{items}`,
`{payload}`, `{token}`. Every other value is percent-encoded whole, so it is safe in a URL
path, query or fragment alike; `{url}` goes as is where it starts the URL and percent-encoded
in a query value (`ret={url}`). In a `line_template` (one line per link) a query parameter
whose placeholder rendered empty is left out (`id_product_attribute=` of a product without
combinations), as form_post leaves out an empty field. An `item_template` keeps it: lines
are joined into one query, and `variant[]=` holds the place of a line in parallel arrays.
"""

import base64
import json
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import HttpUrl

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.cart.refs import CartRef
from shopmcp.cart.token import TokenError, qty_text, sign
from shopmcp.config import (
    CartConfig,
    ConfigError,
    JsonFragmentCart,
    ReceiverCart,
    UrlTemplateCart,
)
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

_PLACEHOLDER = re.compile(r"\{(id|product_id|variant_id|qty|url|items|payload|token)\}")


class CartStrategy(Protocol):
    kind: CartKind

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],  # Product.id -> cart key, from adapter.cart_refs()
    ) -> CartHandoff: ...


def _values(line: CartLine, product: Product, ref: CartRef) -> dict[str, str]:
    return {
        "id": quote(ref.id, safe=""),
        "product_id": quote(ref.product_id, safe=""),
        "variant_id": quote(ref.variant_id or "", safe=""),
        "qty": qty_text(line.quantity),
        "url": str(product.url),  # already a valid URL: HttpUrl normalized it
    }


def _fill(template: str, values: Mapping[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m[1], m[0]), template)


def _fill_query(template: str, values: Mapping[str, str], *, drop_empty: bool) -> str:
    """A query from a template; `drop_empty` leaves out a parameter whose placeholder
    rendered empty. `{url}` is a value here, not the start of the URL: percent-encoded."""
    if "url" in values:
        values = {**values, "url": quote(values["url"], safe="")}
    params = []
    for param in template.split("&"):
        name, eq, value = param.partition("=")
        filled = _fill(value, values)
        if drop_empty and eq and value and not filled:  # the placeholder had nothing to say
            continue
        params.append(_fill(name, values) + eq + filled)
    return "&".join(params)


def _fill_url(template: str, values: Mapping[str, str], *, drop_empty: bool = False) -> str:
    """A URL from a template. Its query is appended to the one `{url}` may already have:
    `{url}?action=ADD2BASKET` on `https://shop.ru/p/1/?color=red` gives `...?color=red&action=`."""
    query_at, fragment_at = template.find("?"), template.find("#")
    if query_at < 0 or 0 <= fragment_at < query_at:  # no query, or a `?` inside the fragment
        return _fill(template, values)
    end = fragment_at if fragment_at > query_at else len(template)
    parts = urlsplit(_fill(template[:query_at], values))
    filled = _fill_query(template[query_at + 1 : end], values, drop_empty=drop_empty)
    query = "&".join(q for q in (parts.query, filled) if q)
    fragment = _fill(template[end + 1 :], values) if end < len(template) else parts.fragment
    return urlunsplit(parts._replace(query=query, fragment=fragment))


def _link(line: CartLine, product: Product, add_url: str | None = None) -> CartLinkItem:
    return CartLinkItem(
        product_id=line.product_id,
        name=product.name,
        quantity=line.quantity,
        url=product.url,
        add_url=add_url,
    )


def _links(lines: Sequence[CartLine], products: Mapping[str, Product]) -> list[CartLinkItem]:
    return [_link(line, products[line.product_id]) for line in lines]


def _too_long(texts: Texts, links: list[CartLinkItem]) -> CartHandoff:
    note = texts.get("cart.note.url_too_long", _URL_TOO_LONG)
    return CartHandoff(kind="link_list", links=links, note=note)


class LinkListStrategy:
    """Product page links with quantities; works for any store. With a `line_template`
    every line also gets `add_url`, a link that puts that one line into the cart."""

    kind: CartKind = "link_list"

    def __init__(self, line_template: str | None = None) -> None:
        self._line = line_template

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        if self._line is None:
            return CartHandoff(kind=self.kind, links=_links(lines, products))
        links = []
        for line in lines:
            product = products[line.product_id]
            values = _values(line, product, refs[line.product_id])
            add_url = _fill_url(self._line, values, drop_empty=True)
            links.append(_link(line, product, add_url))
        return CartHandoff(kind=self.kind, links=links)


class UrlTemplateStrategy:
    """One URL that fills the cart, e.g. `https://shop.ru/cart/add?id[]=1&qty[]=2`.

    Over `MAX_URL_LENGTH` it degrades to a link list with a note.
    """

    kind: CartKind = "url_template"

    def __init__(self, cfg: UrlTemplateCart, texts: Texts) -> None:
        # Both carry their placeholders: UrlTemplateCart validates them.
        self._url = cfg.url_template
        self._item = cfg.item_template
        self._sep = cfg.item_separator
        self._texts = texts

    def url(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> str:
        # Empty parameters stay: the store pairs `product[]` and `variant[]` by position.
        items = self._sep.join(
            _fill_query(
                self._item,
                _values(line, products[line.product_id], refs[line.product_id]),
                drop_empty=False,
            )
            for line in lines
        )
        return _fill_url(self._url, {"items": items})

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        links = _links(lines, products)
        url = self.url(lines, products, refs)
        if len(url) > MAX_URL_LENGTH:
            return _too_long(self._texts, links)
        return CartHandoff(kind=self.kind, url=url, links=links)


def _json_id(value: str) -> int | str:
    # Ecwid ids are numbers in its JSON; "007" stays a string, int() would lose the zeros.
    return int(value) if value.isascii() and value.isdigit() and str(int(value)) == value else value


def _json_qty(quantity: Decimal) -> int | float:
    return int(quantity) if quantity == quantity.to_integral_value() else float(quantity)


class JsonFragmentStrategy:
    """The cart as JSON in the URL, read by the storefront script (Ecwid):
    `https://shop.ru/#!/~/cart/create=%7B%22products%22%3A...`.

    Ecwid keys a line by the product id and option names, not by a variant id, so `{id}`
    is not used: `product_id` and `options` of the CartRef are. Over `MAX_URL_LENGTH` it
    degrades to a link list with a note.
    """

    kind: CartKind = "json_fragment"

    def __init__(self, cfg: JsonFragmentCart, texts: Texts) -> None:
        self._url = cfg.url_template
        self._encoding = cfg.payload_encoding
        self._texts = texts

    def payload(self, lines: Sequence[CartLine], refs: Mapping[str, CartRef]) -> str:
        items = []
        for line in lines:
            ref = refs[line.product_id]
            item: dict[str, Any] = {
                "id": _json_id(ref.product_id),
                "quantity": _json_qty(line.quantity),
            }
            if ref.options:
                item["options"] = dict(ref.options)
            items.append(item)
        # ASCII JSON: base64 is decoded by the browser's atob, which knows only Latin-1;
        # `\uXXXX` escapes pass it intact and JSON.parse restores the text.
        text = json.dumps({"products": items}, ensure_ascii=True, separators=(",", ":"))
        if self._encoding == "base64":
            # Ecwid docs (read 15.10.2026), Wix sites: encodeURIComponent(btoa(json)), that is
            # the standard alphabet with padding, then percent-encoded.
            text = base64.b64encode(text.encode("ascii")).decode("ascii")
        return quote(text, safe="")

    def url(self, lines: Sequence[CartLine], refs: Mapping[str, CartRef]) -> str:
        return _fill_url(self._url, {"payload": self.payload(lines, refs)})

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        links = _links(lines, products)
        url = self.url(lines, refs)
        if len(url) > MAX_URL_LENGTH:
            return _too_long(self._texts, links)
        return CartHandoff(kind=self.kind, url=url, links=links)


class ReceiverStrategy:
    """One link to our module on the store site, e.g. `https://shop.ru/mcp/cart/?mcp_cart=…`.

    The signed token (cart/token.py) carries the lines; the module checks it and adds them
    to the customer's cart. A token or URL too long degrades to a link list with a note.
    """

    kind: CartKind = "receiver"

    def __init__(self, cfg: ReceiverCart, texts: Texts, *, store_id: str) -> None:
        self._url = cfg.url_template
        self._secret = cfg.secret.get_secret_value().encode()
        self._kid = cfg.key_id
        self._ttl = cfg.ttl_minutes * 60
        self._store = store_id
        self._texts = texts

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        links = _links(lines, products)
        now = int(time.time())  # passed to sign: expires_at must be the token's exp
        signed = [(refs[line.product_id], line.quantity) for line in lines]
        try:
            token = sign(
                signed,
                store=self._store,
                secret=self._secret,
                kid=self._kid,
                ttl=self._ttl,
                now=now,
            )
        except TokenError:
            return _too_long(self._texts, links)
        url = _fill_url(self._url, {"token": quote(token, safe="")})
        if len(url) > MAX_URL_LENGTH:
            return _too_long(self._texts, links)
        expires_at = datetime.fromtimestamp(now + self._ttl, UTC)
        return CartHandoff(kind=self.kind, url=url, links=links, expires_at=expires_at)


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
    if cfg.strategy == "json_fragment":
        return JsonFragmentStrategy(cfg, texts)
    if cfg.strategy == "receiver":
        return ReceiverStrategy(cfg, texts, store_id=store_id)
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
    return LinkListStrategy(cfg.line_template)

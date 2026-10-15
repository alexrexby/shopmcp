"""form_post: the cart link opens our trampoline page, which POSTs the lines to the store.

For engines that take cart lines only by POST (InSales `/cart_items`, Shop-Script
`/cart/add/`). The link is `{public_url origin}/cart/h/{token}`: the signed token
(cart/token.py) carries the lines, server/trampoline.py turns them into forms. `single`
sends one form with every line; `chain` sends one form per line and relies on the store
redirecting back to the page (Referer) for the next one.

Field values go into an HTML form, not a URL: the browser encodes them, so they are filled
raw here (not with `strategies._values`, which percent-encodes). An id inside a field name
(`variant_ids[{id}]`) must not carry `[` or `]`: Rails and PHP would read a nested
structure. Such a cart degrades to product page links, as an overlong one does.
"""

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import HttpUrl

from shopmcp.cart.refs import CartRef
from shopmcp.cart.strategies import (
    _PLACEHOLDER,
    MAX_URL_LENGTH,
    CartKind,
    _fill,
    _links,
    _too_long,
)
from shopmcp.cart.token import TokenError, qty_text, sign
from shopmcp.config import ConfigError, FormPostCart
from shopmcp.domain.models import CartHandoff, CartLine, Product
from shopmcp.i18n import Texts, define

PATH = "/cart/h/"  # the trampoline route is PATH + "{token}"
KEY_ID = "1"  # FormPostCart has no key_id: one secret per store, rotation is a new secret

# What a token line knows; `{url}`, `{items}` and the rest have nothing to fill in a form.
FIELD_PLACEHOLDERS = frozenset({"id", "product_id", "variant_id", "qty"})
_NAME_BREAKERS = ("[", "]")

_UNSAFE_ID = define(
    "cart.note.form_unsafe_id",
    ru="Эти товары не получится передать в корзину одной ссылкой: добавьте их по ссылкам "
    "на карточки.",
    en="These products cannot go into the cart by one link: add them from their page links.",
)

Form = list[tuple[str, str]]


def form(fields: Sequence[tuple[str, str]], ref: CartRef, quantity: Decimal) -> Form | None:
    """The (name, value) pairs of one line; a field rendered empty is left out. None when
    an id would land in a field name with `[` or `]` and change its structure."""
    values = {
        "id": ref.id,
        "product_id": ref.product_id,
        "variant_id": ref.variant_id or "",
        "qty": qty_text(quantity),
    }
    pairs = []
    for name, value in fields:
        used = _PLACEHOLDER.findall(name)
        if any(c in values[p] for p in used if p in values for c in _NAME_BREAKERS):
            return None
        filled_name, filled_value = _fill(name, values), _fill(value, values)
        if filled_name and filled_value:
            pairs.append((filled_name, filled_value))
    return pairs


def forms(cfg: FormPostCart, lines: Sequence[tuple[CartRef, Decimal]]) -> list[Form] | None:
    """One form per line for `chain`, one form with every line for `single`."""
    per_line = [form(cfg.fields, ref, quantity) for ref, quantity in lines]
    if any(f is None for f in per_line):
        return None
    done = [f for f in per_line if f is not None]
    return done if cfg.mode == "chain" else [[pair for f in done for pair in f]]


def origin(url: HttpUrl | str) -> str:
    parts = urlsplit(str(url))
    return f"{parts.scheme}://{parts.netloc}"


class FormPostStrategy:
    """One link to the trampoline on this server's origin; it expires with the token."""

    kind: CartKind = "form_post"

    def __init__(self, cfg: FormPostCart, texts: Texts, *, public_url: HttpUrl, store_id: str):
        self._cfg = cfg
        self._secret = cfg.secret.get_secret_value().encode()
        self._ttl = cfg.ttl_minutes * 60
        self._base = origin(public_url) + PATH
        self._store = store_id
        self._texts = texts

    async def handoff(
        self,
        lines: Sequence[CartLine],
        products: Mapping[str, Product],
        refs: Mapping[str, CartRef],
    ) -> CartHandoff:
        links = _links(lines, products)
        signed = [(refs[line.product_id], line.quantity) for line in lines]
        if forms(self._cfg, signed) is None:
            note = self._texts.get("cart.note.form_unsafe_id", _UNSAFE_ID)
            return CartHandoff(kind="link_list", links=links, note=note)
        now = int(time.time())  # passed to sign: expires_at must be the token's exp
        try:
            token = sign(
                signed, store=self._store, secret=self._secret, kid=KEY_ID, ttl=self._ttl, now=now
            )
        except TokenError:
            return _too_long(self._texts, links)
        url = self._base + token  # base64url and `.`: nothing to encode
        if len(url) > MAX_URL_LENGTH:
            return _too_long(self._texts, links)
        expires_at = datetime.fromtimestamp(now + self._ttl, UTC)
        return CartHandoff(kind=self.kind, url=url, links=links, expires_at=expires_at)


def create(
    cfg: FormPostCart, texts: Texts, *, site: HttpUrl, public_url: HttpUrl, store_id: str
) -> FormPostStrategy:
    """Called by strategies.create; `site` is unused: action and finish_url are already
    checked to stay on it (config.py)."""
    unknown = sorted(
        {p for pair in cfg.fields for p in _PLACEHOLDER.findall("".join(pair))} - FIELD_PLACEHOLDERS
    )
    if unknown:
        raise ConfigError(
            f"cart.fields: {unknown} cannot be filled in a form, use {sorted(FIELD_PLACEHOLDERS)}"
        )
    return FormPostStrategy(cfg, texts, public_url=public_url, store_id=store_id)


def secrets(cfg: FormPostCart) -> Mapping[str, bytes]:
    """kid -> key, for token.verify on the trampoline."""
    return {KEY_ID: cfg.secret.get_secret_value().encode()}

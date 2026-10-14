"""Signed cart token: the whole handoff state lives in the link, the server stores nothing.

`<b64url(payload_json)>.<b64url(HMAC-SHA256(secret, b64url(payload_json)))>`, no padding.
Not a JWT on purpose: a store module verifies it with `hash_hmac` + `hash_equals` in a few
lines, without a library and without an algorithm to choose. The contract for those
modules, with test vectors, is docs/en/cart-receiver.md; tests/unit/test_cart_token.py
pins the same vectors.

The signature gives integrity, expiry and binding to one store. It does not stop anyone
from handing a valid cart to someone else: the receiver checks `Sec-Fetch-Dest` for that.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets as _random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from shopmcp.cart.refs import CartRef

VERSION = 1
MAX_LENGTH = 1800  # characters: the token goes into a URL next to the store's own path
CLOCK_SKEW = 60  # seconds the store's clock may differ from ours
JTI_BYTES = 16

Line = tuple[CartRef, Decimal]


class TokenError(ValueError):
    """The token is not acceptable; `str()` says why, without the token itself."""


@dataclass(frozen=True)
class Payload:
    kid: str
    store: str
    iat: int
    exp: int
    jti: str
    lines: list[Line]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as e:
        raise TokenError("malformed token") from e


def _mac(secret: bytes, body: str) -> str:
    return b64url(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())


def _qty(quantity: Decimal) -> str:
    return format(quantity.normalize(), "f")  # Decimal("2.000") -> "2", never "2E+0"


def sign(
    lines: Sequence[Line],
    *,
    store: str,
    secret: bytes,
    kid: str,
    ttl: int,
    now: int | None = None,
    jti: str | None = None,
) -> str:
    """Token for `lines`, valid `ttl` seconds. `now` and `jti` are fixed only by tests.

    Raises TokenError when the token would exceed MAX_LENGTH: the caller degrades to links.
    """
    iat = int(time.time()) if now is None else now
    payload = {
        "v": VERSION,
        "kid": kid,
        "s": store,
        "iat": iat,
        "exp": iat + ttl,
        "jti": jti or b64url(_random.token_bytes(JTI_BYTES)),
        "l": [[ref.product_id, ref.variant_id, _qty(q), dict(ref.options)] for ref, q in lines],
    }
    body = b64url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    token = f"{body}.{_mac(secret, body)}"
    if len(token) > MAX_LENGTH:
        raise TokenError(f"token is {len(token)} characters, the limit is {MAX_LENGTH}")
    return token


def verify(
    token: str, *, store: str, secrets: Mapping[str, bytes], now: int | None = None
) -> Payload:
    """The payload of a token signed by one of `secrets` (kid -> key) for `store`."""
    if len(token) > MAX_LENGTH:
        raise TokenError("token too long")
    body, dot, mac = token.partition(".")
    if not dot or not body or not mac:
        raise TokenError("malformed token")
    try:
        data = json.loads(_unb64url(body))
    except (UnicodeDecodeError, ValueError) as e:
        raise TokenError("malformed token") from e
    if not isinstance(data, dict):
        raise TokenError("malformed token")
    # kid is read before the signature is checked: it only picks the key to check with.
    secret = secrets.get(data.get("kid")) if isinstance(data.get("kid"), str) else None
    if secret is None:
        raise TokenError("unknown key id")
    if not hmac.compare_digest(_mac(secret, body), mac):
        raise TokenError("bad signature")
    return _payload(data, store, int(time.time()) if now is None else now)


def _payload(data: dict[str, Any], store: str, now: int) -> Payload:
    if data.get("v") != VERSION:
        raise TokenError("unsupported version")
    if data.get("s") != store:
        raise TokenError("token of another store")
    iat, exp, jti = data.get("iat"), data.get("exp"), data.get("jti")
    if not _is_int(exp) or not _is_int(iat) or not isinstance(jti, str) or not jti:
        raise TokenError("malformed token")
    if now > exp + CLOCK_SKEW:
        raise TokenError("token expired")
    if iat > now + CLOCK_SKEW:
        raise TokenError("token issued in the future")
    return Payload(
        kid=data["kid"], store=store, iat=iat, exp=exp, jti=jti, lines=_lines(data.get("l"))
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _lines(raw: Any) -> list[Line]:
    if not isinstance(raw, list) or not raw:
        raise TokenError("malformed lines")
    lines = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 4 or not isinstance(item[2], str):
            raise TokenError("malformed lines")
        product_id, variant_id, qty, options = item
        try:
            ref = CartRef(product_id=product_id, variant_id=variant_id, options=options)
            quantity = Decimal(qty)
        except (ValidationError, InvalidOperation) as e:
            raise TokenError("malformed lines") from e
        if not quantity.is_finite() or quantity <= 0:
            raise TokenError("malformed lines")
        lines.append((ref, quantity))
    return lines

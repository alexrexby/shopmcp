"""Cart token: the test vectors published for store modules in docs/*/cart-receiver.md.

The expected token below was cross-checked outside this module with
`openssl dgst -sha256 -hmac <secret>` over the payload part; a store module written in any
language must reproduce it byte for byte.
"""

import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from shopmcp.cart.refs import CartRef
from shopmcp.cart.token import MAX_LENGTH, TokenError, _unb64url, b64url, sign, verify

SECRET = b"test-secret-0123456789-abcdefghij"
STORE = "demo"
IAT = 1760400000
EXP = IAT + 86400
JTI = "AAECAwQFBgcICQoLDA0ODw"
LINES = [
    (CartRef(product_id="1001", variant_id="2002"), Decimal(2)),
    (CartRef(product_id="77", options={"Размер": "M"}), Decimal("1.50")),
]
PAYLOAD = (
    '{"v":1,"kid":"1","s":"demo","iat":1760400000,"exp":1760486400,'
    '"jti":"AAECAwQFBgcICQoLDA0ODw",'
    '"l":[["1001","2002","2",{}],["77",null,"1.5",{"Размер":"M"}]]}'
)
TOKEN = (
    "eyJ2IjoxLCJraWQiOiIxIiwicyI6ImRlbW8iLCJpYXQiOjE3NjA0MDAwMDAsImV4cCI6MTc2MDQ4NjQwMCwianRp"
    "IjoiQUFFQ0F3UUZCZ2NJQ1FvTERBME9EdyIsImwiOltbIjEwMDEiLCIyMDAyIiwiMiIse31dLFsiNzciLG51bGws"
    "IjEuNSIseyLQoNCw0LfQvNC10YAiOiJNIn1dXX0"
    ".QJCt2zh7AIss7pR9RmjTB5Nbbx2TGsdvO0NsNlwDR5Q"
)
KEYS = {"1": SECRET}


def test_vector_sign():
    token = sign(LINES, store=STORE, secret=SECRET, kid="1", ttl=86400, now=IAT, jti=JTI)
    assert token == TOKEN
    assert _unb64url(token.split(".")[0]).decode() == PAYLOAD


def test_vector_verify():
    p = verify(TOKEN, store=STORE, secrets=KEYS, now=IAT + 100)
    assert (p.kid, p.store, p.iat, p.exp, p.jti) == ("1", STORE, IAT, EXP, JTI)
    assert p.lines == [(ref, q.normalize()) for ref, q in LINES]
    assert p.lines[0][0].id == "2002" and p.lines[1][0].id == "77"


def test_vector_expired():
    verify(TOKEN, store=STORE, secrets=KEYS, now=EXP + 60)  # within the clock skew
    with pytest.raises(TokenError, match="expired"):
        verify(TOKEN, store=STORE, secrets=KEYS, now=EXP + 61)


def test_vector_another_store():
    with pytest.raises(TokenError, match="another store"):
        verify(TOKEN, store="other", secrets=KEYS, now=IAT)


@pytest.mark.parametrize(
    "token",
    [
        TOKEN[:-1] + ("A" if TOKEN[-1] != "A" else "B"),  # signature byte changed
        # payload changed (a line's quantity), the old signature kept
        b64url(PAYLOAD.replace('"2"', '"9"', 1).encode()) + "." + TOKEN.split(".")[1],
    ],
)
def test_vector_tampered(token):
    with pytest.raises(TokenError, match="bad signature"):
        verify(token, store=STORE, secrets=KEYS, now=IAT)


def test_wrong_secret_and_unknown_kid():
    with pytest.raises(TokenError, match="bad signature"):
        verify(TOKEN, store=STORE, secrets={"1": SECRET + b"x"}, now=IAT)
    with pytest.raises(TokenError, match="unknown key id"):
        verify(TOKEN, store=STORE, secrets={"2": SECRET}, now=IAT)


def test_key_rotation_by_kid():
    old = sign(LINES, store=STORE, secret=SECRET, kid="1", ttl=60, now=IAT)
    new = sign(LINES, store=STORE, secret=b"n" * 32, kid="2", ttl=60, now=IAT)
    keys = {"1": SECRET, "2": b"n" * 32}
    assert verify(old, store=STORE, secrets=keys, now=IAT).kid == "1"
    assert verify(new, store=STORE, secrets=keys, now=IAT).kid == "2"


def test_jti_is_random_and_unpadded():
    a = sign(LINES, store=STORE, secret=SECRET, kid="1", ttl=60)
    b = sign(LINES, store=STORE, secret=SECRET, kid="1", ttl=60)
    assert a != b and "=" not in a
    jti = json.loads(_unb64url(a.split(".")[0]))["jti"]
    assert len(_unb64url(jti)) == 16


def test_too_long_is_refused_both_ways():
    lines = [(CartRef(product_id=f"{i:0128d}"), Decimal(1)) for i in range(20)]
    with pytest.raises(TokenError, match="limit"):
        sign(lines, store=STORE, secret=SECRET, kid="1", ttl=60)
    with pytest.raises(TokenError, match="too long"):
        verify("a" * (MAX_LENGTH + 1), store=STORE, secrets=KEYS)


def _signed(payload: dict) -> str:
    body = b64url(json.dumps(payload).encode())
    return f"{body}.{b64url(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())}"


BASE = {"v": 1, "kid": "1", "s": STORE, "iat": IAT, "exp": EXP, "jti": JTI}


@pytest.mark.parametrize(
    "change, error",
    [
        ({"v": 2}, "version"),
        ({"exp": None}, "malformed"),
        ({"exp": "1760486400"}, "malformed"),
        ({"iat": IAT + 3600}, "future"),
        ({"l": []}, "lines"),
        ({"l": [["<script>", None, "1", {}]]}, "lines"),  # outside the id alphabet
        ({"l": [["1", None, "0", {}]]}, "lines"),
        ({"l": [["1", None, "NaN", {}]]}, "lines"),
        ({"l": [["1", None, 1, {}]]}, "lines"),  # quantity is a decimal string
    ],
)
def test_signed_but_invalid_payload(change, error):
    payload = {**BASE, "l": [["1", None, "1", {}]], **change}
    if payload.get("exp") is None:
        del payload["exp"]
    with pytest.raises(TokenError, match=error):
        verify(_signed(payload), store=STORE, secrets=KEYS, now=IAT)


@pytest.mark.parametrize("junk", ["", ".", "abc", "abc.", ".abc", "!!!.abc", "e30.abc"])
def test_junk_is_token_error(junk):
    with pytest.raises(TokenError):
        verify(junk, store=STORE, secrets=KEYS, now=IAT)

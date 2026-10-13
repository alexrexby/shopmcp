"""Stage 1C: JWKS token verification, the per-tool scope gate and RFC 9728 metadata.

A real MCPServer runs behind `wrap_with_auth`; the JWKS is served by a local Starlette
app on a real socket, because PyJWKClient fetches it with urllib.
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import jwt
import pydantic_core
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from shopmcp.config import StoreConfig
from shopmcp.server import auth
from shopmcp.server.auth import JwksTokenVerifier, ToolScopeGate, wrap_with_auth

PUBLIC_URL = "http://localhost:8000/mcp"
METADATA_URL = "http://localhost:8000/.well-known/oauth-protected-resource/mcp"
ISSUER = "https://id.demo.example"
PERSONAL, PUBLIC = "demo_orders_history", "demo_products_search"
ACCEPT = {"accept": "application/json, text/event-stream"}
MODERN = "2026-07-28"  # protocol version served by the SDK's second (json.loads) transport

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROTATED = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FOREIGN = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**data, "kid": kid, "use": "sig", "alg": "RS256"}


def mint(key: Any = KEY, kid: str = "k1", alg: str = "RS256", drop: tuple[str, ...] = (), **claims):
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": PUBLIC_URL,
        "sub": "user-42",
        "client_id": "agent",
        "iat": now,
        "exp": now + 300,
        "scope": "orders.read",
    }
    payload.update(claims)
    for name in drop:
        payload.pop(name)
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


# --- local JWKS endpoint ------------------------------------------------------


class JwksState:
    def __init__(self, url: str) -> None:
        self.url = url
        self.keys: list[dict[str, Any]] = []
        self.fetches = 0
        self.delay = 0.0


@pytest.fixture(scope="module")
def jwks_server() -> Iterator[JwksState]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    state = JwksState(f"http://127.0.0.1:{sock.getsockname()[1]}/jwks")

    async def endpoint(request: Request) -> JSONResponse:
        state.fetches += 1
        if state.delay:
            await anyio.sleep(state.delay)
        return JSONResponse({"keys": state.keys})

    server = uvicorn.Server(
        uvicorn.Config(Starlette(routes=[Route("/jwks", endpoint)]), log_level="warning")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield state
    server.should_exit = True
    thread.join()


@pytest.fixture
def jwks(jwks_server: JwksState) -> JwksState:
    jwks_server.keys = [jwk(KEY, "k1")]
    jwks_server.fetches = 0
    jwks_server.delay = 0.0
    return jwks_server


# --- server under test --------------------------------------------------------


def store_config(jwks_uri: str) -> StoreConfig:
    return StoreConfig.model_validate(
        {
            "id": "demo",
            "prefix": "demo",
            "brand": "Demo",
            "site": "https://demo.example",
            "adapter": {"kind": "fake"},
            "server": {"public_url": PUBLIC_URL},
            "auth": {
                "enabled": True,
                "issuer": ISSUER,
                "jwks_uri": jwks_uri,
                "tool_scopes": {"orders_history": ["orders.read"]},
            },
            "conformance": {"sample_query": "x"},
        }
    )


def build(jwks_uri: str) -> Any:
    mcp = MCPServer(name="demo")

    @mcp.tool(name=PERSONAL)
    def orders_history() -> str:
        token = get_access_token()
        return (token.subject or "") if token else "anonymous"

    @mcp.tool(name=PUBLIC)
    def products_search() -> str:
        return "catalog"

    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="localhost")
    return wrap_with_auth(app, store_config(jwks_uri), {PERSONAL: ("orders.read",)})


@pytest.fixture
def client(jwks: JwksState) -> Iterator[TestClient]:
    # A fresh app per test: the SDK session manager runs only once per instance.
    with TestClient(build(jwks.url), base_url="http://localhost:8000") as c:
        yield c


def call(name: str, rid: int = 1, meta: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "arguments": {}}
    if meta:
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params}


def post(c: TestClient, body: Any, token: str | None = None, **headers: str) -> Any:
    h = {**ACCEPT, "content-type": "application/json", **headers}
    if token:
        h["authorization"] = f"Bearer {token}"
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return c.post("/mcp", content=content, headers=h)


def modern(name: str) -> dict[str, str]:
    return {"mcp-protocol-version": MODERN, "mcp-method": "tools/call", "mcp-name": name}


def text(r: Any) -> str:
    return r.json()["result"]["content"][0]["text"]


def challenge(r: Any) -> str:
    return r.headers["www-authenticate"]


# --- 401 / 403 / 200 ----------------------------------------------------------


def test_personal_tool_without_token_is_401_with_challenge(client):
    r = post(client, call(PERSONAL))
    assert r.status_code == 401
    assert challenge(r).startswith("Bearer ")
    assert 'error="invalid_token"' in challenge(r)
    assert f'resource_metadata="{METADATA_URL}"' in challenge(r)
    assert 'scope="orders.read"' in challenge(r)
    assert r.json()["error"] == "invalid_token"


def test_public_tool_without_token_is_200(client):
    r = post(client, call(PUBLIC))
    assert r.status_code == 200
    assert text(r) == "catalog"


def test_valid_token_reaches_tool_and_auth_context(client):
    # Positive control for every rejection below: the same mint() with defaults passes.
    r = post(client, call(PERSONAL), token=mint())
    assert r.status_code == 200
    assert text(r) == "user-42"  # get_access_token() inside the tool: AuthContextMiddleware works


def test_scp_list_is_accepted(client):
    r = post(client, call(PERSONAL), token=mint(drop=("scope",), scp=["orders.read"]))
    assert r.status_code == 200


def test_token_without_scope_is_403(client):
    r = post(client, call(PERSONAL), token=mint(scope="profile"))
    assert r.status_code == 403
    assert 'error="insufficient_scope"' in challenge(r)
    assert 'scope="orders.read"' in challenge(r)
    assert f'resource_metadata="{METADATA_URL}"' in challenge(r)


BAD_TOKENS = {
    "foreign_aud": lambda: mint(aud="https://other.example/mcp"),
    "foreign_iss": lambda: mint(iss="https://evil.example"),
    "expired": lambda: mint(exp=int(time.time()) - 120),  # beyond the 30 s leeway
    "not_yet_valid": lambda: mint(nbf=int(time.time()) + 120),
    "no_exp": lambda: mint(drop=("exp",)),
    "no_aud": lambda: mint(drop=("aud",)),
    "foreign_signature": lambda: mint(key=FOREIGN),  # same kid, someone else's key
    "unknown_kid": lambda: mint(kid="nope"),
    "alg_none": lambda: jwt.encode(
        {
            "iss": ISSUER,
            "aud": PUBLIC_URL,
            "sub": "x",
            "exp": int(time.time()) + 300,
            "scope": "orders.read",
        },
        None,
        algorithm="none",
        headers={"kid": "k1"},
    ),
    "alg_hs256": lambda: jwt.encode(
        {
            "iss": ISSUER,
            "aud": PUBLIC_URL,
            "sub": "x",
            "exp": int(time.time()) + 300,
            "scope": "orders.read",
        },
        "a-shared-secret-of-thirty-two-bytes!",
        algorithm="HS256",
        headers={"kid": "k1"},
    ),
    "garbage": lambda: "not.a.jwt",
}


@pytest.mark.parametrize("case", BAD_TOKENS)
def test_bad_token_on_personal_tool_is_401(client, case):
    r = post(client, call(PERSONAL), token=BAD_TOKENS[case]())
    assert r.status_code == 401
    assert 'error="invalid_token"' in challenge(r)


def test_bad_token_on_public_tool_is_anonymous_not_an_error(client):
    r = post(client, call(PUBLIC), token=BAD_TOKENS["foreign_aud"]())
    assert r.status_code == 200


# --- gate bypass attempts -----------------------------------------------------


def test_batch_with_one_personal_call_is_401(client):
    r = post(client, [call(PUBLIC, 1), call(PERSONAL, 2)])
    assert r.status_code == 401
    assert 'scope="orders.read"' in challenge(r)


def test_notification_shaped_call_is_gated(client):
    msg = call(PERSONAL)
    del msg["id"]
    assert post(client, msg).status_code == 401


@pytest.mark.parametrize(
    "name", ["DEMO_ORDERS_HISTORY", "Demo_Orders_History", "\uff44emo_orders_history"]
)
def test_name_case_and_width_variants_are_gated(client, name):
    # "\uff44" is a fullwidth d: NFKC folds it, as a client-side normalizer might.
    assert post(client, call(name)).status_code == 401


def test_utf16_body_on_modern_transport(client, jwks):
    # The 2026-07-28 transport decodes with json.loads, which accepts UTF-16; pydantic_core
    # (the legacy transport) does not. A gate with only one parser is bypassed here.
    body = json.dumps(call(PERSONAL, meta=True)).encode("utf-16")
    assert post(client, body, token=mint(), **modern(PERSONAL)).status_code == 200  # path is live
    assert post(client, body, **modern(PERSONAL)).status_code == 401


def test_lone_surrogate_body_on_modern_transport(client):
    msg = call(PERSONAL, meta=True)
    msg["params"]["arguments"] = {"note": "\ud800"}  # json.dumps escapes it as \ud800
    body = json.dumps(msg).encode()
    with pytest.raises(ValueError):
        pydantic_core.from_json(body)
    assert post(client, body, token=mint(), **modern(PERSONAL)).status_code == 200
    assert post(client, body, **modern(PERSONAL)).status_code == 401


def test_invalid_json_is_left_to_the_sdk(client):
    r = post(client, b'{"jsonrpc": "2.0", "method": "tools/call", ')
    assert r.status_code == 400  # SDK parse error, not a gate decision
    assert "www-authenticate" not in r.headers


def test_body_over_limit_is_413(client):
    big = call(PUBLIC)
    big["params"]["arguments"] = {"q": "x" * auth.MAX_BODY}
    r = post(client, big)
    assert r.status_code == 413
    assert r.json()["error"] == "request_too_large"


# --- gate at the ASGI level ---------------------------------------------------


class Recorder:
    """Inner app: records what it was given to read."""

    def __init__(self) -> None:
        self.called = False
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send):
        self.called = True
        self.messages.append(await receive())
        self.messages.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def run_gate(messages: list[dict[str, Any]], headers=(), max_body: int = 100):
    inner = Recorder()
    gate = ToolScopeGate(inner, {PERSONAL: ("orders.read",)}, METADATA_URL, max_body)
    queue = list(messages)
    sent: list[dict[str, Any]] = []

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": list(headers)}
    await gate(scope, receive, send)
    return inner, sent


@pytest.mark.anyio
async def test_chunked_body_over_limit_is_413_without_reaching_app():
    chunk = {"type": "http.request", "body": b"x" * 60, "more_body": True}
    inner, sent = await run_gate([chunk, chunk, {"type": "http.request", "body": b""}])
    assert sent[0]["status"] == 413
    assert not inner.called


@pytest.mark.anyio
async def test_declared_length_over_limit_is_413_before_reading():
    inner, sent = await run_gate([], headers=[(b"content-length", b"101")])
    assert sent[0]["status"] == 413
    assert not inner.called


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["²", "9" * 5000])
async def test_malformed_content_length_does_not_crash_gate(value):
    msg = {"type": "http.request", "body": b"{}", "more_body": False}
    _, sent = await run_gate([msg], headers=[(b"content-length", value.encode("latin-1"))])
    assert sent[0]["status"] in (200, 413)


@pytest.mark.anyio
async def test_body_split_into_chunks_is_gated_as_a_whole():
    raw = json.dumps(call(PERSONAL)).encode()
    first = {"type": "http.request", "body": raw[:20], "more_body": True}
    rest = {"type": "http.request", "body": raw[20:], "more_body": False}
    inner, sent = await run_gate([first, rest], max_body=1000)
    assert sent[0]["status"] == 401
    assert not inner.called


@pytest.mark.anyio
async def test_second_body_after_the_end_never_reaches_app():
    public = json.dumps(call(PUBLIC)).encode()
    personal = json.dumps(call(PERSONAL)).encode()
    inner, _ = await run_gate(
        [
            {"type": "http.request", "body": public, "more_body": False},
            {"type": "http.request", "body": personal, "more_body": False},
        ],
        max_body=1000,
    )
    assert inner.messages[0]["body"] == public
    assert inner.messages[1] == {"type": "http.disconnect"}


@pytest.mark.anyio
async def test_lifespan_passes_through_gate():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await ToolScopeGate(inner, {}, METADATA_URL, 10)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_scope_token_that_would_break_the_header_is_refused():
    with pytest.raises(ValueError):
        ToolScopeGate(None, {PERSONAL: ('orders.read", evil="1',)}, METADATA_URL, 10)


# --- RFC 9728 -----------------------------------------------------------------


def test_protected_resource_metadata(client):
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    data = r.json()
    assert data["resource"] == PUBLIC_URL
    assert [s.rstrip("/") for s in data["authorization_servers"]] == [ISSUER]
    assert data["scopes_supported"] == ["orders.read"]


# --- verifier -----------------------------------------------------------------


def verifier(jwks: JwksState) -> JwksTokenVerifier:
    return JwksTokenVerifier(ISSUER, PUBLIC_URL, jwks.url)


@pytest.mark.anyio
async def test_verifier_rejects_foreign_audience(jwks):
    v = verifier(jwks)
    assert await v.verify_token(mint()) is not None
    assert await v.verify_token(mint(aud="https://other.example/mcp")) is None


@pytest.mark.anyio
async def test_verifier_rejects_expired(jwks):
    # On its own: over HTTP the SDK backend re-checks expires_at and would hide a regression.
    assert await verifier(jwks).verify_token(mint(exp=int(time.time()) - 120)) is None


@pytest.mark.anyio
async def test_verifier_maps_claims(jwks):
    token = await verifier(jwks).verify_token(mint(drop=("client_id",), azp="agent-2"))
    assert token is not None
    assert (token.client_id, token.subject, token.scopes) == ("agent-2", "user-42", ["orders.read"])
    assert token.resource == PUBLIC_URL


@pytest.mark.parametrize("algorithms", [(), ("HS256",), ("none",), ("RS256", "HS256")])
def test_verifier_refuses_symmetric_or_empty_allowlist(jwks, algorithms):
    with pytest.raises(ValueError):
        JwksTokenVerifier(ISSUER, PUBLIC_URL, jwks.url, algorithms=algorithms)


@pytest.mark.anyio
async def test_jwks_is_cached(jwks):
    v = verifier(jwks)
    for _ in range(3):
        assert await v.verify_token(mint()) is not None
    assert jwks.fetches == 1


@pytest.mark.anyio
async def test_disallowed_alg_costs_no_jwks_fetch(jwks):
    assert await verifier(jwks).verify_token(BAD_TOKENS["alg_none"]()) is None
    assert jwks.fetches == 0


@pytest.mark.anyio
async def test_kid_rotation_refetches_jwks(jwks, monkeypatch):
    monkeypatch.setattr(auth, "JWKS_COOLDOWN", 0.0)
    v = verifier(jwks)
    assert await v.verify_token(mint()) is not None
    jwks.keys = [jwk(KEY, "k1"), jwk(ROTATED, "k2")]
    assert await v.verify_token(mint(key=ROTATED, kid="k2")) is not None
    assert jwks.fetches == 2


@pytest.mark.anyio
async def test_unknown_kid_spam_does_not_hammer_jwks(jwks):
    v = verifier(jwks)
    assert await v.verify_token(mint()) is not None
    for i in range(5):
        assert await v.verify_token(mint(kid=f"random-{i}")) is None
    assert jwks.fetches == 1  # default cooldown holds back forced refetches


@pytest.mark.anyio
async def test_slow_jwks_fails_closed_within_timeout(jwks, monkeypatch):
    monkeypatch.setattr(auth, "JWKS_TIMEOUT", 0.3)
    jwks.delay = 2.0
    started = time.monotonic()
    assert await verifier(jwks).verify_token(mint()) is None
    assert time.monotonic() - started < 1.5


# --- a slow JWKS must not take the catalog down (security Н2) ---------------------------


@pytest.fixture
def silent_jwks() -> Iterator[str]:
    """Accepts connections (the kernel backlog does) and never answers: a dropped IdP."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(256)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}/jwks"
    sock.close()


def junk_bearer() -> str:
    header = jwt.utils.base64url_encode(b'{"alg":"RS256","kid":"x"}').decode()
    return f"{header}.e30.AAAA"


@pytest.mark.anyio
async def test_dead_jwks_neither_starves_threads_nor_is_retried(silent_jwks, monkeypatch):
    monkeypatch.setattr(auth, "JWKS_TIMEOUT", 1.0)
    v = JwksTokenVerifier(ISSUER, PUBLIC_URL, silent_jwks)
    probe_wait: list[float] = []

    async def probe() -> None:
        await anyio.sleep(0.2)  # the junk is in flight by now
        started = time.monotonic()
        await anyio.to_thread.run_sync(lambda: None)  # what a catalog search needs
        probe_wait.append(time.monotonic() - started)

    with anyio.fail_after(15):
        started = time.monotonic()
        async with anyio.create_task_group() as tg:
            for _ in range(50):
                tg.start_soon(v.verify_token, junk_bearer())
            tg.start_soon(probe)
        took = time.monotonic() - started
    assert probe_wait[0] < 0.5, probe_wait  # the shared pool stayed free
    assert took < 4, took  # one timed-out fetch, then the negative cache answers


@pytest.mark.anyio
async def test_rate_limit_comes_before_token_checks(monkeypatch):
    from fakes import FakeAdapter
    from test_tools_spec import cfg

    from shopmcp.server.app import build_app

    checked: list[str] = []

    async def counting(self, token):
        checked.append(token)

    monkeypatch.setattr(JwksTokenVerifier, "verify_token", counting)
    c = cfg(
        auth__enabled=True,
        auth__issuer=ISSUER,
        auth__jwks_uri="http://127.0.0.1:9/jwks",
        limits__rate_per_minute=3,
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(build_app(c, adapter=FakeAdapter()), base_url="http://localhost:8000") as tc:
        codes = [post(tc, body, token=junk_bearer()).status_code for _ in range(6)]
    assert codes.count(429) == 3
    assert len(checked) == 3  # the throttled requests never reached the verifier

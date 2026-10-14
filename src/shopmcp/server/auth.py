"""Hybrid auth: public tools without a token, personal tools behind a per-tool scope.

The SDK guards the whole endpoint or nothing (`RequireAuthMiddleware`), so the per-tool
decision is a thin ASGI gate in front of it. The server is only a resource server: tokens
come from the store's authorization server and are checked here against its JWKS.
"""

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import anyio.to_thread
import jwt
import pydantic_core
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from pydantic import AnyHttpUrl
from starlette.datastructures import Headers
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shopmcp.config import StoreConfig

logger = logging.getLogger(__name__)

# Asymmetric only: with HS* the "key" is the public JWKS, i.e. anyone could sign.
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
JWKS_TIMEOUT = 5.0  # seconds per JWKS fetch; verification fails closed on timeout
JWKS_LIFESPAN = 300.0  # seconds the fetched key set is trusted
JWKS_COOLDOWN = 30.0  # min seconds between refetches forced by an unknown kid
JWKS_DOWN_BACKOFF = 10.0  # seconds a failed JWKS fetch is not retried: tokens just fail
# Seconds a check waits for another thread's JWKS fetch. JWKS_TIMEOUT bounds one socket
# operation, not the fetch: an IdP trickling the body holds the lock for as long as it likes.
JWKS_LOCK_WAIT = 2 * JWKS_TIMEOUT
VERIFY_THREADS = 4  # worker threads token checks may hold; the catalog keeps the rest
MAX_BODY = 256 * 1024  # bytes; the gate must see the whole body before the SDK does

# RFC 6749 scope-token: no space, quote or backslash, so it is safe inside a quoted header.
_SCOPE_TOKEN = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")
# Both parsers the SDK uses on a POST body: the legacy transport (pydantic_core) and the
# 2026-07-28 one (json). They disagree (UTF-16, BOM, lone surrogates), so the gate looks
# through both: a call either of them would dispatch is a call the gate must see.
_PARSERS: tuple[Callable[[bytes], Any], ...] = (pydantic_core.from_json, json.loads)


def issuer_forms(issuer: str) -> tuple[str, ...]:
    """`iss` values accepted for `auth.issuer`: as written and as the RFC 9728 metadata
    advertises it, after the SDK's URL normalization (which adds "/" to a bare host).
    A bare host is taken with and without the slash: authorization servers publish both
    (`https://accounts.example`, `https://tenant.example/`). A path is compared exactly."""
    try:
        normalized = str(AnyHttpUrl(issuer))
    except ValueError:
        return (issuer,)
    forms = {issuer, normalized}
    if urllib.parse.urlsplit(normalized).path == "/":
        forms.add(normalized.rstrip("/"))
    return tuple(sorted(forms))


class JwksTokenVerifier(TokenVerifier):
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_uri: str,
        algorithms: tuple[str, ...] = ("RS256", "ES256"),
        leeway: int = 30,
    ) -> None:
        if not algorithms or not set(algorithms) <= ASYMMETRIC_ALGORITHMS:
            raise ValueError(
                f"algorithms must be a non-empty subset of {sorted(ASYMMETRIC_ALGORITHMS)}"
            )
        self.issuer = issuer
        self._issuers = issuer_forms(issuer)
        self.audience = audience
        self.algorithms = algorithms
        self.leeway = leeway
        # Blocking urllib client with its own lock: called from a worker thread only.
        self._jwks = jwt.PyJWKClient(
            jwks_uri, lifespan=JWKS_LIFESPAN, timeout=JWKS_TIMEOUT, cooldown_duration=JWKS_COOLDOWN
        )
        # A slow or dead JWKS must not take the shared thread pool (the catalog searches run
        # there) nor be hit again by every junk Bearer: own small limiter + a negative cache.
        self._limiter: anyio.CapacityLimiter | None = None  # needs a running event loop
        self._jwks_down_until = 0.0
        self._jwks_lock = threading.Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        """Signature by JWKS (cached PyJWKClient, called via anyio.to_thread), iss,
        aud == resource url, exp/nbf; scopes from "scope" (str) or "scp" (list)."""
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(VERIFY_THREADS)
        try:
            return await anyio.to_thread.run_sync(self._verify, token, limiter=self._limiter)
        # Any failure means "not authenticated": an exception here would turn a stray bad
        # token on a public tool into a 500 from AuthenticationMiddleware.
        except Exception as e:  # noqa: BLE001
            logger.info("bearer token rejected: %s: %s", type(e).__name__, e)
            return None

    def _verify(self, token: str) -> AccessToken:
        # Checked before the key lookup, so alg=none or HS256 never costs a JWKS fetch.
        if jwt.get_unverified_header(token).get("alg") not in self.algorithms:
            raise jwt.InvalidAlgorithmError("alg is not allowed")
        # Checked under the lock: threads queued behind a failing fetch must see its failure
        # instead of each fetching again (PyJWKClient serializes lookups anyway).
        if not self._jwks_lock.acquire(timeout=JWKS_LOCK_WAIT):
            raise jwt.PyJWKClientConnectionError("JWKS fetch in progress for too long")
        try:
            if time.monotonic() < self._jwks_down_until:
                raise jwt.PyJWKClientConnectionError("JWKS unreachable a moment ago, not retried")
            try:
                key = self._jwks.get_signing_key_from_jwt(token)
            except jwt.PyJWKClientConnectionError:
                self._jwks_down_until = time.monotonic() + JWKS_DOWN_BACKOFF
                raise
        finally:
            self._jwks_lock.release()
        claims = jwt.decode(
            token,
            key,
            algorithms=list(self.algorithms),
            audience=self.audience,
            issuer=self._issuers,
            leeway=self.leeway,
            options={"require": ["exp", "iss", "aud"]},
        )
        scopes = _scopes(claims.get("scope", claims.get("scp")))
        client_id = claims.get("client_id") or claims.get("azp") or claims.get("sub")
        if scopes is None or not isinstance(client_id, str):
            raise jwt.InvalidTokenError("malformed scope or no client_id/azp/sub")
        sub = claims.get("sub")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.audience,
            subject=sub if isinstance(sub, str) else None,
            claims=claims,
        )


def _scopes(raw: Any) -> list[str] | None:
    """`scope` is a space-separated string (RFC 9068), `scp` a list (Azure, Okta)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list) and all(isinstance(s, str) for s in raw):
        return list(raw)
    return None


def _fold(name: str) -> str:
    # The SDK looks tools up by exact name; folding only makes the gate stricter than that.
    return unicodedata.normalize("NFKC", name).casefold()


def _called_tools(body: bytes) -> list[str]:
    """Names of every tools/call in the body, single message or batch, as any SDK parser
    would read it. Unparsable for all parsers -> nothing to gate; the SDK answers -32700."""
    names: list[str] = []
    for parse in _PARSERS:
        try:
            doc = parse(body)
        except (ValueError, RecursionError):
            continue
        for msg in doc if isinstance(doc, list) else [doc]:
            if not isinstance(msg, dict):
                continue
            method, params = msg.get("method"), msg.get("params")
            if (
                isinstance(method, str)
                and _fold(method) == "tools/call"
                and isinstance(params, dict)
                and isinstance(name := params.get("name"), str)
            ):
                names.append(name)
    return names


class ToolScopeGate:
    """For POST on the MCP path: buffer the body (<= max_body), parse JSON-RPC (single or
    batch); for each tools/call of a tool in `tool_scopes`: no authenticated user -> 401,
    missing scope -> 403, both with WWW-Authenticate carrying resource_metadata and scope.
    Otherwise replay the buffered body to `app`. Non-HTTP scopes pass through.

    Every HTTP request is inspected, not only POST on one path: the gate then does not
    depend on how the app routes, and a body-less GET costs one receive()."""

    def __init__(
        self,
        app: ASGIApp,
        tool_scopes: Mapping[str, tuple[str, ...]],
        resource_metadata_url: str,
        max_body: int,
    ) -> None:
        bad = [s for scopes in tool_scopes.values() for s in scopes if not _SCOPE_TOKEN.match(s)]
        if bad:
            raise ValueError(f"malformed scope tokens: {bad}")
        if '"' in resource_metadata_url or "\\" in resource_metadata_url:
            raise ValueError("resource_metadata_url must not contain quotes or backslashes")
        self.app = app
        self.tool_scopes = {_fold(name): tuple(scopes) for name, scopes in tool_scopes.items()}
        self.resource_metadata_url = resource_metadata_url
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length", "")
        # isascii: "²".isdigit() is true and int() of it raises; so does int() of a
        # 5000-digit header, hence the length cap. Malformed values are left to the server.
        if (
            declared.isascii()
            and declared.isdigit()
            and (len(declared) > 18 or int(declared) > self.max_body)
        ):
            await self._too_large(send)
            return
        chunks: list[bytes] = []
        size, more = 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:  # chunked or a lying Content-Length
                await self._too_large(send)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)

        required: list[str] = []
        protected = False
        for name in _called_tools(body):
            scopes = self.tool_scopes.get(_fold(name))
            if scopes is not None:
                protected = True
                required += [s for s in scopes if s not in required]
        if protected:
            user = scope.get("user")
            if not isinstance(user, AuthenticatedUser):
                await self._deny(send, 401, "invalid_token", "Authentication required", required)
                return
            missing = [s for s in required if s not in user.scopes]
            if missing:
                await self._deny(
                    send,
                    403,
                    "insufficient_scope",
                    f"Required scope: {' '.join(missing)}",
                    required,
                )
                return

        await self.app(scope, _replay(body, receive), send)

    async def _deny(
        self, send: Send, status: int, error: str, description: str, scopes: Sequence[str]
    ) -> None:
        challenge = (
            f'Bearer error="{error}", error_description="{description}", '
            f'resource_metadata="{self.resource_metadata_url}", scope="{" ".join(scopes)}"'
        )
        await _send_json(
            send, status, {"error": error, "error_description": description}, challenge
        )

    async def _too_large(self, send: Send) -> None:
        body = {
            "error": "request_too_large",
            "error_description": f"Request body exceeds {self.max_body} bytes",
        }
        await _send_json(send, 413, body)


def _replay(body: bytes, receive: Receive) -> Receive:
    """The buffered body exactly once, then the client's own receive() (disconnects).
    The body was read to its end, so a further http.request can only be a second body
    the gate never saw: it is turned into a disconnect instead of reaching the app."""
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        message = await receive()
        if message["type"] == "http.request":
            return {"type": "http.disconnect"}
        return message

    return replay


async def _send_json(
    send: Send, status: int, payload: dict[str, str], www_authenticate: str | None = None
) -> None:
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if www_authenticate is not None:
        headers.append((b"www-authenticate", www_authenticate.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _MetadataRoutes:
    """Serves the RFC 9728 routes without a token; everything else (lifespan included)
    goes to `app`. Not a Starlette Router: that would swallow the app's lifespan."""

    def __init__(self, routes: Iterable[BaseRoute], app: ASGIApp) -> None:
        self.routes = list(routes)
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for route in self.routes:
                match, child = route.matches(scope)
                if match is not Match.NONE:
                    await route.handle({**scope, **child}, receive, send)
                    return
        await self.app(scope, receive, send)


def wrap_with_auth(
    app: ASGIApp, cfg: StoreConfig, tool_scopes: Mapping[str, tuple[str, ...]]
) -> ASGIApp:
    """AuthenticationMiddleware(BearerAuthBackend(verifier)) -> AuthContextMiddleware ->
    ToolScopeGate -> app, plus the RFC 9728 route from create_protected_resource_routes.

    `tool_scopes` is keyed by full tool name and is the only source: `build_tools` has
    already applied `[auth.tool_scopes]` of the config to the tools' defaults."""
    resource_url = str(cfg.server.public_url)
    scopes = {name: tuple(v) for name, v in tool_scopes.items()}
    metadata_url = build_resource_metadata_url(AnyHttpUrl(resource_url))

    verifier = JwksTokenVerifier(cfg.auth.issuer, resource_url, cfg.auth.jwks_uri)
    gate = ToolScopeGate(app, scopes, str(metadata_url), MAX_BODY)
    authed = AuthenticationMiddleware(
        AuthContextMiddleware(gate), backend=BearerAuthBackend(verifier)
    )
    routes = create_protected_resource_routes(
        AnyHttpUrl(resource_url),
        [AnyHttpUrl(cfg.auth.issuer)],
        scopes_supported=sorted({s for v in scopes.values() for s in v}),
    )
    return _MetadataRoutes(routes, authed)

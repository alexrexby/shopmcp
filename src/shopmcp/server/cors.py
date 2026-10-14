"""CORS on the MCP endpoint for browser clients, opt-in by `server.allowed_origins`.

The SDK puts no CORS on the MCP path: `allowed_origins` alone only lets an `Origin` past
its DNS-rebinding check, and a browser still blocks the preflight (405) and the answer
(no `Access-Control-Allow-Origin`). This layer answers the preflight itself and adds the
headers to every answer for an allowed origin, 401/403/429 included, so it sits outside
the rate limit and the auth. Other paths are left alone: the RFC 9728 metadata route has
the SDK's own CORS, and a second `Access-Control-Allow-Origin` makes a browser reject it.
"""

import re
from collections.abc import Iterable, Sequence

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Request headers a browser client may send. `*` would not cover Authorization (Fetch
# spec), and the 2026-07-28 transport sends a family of `mcp-*` headers (mcp-param-<name>
# among them), so the requested list is echoed back, filtered by this rule.
_ALLOWED = frozenset({"content-type", "authorization", "last-event-id"})
_TOKEN = re.compile(r"^[a-z0-9-]+$")
EXPOSED = "www-authenticate, mcp-session-id, mcp-protocol-version, retry-after"
MAX_AGE = "600"  # seconds a browser may reuse a preflight answer


def origin_allowed(origin: str, allowed: Sequence[str]) -> bool:
    """The SDK's rule (`TransportSecurityMiddleware._validate_origin`): an exact match, or
    `scheme://host:*` for any port. The same rule on both sides, or a preflight would pass
    for a POST the SDK then refuses with 403, or the other way round."""
    if origin in allowed:
        return True
    return any(a.endswith(":*") and origin.startswith(a[:-2] + ":") for a in allowed)


def allowed_headers(requested: str) -> str:
    names = (h.strip().lower() for h in requested.split(","))
    return ", ".join(
        n for n in names if _TOKEN.match(n) and (n in _ALLOWED or n.startswith("mcp-"))
    )


class McpCors:
    """For an allowed `Origin` on the MCP path: a preflight (OPTIONS with
    Access-Control-Request-Method) gets 204 here and never reaches the app; any other
    request goes on and its answer gets Access-Control-Allow-Origin = that origin.
    A foreign origin passes untouched: PostOnly answers its preflight 405, the SDK its
    POST 403, both without CORS headers. `*` and credentials are never sent."""

    def __init__(self, app: ASGIApp, path: str, origins: Iterable[str]) -> None:
        self.app = app
        self.paths = {path, path.rstrip("/") + "/"}
        self.origins = list(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in self.paths:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if not origin or not origin_allowed(origin, self.origins):
            await self.app(scope, receive, send)
            return
        cors = [(b"access-control-allow-origin", origin.encode("latin-1")), (b"vary", b"Origin")]
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            requested = allowed_headers(headers.get("access-control-request-headers", ""))
            cors += [
                (b"access-control-allow-methods", b"POST"),
                (b"access-control-max-age", MAX_AGE.encode()),
            ]
            if requested:
                cors.append((b"access-control-allow-headers", requested.encode()))
            await send({"type": "http.response.start", "status": 204, "headers": cors})
            await send({"type": "http.response.body", "body": b""})
            return
        cors.append((b"access-control-expose-headers", EXPOSED.encode()))

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *cors]}
            await send(message)

        await self.app(scope, receive, send_with_cors)

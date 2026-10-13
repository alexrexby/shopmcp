"""Hybrid auth: public tools without a token, personal tools behind a per-tool scope.

The SDK guards the whole endpoint or nothing (`RequireAuthMiddleware`), so the per-tool
decision is a thin ASGI gate in front of it. Implemented in stage 1C.
"""

from collections.abc import Mapping

from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.types import ASGIApp, Receive, Scope, Send

from shopmcp.config import StoreConfig


class JwksTokenVerifier(TokenVerifier):
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_uri: str,
        algorithms: tuple[str, ...] = ("RS256", "ES256"),
        leeway: int = 30,
    ) -> None:
        raise NotImplementedError

    async def verify_token(self, token: str) -> AccessToken | None:
        """Signature by JWKS (cached PyJWKClient, called via anyio.to_thread), iss,
        aud == resource url, exp/nbf; scopes from "scope" (str) or "scp" (list)."""
        raise NotImplementedError


class ToolScopeGate:
    """For POST on the MCP path: buffer the body (<= max_body), parse JSON-RPC (single or
    batch); for each tools/call of a tool in `tool_scopes`: no authenticated user -> 401,
    missing scope -> 403, both with WWW-Authenticate carrying resource_metadata and scope.
    Otherwise replay the buffered body to `app`. Non-HTTP scopes pass through."""

    def __init__(
        self,
        app: ASGIApp,
        tool_scopes: Mapping[str, tuple[str, ...]],
        resource_metadata_url: str,
        max_body: int,
    ) -> None:
        raise NotImplementedError

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError


def wrap_with_auth(
    app: ASGIApp, cfg: StoreConfig, tool_scopes: Mapping[str, tuple[str, ...]]
) -> ASGIApp:
    """AuthenticationMiddleware(BearerAuthBackend(verifier)) -> AuthContextMiddleware ->
    ToolScopeGate -> app, plus the RFC 9728 route from create_protected_resource_routes."""
    raise NotImplementedError

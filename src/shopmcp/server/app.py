"""ASGI app for one store: MCPServer with the tools its adapter supports, /healthz,
rate limit and, when enabled, auth and CORS. Every SDK call of the server side lives here and in
`server/auth.py`."""

import functools
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from shopmcp import __version__
from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterContext, ShopAdapter
from shopmcp.cart import strategies
from shopmcp.config import ConfigError, StoreConfig
from shopmcp.envelope import fail
from shopmcp.errors import InternalError, ShopError
from shopmcp.i18n import Texts, builtin
from shopmcp.server.cors import McpCors
from shopmcp.server.instructions import build_instructions
from shopmcp.server.ratelimit import RateLimit
from shopmcp.tools.spec import ToolSpec, build_tools, canonical_tools, with_param_docs

log = logging.getLogger(__name__)

HEALTH_PATH = "/healthz"
HTTP_TIMEOUT = 10.0  # seconds, for adapters that call the store over HTTP


def check_text_keys(cfg: StoreConfig, specs: Iterable[ToolSpec]) -> None:
    """A `[texts]` key nobody reads is a typo that would silently do nothing: fail the start."""
    known = {key for key, _ in builtin()}
    known.update(key for spec in specs for key, _ in spec.texts())
    unknown = sorted(set(cfg.texts) - known)
    if unknown:
        raise ConfigError(f"texts: unknown keys {unknown}")


def check_tool_scopes(cfg: StoreConfig, specs: Iterable[ToolSpec]) -> None:
    """A misspelled `[auth.tool_scopes]` key would leave the real tool on its default."""
    unknown = sorted(set(cfg.auth.tool_scopes) - {spec.suffix for spec in specs})
    if unknown:
        raise ConfigError(f"auth.tool_scopes: unknown tools {unknown}")


class PostOnly:
    """Only POST reaches the MCP endpoint. The server is stateless and answers in JSON, yet
    the SDK still opens a never-ending SSE stream on GET; here GET and the rest get 405."""

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self.paths = {path, path.rstrip("/") + "/"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in self.paths or scope["method"] == "POST":
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": "Method Not Allowed"},
            }
        ).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"allow", b"POST"),
        ]
        await send({"type": "http.response.start", "status": 405, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def guarded(
    fn: Callable[..., Awaitable[CallToolResult]], texts: Texts
) -> Callable[..., Awaitable[CallToolResult]]:
    """ShopError -> its error result; anything else -> internal_error without the trace."""

    @functools.wraps(fn)  # keeps the signature: the SDK builds the schemas from it
    async def tool(*args: Any, **kwargs: Any) -> CallToolResult:
        try:
            return await fn(*args, **kwargs)
        except ShopError as e:
            return fail(e, texts)
        except Exception:
            log.exception("tool %s failed", fn.__name__)
            return fail(InternalError(), texts)

    return tool


def build_app(
    cfg: StoreConfig,
    *,
    adapter: ShopAdapter | None = None,
    data_dir: Path = Path("data"),
    config_dir: Path = Path("."),
) -> ASGIApp:
    """`adapter` overrides the one from `cfg.adapter` (tests); `data_dir` holds its index;
    relative paths in the config start at `config_dir`, the directory of store.toml."""
    texts = Texts(cfg.locale, cfg.texts)
    if adapter is None:
        ctx = AdapterContext(
            data_dir=data_dir, http_timeout=HTTP_TIMEOUT, store_id=cfg.id, config_dir=config_dir
        )
        adapter = registry.create(cfg.adapter, ctx)
    shop = adapter  # narrowed for the closures below
    cart = strategies.create(
        cfg.cart, shop, texts, site=cfg.site, public_url=cfg.server.public_url, store_id=cfg.id
    )
    canonical = canonical_tools(cfg, shop, cart)
    check_text_keys(cfg, canonical)
    check_tool_scopes(cfg, canonical)
    specs = build_tools(cfg, shop, cart)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        await shop.start()
        try:
            yield
        finally:
            await shop.close()

    server = MCPServer(
        name=cfg.brand,
        title=cfg.brand,
        instructions=build_instructions(cfg, specs, texts),
        website_url=str(cfg.site),
        version=__version__,
        lifespan=lifespan,
        # No events to publish: subscriptions/listen (2026-07-28) would only hold open an
        # endless SSE stream over POST, the same class as GET that PostOnly refuses.
        subscriptions=False,
    )
    for spec in specs:
        key = f"tool.{spec.suffix}"
        docs = {n: texts.get(f"{key}.param.{n}", t) for n, t in spec.params.items()}
        server.add_tool(
            with_param_docs(guarded(spec.fn, texts), docs),
            name=f"{cfg.prefix}_{spec.suffix}",
            title=texts.get(f"{key}.title", spec.title),
            description=texts.get(f"{key}.description", spec.description),
            annotations=spec.annotations,
        )

    @server.custom_route(HEALTH_PATH, methods=["GET"])
    async def healthz(_: Request) -> Response:
        try:
            health = await shop.health()
        except Exception:
            log.exception("health check failed")
            return JSONResponse({"status": "error", "store": cfg.id}, status_code=503)
        if health.get("stale"):
            # A frozen catalog sells yesterday's prices: the HEALTHCHECK must turn red.
            return JSONResponse({"status": "stale", "store": cfg.id, **health}, status_code=503)
        return JSONResponse({"status": "ok", "store": cfg.id, **health})

    if cfg.cart.strategy == "form_post":
        # Imported only here: a store without the trampoline does not depend on it. Outside
        # the auth (a customer's browser opens it), under the rate limit like any request.
        from shopmcp.cart.form_post import PATH
        from shopmcp.server.trampoline import route

        server.custom_route(PATH + "{token}", methods=["GET"])(route(cfg, texts))

    # host and allowed_hosts always explicit: with the default host the SDK allows only
    # localhost Host headers, and everything behind a reverse proxy is rejected.
    app: ASGIApp = server.streamable_http_app(
        streamable_http_path=cfg.server.path,
        stateless_http=True,
        json_response=True,
        host=cfg.server.host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=cfg.server.allowed_hosts, allowed_origins=cfg.server.allowed_origins
        ),
    )
    app = PostOnly(app, cfg.server.path)
    if cfg.auth.enabled:
        # Imported only here: a store without auth does not depend on the auth module.
        from shopmcp.server.auth import wrap_with_auth

        tool_scopes = {f"{cfg.prefix}_{s.suffix}": s.scopes for s in specs if s.scopes}
        app = wrap_with_auth(app, cfg, tool_scopes)
    # Outside the auth: a token check costs a JWKS lookup, so it is counted like any request.
    app = RateLimit(
        app,
        cfg.limits.rate_per_minute,
        trusted_proxies=cfg.server.trusted_proxies,
        exempt_paths=(HEALTH_PATH,),
    )
    if cfg.server.allowed_origins:
        # Outermost: a 429 or 401 without CORS headers is unreadable to a browser client.
        app = McpCors(app, cfg.server.path, cfg.server.allowed_origins)
    return app

"""ASGI app for one store: MCPServer with the tools its adapter supports, /healthz,
rate limit and, when enabled, auth. Every SDK call of the server side lives here and in
`server/auth.py`."""

import functools
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
from starlette.types import ASGIApp

from shopmcp import __version__
from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterContext, ShopAdapter
from shopmcp.cart import strategies
from shopmcp.config import ConfigError, StoreConfig
from shopmcp.envelope import fail
from shopmcp.errors import InternalError, ShopError
from shopmcp.i18n import Texts, builtin
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
    cfg: StoreConfig, *, adapter: ShopAdapter | None = None, data_dir: Path = Path("data")
) -> ASGIApp:
    """`adapter` overrides the one from `cfg.adapter` (tests); `data_dir` holds its index."""
    texts = Texts(cfg.locale, cfg.texts)
    if adapter is None:
        ctx = AdapterContext(data_dir=data_dir, http_timeout=HTTP_TIMEOUT, store_id=cfg.id)
        adapter = registry.create(cfg.adapter, ctx)
    shop = adapter  # narrowed for the closures below
    cart = strategies.create(cfg.cart, shop, texts)
    check_text_keys(cfg, canonical_tools(cfg, shop, cart))
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
        return JSONResponse({"status": "ok", "store": cfg.id, **health})

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
    app = RateLimit(
        app,
        cfg.limits.rate_per_minute,
        trusted_proxies=cfg.server.trusted_proxies,
        exempt_paths=(HEALTH_PATH,),
    )
    if cfg.auth.enabled:
        # Imported only here: a store without auth does not depend on the auth module.
        from shopmcp.server.auth import wrap_with_auth

        tool_scopes = {f"{cfg.prefix}_{s.suffix}": s.scopes for s in specs if s.scopes}
        app = wrap_with_auth(app, cfg, tool_scopes)
    return app

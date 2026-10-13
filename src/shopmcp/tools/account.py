"""Personal tools: they need the customer's token issued by the store's auth server."""

from typing import Annotated

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult
from pydantic import Field

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.config import StoreConfig
from shopmcp.domain.models import OrderPage
from shopmcp.envelope import ok
from shopmcp.errors import Unauthorized
from shopmcp.i18n import L10n
from shopmcp.tools.spec import READ_ONLY, ToolSpec, page_meta

ORDERS_SCOPE = "orders.read"  # default; `[auth.tool_scopes]` of the store overrides it


def tools(cfg: StoreConfig, adapter: ShopAdapter) -> list[ToolSpec]:
    max_page = cfg.limits.max_page_size
    budget = cfg.limits.max_response_bytes
    default_limit = min(10, max_page)

    async def orders_history(
        page: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=max_page)] = default_limit,
    ) -> Annotated[CallToolResult, OrderPage]:
        # Defence in depth: ToolScopeGate answers 401 over HTTP, this is the second lock.
        token = get_access_token()
        if token is None:
            raise Unauthorized()
        found, total = await adapter.orders(token, page, limit)
        return ok(OrderPage(meta=page_meta(page, limit, total), items=found), budget)

    return [
        ToolSpec(
            suffix="orders_history",
            requires=frozenset({Capability.ORDERS}),
            scopes=(ORDERS_SCOPE,),
            annotations=READ_ONLY,
            output_model=OrderPage,
            fn=orders_history,
            title=L10n(ru="История заказов", en="Order history"),
            description=L10n(
                ru="Заказы покупателя, постранично. Требует входа в аккаунт магазина.",
                en="The customer's orders, paged. Requires sign-in to the store account.",
            ),
            params={
                "page": L10n(ru="Номер страницы, с 1", en="Page number, from 1"),
                "limit": L10n(
                    ru=f"Заказов на странице, от 1 до {max_page}",
                    en=f"Orders per page, 1 to {max_page}",
                ),
            },
        )
    ]

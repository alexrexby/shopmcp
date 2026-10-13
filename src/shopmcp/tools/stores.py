"""Pickup points of the store."""

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.config import StoreConfig
from shopmcp.domain.models import PickupPointPage
from shopmcp.envelope import ok
from shopmcp.i18n import L10n
from shopmcp.tools.spec import READ_ONLY, ToolSpec, page_meta


def tools(cfg: StoreConfig, adapter: ShopAdapter) -> list[ToolSpec]:
    max_page = cfg.limits.max_page_size
    budget = cfg.limits.max_response_bytes
    default_limit = min(10, max_page)

    async def pickup_points(
        city: str | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=max_page)] = default_limit,
    ) -> Annotated[CallToolResult, PickupPointPage]:
        found, total = await adapter.pickup_points(city, page, limit)
        return ok(PickupPointPage(meta=page_meta(page, limit, total), items=found), budget)

    return [
        ToolSpec(
            suffix="pickup_points",
            requires=frozenset({Capability.PICKUP_POINTS}),
            scopes=(),
            annotations=READ_ONLY,
            output_model=PickupPointPage,
            fn=pickup_points,
            title=L10n(ru="Пункты самовывоза", en="Pickup points"),
            description=L10n(
                ru="Магазины и пункты выдачи: адрес, часы работы, телефон. Постранично.",
                en="Stores and pickup points: address, opening hours, phone. Paged.",
            ),
            params={
                "city": L10n(ru="Только этот город", en="Only this city"),
                "page": L10n(ru="Номер страницы, с 1", en="Page number, from 1"),
                "limit": L10n(
                    ru=f"Пунктов на странице, от 1 до {max_page}",
                    en=f"Points per page, 1 to {max_page}",
                ),
            },
        )
    ]

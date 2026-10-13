"""Promotions: products whose current price is below the old one."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.config import StoreConfig
from shopmcp.domain.models import ProductPage
from shopmcp.envelope import ok
from shopmcp.i18n import L10n
from shopmcp.tools.catalog import SHORT, summarize
from shopmcp.tools.spec import READ_ONLY, ToolSpec, page_meta

PromoSort = Literal["discount", "price_asc", "price_desc"]


def tools(cfg: StoreConfig, adapter: ShopAdapter) -> list[ToolSpec]:
    max_page = cfg.limits.max_page_size
    budget = cfg.limits.max_response_bytes
    default_limit = min(10, max_page)

    async def promotions(
        page: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=max_page)] = default_limit,
        sort: PromoSort = "discount",
    ) -> Annotated[CallToolResult, ProductPage]:
        found, total = await adapter.promotions(page, limit, sort)
        result = ProductPage(
            meta=page_meta(page, limit, total), items=[summarize(p, SHORT) for p in found]
        )
        return ok(result, budget)

    return [
        ToolSpec(
            suffix="promotions",
            requires=frozenset({Capability.PROMOTIONS}),
            scopes=(),
            annotations=READ_ONLY,
            output_model=ProductPage,
            fn=promotions,
            title=L10n(ru="Акции и скидки", en="Promotions"),
            description=L10n(
                ru="Товары со скидкой: текущая цена ниже старой. Постранично.",
                en="Discounted products: the current price is below the old one. Paged.",
            ),
            params={
                "page": L10n(ru="Номер страницы, с 1", en="Page number, from 1"),
                "limit": L10n(
                    ru=f"Товаров на странице, от 1 до {max_page}",
                    en=f"Products per page, 1 to {max_page}",
                ),
                "sort": L10n(
                    ru="discount — сначала самые большие скидки; price_asc, price_desc — по цене",
                    en="discount: the largest discounts first; price_asc, price_desc: by price",
                ),
            },
        )
    ]

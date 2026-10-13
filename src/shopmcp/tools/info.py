"""Store info straight from the config: present in every store."""

from typing import Annotated

from mcp.types import CallToolResult

from shopmcp.config import StoreConfig
from shopmcp.domain.models import StoreInfo
from shopmcp.envelope import ok
from shopmcp.i18n import L10n
from shopmcp.tools.spec import READ_ONLY, ToolSpec


def tools(cfg: StoreConfig) -> list[ToolSpec]:
    info = StoreInfo(
        brand=cfg.brand,
        site=cfg.site,
        phone=cfg.phone,
        delivery=cfg.delivery,
        payment=cfg.payment,
        regions=cfg.regions,
    )

    async def store_info() -> Annotated[CallToolResult, StoreInfo]:
        return ok(info)

    return [
        ToolSpec(
            suffix="store_info",
            requires=frozenset(),
            scopes=(),
            annotations=READ_ONLY,
            output_model=StoreInfo,
            fn=store_info,
            title=L10n(ru="О магазине", en="About the store"),
            description=L10n(
                ru="Сайт, телефон, доставка, оплата и регионы работы магазина.",
                en="Website, phone, delivery, payment and service regions of the store.",
            ),
        )
    ]

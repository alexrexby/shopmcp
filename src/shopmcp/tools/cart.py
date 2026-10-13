"""Cart handoff tool: present in every store that can look products up by id (the cart
is checked against the catalog first); `link_list` is the fallback strategy."""

from decimal import Decimal
from typing import Annotated

from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.cart.strategies import CartStrategy
from shopmcp.config import StoreConfig
from shopmcp.domain.models import CartHandoff, CartLine
from shopmcp.envelope import ok
from shopmcp.errors import InvalidInput
from shopmcp.i18n import L10n
from shopmcp.tools.spec import READ_ONLY, ToolSpec

# A share link creates an object on the store side: not read-only, not idempotent.
CREATES_SHARE_LINK = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def tools(cfg: StoreConfig, adapter: ShopAdapter, cart: CartStrategy) -> list[ToolSpec]:
    # One constant feeds both the schema (maxItems, maximum) and the description: defect 6.
    max_lines = cfg.limits.max_cart_lines
    max_qty = cfg.limits.max_quantity

    class LineIn(BaseModel):
        # No field descriptions: the localized `lines` description covers them.
        model_config = ConfigDict(extra="forbid")
        id: str
        quantity: float = Field(gt=0, le=max_qty)

    async def cart_link_create(
        lines: Annotated[list[LineIn], Field(min_length=1, max_length=max_lines)],
    ) -> Annotated[CallToolResult, CartHandoff]:
        cart_lines = [
            CartLine(product_id=line.id, quantity=Decimal(str(line.quantity))) for line in lines
        ]
        wanted = list(dict.fromkeys(line.product_id for line in cart_lines))
        products = {p.id: p for p in await adapter.get_products(wanted)}
        missing = [i for i in wanted if i not in products]
        if missing:
            raise InvalidInput(details={"missing": missing})
        return ok(await cart.handoff(cart_lines, products))

    return [
        ToolSpec(
            suffix="cart_link_create",
            # Without DETAILS every call would fail: unknown ids are refused before the handoff.
            requires=frozenset({Capability.DETAILS}),
            scopes=(),
            annotations=CREATES_SHARE_LINK if cart.kind == "share_link" else READ_ONLY,
            output_model=CartHandoff,
            fn=cart_link_create,
            title=L10n(ru="Ссылка на корзину", en="Cart link"),
            description=L10n(
                ru=f"Передаёт собранный список товаров на сайт магазина: от 1 до {max_lines} "
                "позиций. Возвращает ссылку, которая наполняет корзину, либо ссылки на карточки "
                "с количеством. Оформление и оплата — на сайте магазина. Несуществующие id "
                "вернутся ошибкой с их перечнем.",
                en=f"Hands the collected list over to the store website: 1 to {max_lines} lines. "
                "Returns a link that fills the cart, or product page links with quantities. "
                "Checkout and payment happen on the store website. Unknown ids return an error "
                "that lists them.",
            ),
            params={
                "lines": L10n(
                    ru=f"Позиции корзины, от 1 до {max_lines}: id товара и количество "
                    f"(больше 0, не больше {max_qty})",
                    en=f"Cart lines, 1 to {max_lines}: product id and quantity "
                    f"(above 0, at most {max_qty})",
                )
            },
        )
    ]

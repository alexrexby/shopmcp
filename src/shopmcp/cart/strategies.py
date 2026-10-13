"""Cart handoff: the agent builds a list, the store gets a link, the human pays on the site."""

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from shopmcp.domain.models import CartHandoff, CartLine, Product

CartKind = Literal["share_link", "url_template", "link_list"]


class CartStrategy(Protocol):
    kind: CartKind

    async def handoff(
        self, lines: Sequence[CartLine], products: Mapping[str, Product]
    ) -> CartHandoff: ...

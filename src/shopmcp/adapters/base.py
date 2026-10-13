"""Adapter contract: a narrow source-of-data interface plus declared capabilities.

A tool exists only if the adapter declares its capability. Adapters raise only
`shopmcp.errors` exceptions and pass every source string through `clean_text()`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from mcp.server.auth.provider import AccessToken
from pydantic import HttpUrl

from shopmcp.capabilities import Capability
from shopmcp.domain.models import (
    CartLine,
    Category,
    Facets,
    Order,
    PickupPoint,
    Product,
)
from shopmcp.errors import NotSupported

Sort = Literal["relevance", "price_asc", "price_desc", "new", "discount"]


@dataclass(frozen=True)
class SearchQuery:
    q: str | None = None
    category_id: str | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    in_stock: bool | None = None
    sort: Sort = "relevance"
    page: int = 1
    limit: int = 10


@dataclass(frozen=True)
class AdapterContext:
    data_dir: Path
    http_timeout: float
    store_id: str
    config_dir: Path = Path(".")  # directory of store.toml: relative paths in it start here


@runtime_checkable
class ShopAdapter(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...
    async def start(self) -> None: ...  # load/index; called from the server lifespan
    async def close(self) -> None: ...
    async def health(self) -> dict[str, Any]: ...  # for /healthz: index size, feed age
    async def search(self, q: SearchQuery) -> tuple[list[Product], int, Facets]: ...
    async def get_products(self, ids: Sequence[str]) -> list[Product]: ...  # absent ids skipped
    async def by_barcode(self, barcode: str) -> Product | None: ...
    async def analogs(self, product_id: str, limit: int) -> list[Product]: ...
    async def categories(self, parent_id: str | None) -> list[Category]: ...
    async def promotions(self, page: int, limit: int, sort: str) -> tuple[list[Product], int]: ...
    async def pickup_points(
        self, city: str | None, page: int, limit: int
    ) -> tuple[list[PickupPoint], int]: ...
    async def create_share_link(self, lines: Sequence[CartLine]) -> HttpUrl: ...
    async def orders(
        self, token: AccessToken, page: int, limit: int
    ) -> tuple[list[Order], int]: ...


class AdapterBase:
    """Defaults for a ShopAdapter: no capabilities, every data method raises NotSupported.

    Subclasses override `capabilities()` and the methods they declare.
    """

    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def health(self) -> dict[str, Any]:
        return {}

    async def search(self, q: SearchQuery) -> tuple[list[Product], int, Facets]:
        raise NotSupported()

    async def get_products(self, ids: Sequence[str]) -> list[Product]:
        raise NotSupported()

    async def by_barcode(self, barcode: str) -> Product | None:
        raise NotSupported()

    async def analogs(self, product_id: str, limit: int) -> list[Product]:
        raise NotSupported()

    async def categories(self, parent_id: str | None) -> list[Category]:
        raise NotSupported()

    async def promotions(self, page: int, limit: int, sort: str) -> tuple[list[Product], int]:
        raise NotSupported()

    async def pickup_points(
        self, city: str | None, page: int, limit: int
    ) -> tuple[list[PickupPoint], int]:
        raise NotSupported()

    async def create_share_link(self, lines: Sequence[CartLine]) -> HttpUrl:
        raise NotSupported()

    async def orders(self, token: AccessToken, page: int, limit: int) -> tuple[list[Order], int]:
        raise NotSupported()

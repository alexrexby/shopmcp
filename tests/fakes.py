"""In-memory ShopAdapter for tests: capabilities are set per test."""

from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from mcp.server.auth.provider import AccessToken
from pydantic import HttpUrl

from shopmcp.adapters.base import AdapterBase, SearchQuery, ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.cart import strategies
from shopmcp.config import StoreConfig
from shopmcp.domain.models import (
    CartLine,
    Category,
    Facets,
    FacetValue,
    Money,
    Order,
    PickupPoint,
    Price,
    Product,
)
from shopmcp.errors import NotSupported
from shopmcp.i18n import Texts


def make_product(id: str, name: str, price: str = "100", **kw: Any) -> Product:
    return Product(
        id=id,
        name=name,
        url=HttpUrl(f"https://shop.example/p/{id}"),
        price=Price(current=Money(amount=Decimal(price))),
        **kw,
    )


DEFAULT_PRODUCTS = [
    make_product(
        "1", "Молоко 3,2%", "89.90", available=True, category_id="10", barcodes=["4600000000017"]
    ),
    Product(
        id="2",
        name="Творог 9%",
        url=HttpUrl("https://shop.example/p/2"),
        price=Price(
            current=Money(amount=Decimal(159)), old=Money(amount=Decimal(199)), discount_percent=20
        ),
        available=True,
        category_id="10",
    ),
    make_product("3", "Хлеб бородинский", "65", available=False, category_id="20"),
]


class FakeAdapter(AdapterBase):
    def __init__(
        self,
        capabilities: Iterable[Capability] = tuple(Capability),
        products: Sequence[Product] = DEFAULT_PRODUCTS,
        categories: Sequence[Category] = (),
        pickup_points: Sequence[PickupPoint] = (),
        orders: Sequence[Order] = (),
    ) -> None:
        self._caps = frozenset(capabilities)
        self.products = {p.id: p for p in products}
        self._categories = list(categories) or [
            Category(id="10", name="Молочное", path=["Молочное"]),
            Category(id="20", name="Хлеб", path=["Хлеб"]),
        ]
        self._pickup_points = list(pickup_points)
        self._orders = list(orders)
        self.started = self.closed = False

    def _need(self, cap: Capability) -> None:
        if cap not in self._caps:
            raise NotSupported()

    def capabilities(self) -> frozenset[Capability]:
        return self._caps

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def health(self) -> dict[str, Any]:
        return {"products": len(self.products)}

    async def search(self, q: SearchQuery) -> tuple[list[Product], int, Facets]:
        self._need(Capability.SEARCH)
        found = [
            p
            for p in self.products.values()
            if (not q.q or q.q.lower() in p.name.lower())
            and (q.category_id is None or p.category_id == q.category_id)
            and (not q.in_stock or p.available)
        ]
        start = (q.page - 1) * q.limit
        counts: dict[str, int] = {}
        for p in found:
            if p.category_id:
                counts[p.category_id] = counts.get(p.category_id, 0) + 1
        facets = Facets(categories=[FacetValue(id=c, name=c, count=n) for c, n in counts.items()])
        return found[start : start + q.limit], len(found), facets

    async def get_products(self, ids: Sequence[str]) -> list[Product]:
        self._need(Capability.DETAILS)
        return [self.products[i] for i in ids if i in self.products]

    async def by_barcode(self, barcode: str) -> Product | None:
        self._need(Capability.BARCODE)
        return next((p for p in self.products.values() if barcode in p.barcodes), None)

    async def analogs(self, product_id: str, limit: int) -> list[Product]:
        self._need(Capability.ANALOGS)
        base = self.products.get(product_id)
        if base is None:
            return []
        same = [
            p
            for p in self.products.values()
            if p.id != product_id and p.category_id == base.category_id
        ]
        return same[:limit]

    async def categories(self, parent_id: str | None) -> list[Category]:
        self._need(Capability.CATEGORIES)
        return [c for c in self._categories if c.parent_id == parent_id]

    async def promotions(self, page: int, limit: int, sort: str) -> tuple[list[Product], int]:
        self._need(Capability.PROMOTIONS)
        promo = [p for p in self.products.values() if p.price.old is not None]
        return promo[(page - 1) * limit : page * limit], len(promo)

    async def pickup_points(
        self, city: str | None, page: int, limit: int
    ) -> tuple[list[PickupPoint], int]:
        self._need(Capability.PICKUP_POINTS)
        found = [p for p in self._pickup_points if city is None or p.city == city]
        return found[(page - 1) * limit : page * limit], len(found)

    async def create_share_link(self, lines: Sequence[CartLine]) -> HttpUrl:
        self._need(Capability.SHARE_LINK)
        ids = ",".join(f"{line.product_id}:{line.quantity}" for line in lines)
        return HttpUrl(f"https://shop.example/?cart={ids}")

    async def orders(self, token: AccessToken, page: int, limit: int) -> tuple[list[Order], int]:
        self._need(Capability.ORDERS)
        return self._orders[(page - 1) * limit : page * limit], len(self._orders)


def cart_strategy(c: StoreConfig, adapter: ShopAdapter) -> strategies.CartStrategy:
    """The store's cart strategy, as build_app creates it."""
    return strategies.create(
        c.cart, adapter, Texts(c.locale), site=c.site, public_url=c.server.public_url, store_id=c.id
    )

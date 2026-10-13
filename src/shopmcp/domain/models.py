"""Canonical domain model. Adapters map their source into it; the core never sees raw data.

Every optional field has a `None` default: `envelope.ok()` dumps with `exclude_none`,
and the SDK validates structuredContent against the output model, so a required
nullable field would fail validation once its `None` is dropped.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PlainSerializer

# Decimal on the wire is a JSON number, not a string.
Number = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(Model):
    amount: Number
    currency: str = "RUB"


class Price(Model):
    current: Money
    old: Money | None = None
    discount_percent: int | None = None


class Rating(Model):
    value: float
    count: int | None = None


class Category(Model):
    id: str
    name: str
    parent_id: str | None = None
    path: list[str] = []
    product_count: int | None = None


class Param(Model):
    name: str
    value: str
    unit: str | None = None


class Product(Model):
    id: str
    name: str
    url: HttpUrl
    price: Price
    available: bool | None = None
    category_id: str | None = None
    category_path: list[str] = []
    vendor: str | None = None
    barcodes: list[str] = []
    pictures: list[HttpUrl] = []
    description: str | None = None
    params: list[Param] = []
    unit: str | None = None
    rating: Rating | None = None


class ProductSummary(Model):
    """Projection of Product for lists: `mode` short|full|custom picks the fields."""

    id: str
    name: str
    url: HttpUrl | None = None
    price: Price | None = None
    available: bool | None = None
    category_id: str | None = None
    vendor: str | None = None
    picture: HttpUrl | None = None
    description: str | None = None
    params: list[Param] | None = None


class FacetValue(Model):
    id: str
    name: str
    count: int


class Facets(Model):
    categories: list[FacetValue] = []  # top-N only; the full tree is the categories tool


class PageMeta(Model):
    q: str | None = None
    page: int
    limit: int
    total: int
    pages: int
    has_more: bool
    truncated: bool = False


class ProductPage(Model):
    meta: PageMeta
    items: list[ProductSummary]
    facets: Facets | None = None


class ProductDetails(Model):
    items: list[Product]
    missing: list[str] = []


class CategoryList(Model):
    items: list[Category]


class PickupPoint(Model):
    id: str
    name: str
    address: str
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    hours: str | None = None
    phone: str | None = None


class PickupPointPage(Model):
    meta: PageMeta
    items: list[PickupPoint]


class CartLine(Model):
    product_id: str
    quantity: Annotated[Number, Field(gt=0)]  # upper bound comes from config, checked by the tool


class CartLinkItem(Model):
    product_id: str
    name: str
    quantity: Number
    url: HttpUrl


class CartHandoff(Model):
    kind: Literal["share_link", "url_template", "link_list"]
    url: HttpUrl | None = None
    links: list[CartLinkItem] = []
    note: str | None = None


class OrderItem(Model):
    product_id: str | None = None
    name: str
    quantity: Number
    price: Money | None = None


class Order(Model):
    id: str
    number: str | None = None
    created_at: datetime
    status: str
    total: Money
    items: list[OrderItem] = []


class OrderPage(Model):
    meta: PageMeta
    items: list[Order]


class StoreInfo(Model):
    brand: str
    site: HttpUrl
    phone: str | None = None
    delivery: str | None = None
    payment: str | None = None
    regions: list[str] = []

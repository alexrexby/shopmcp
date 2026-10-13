"""Catalog, promo, pickup, account and info tools, called directly against FakeAdapter."""

from typing import Any

import pytest
from fakes import FakeAdapter, make_product
from test_tools_spec import cfg

from shopmcp.capabilities import Capability
from shopmcp.cart import strategies
from shopmcp.config import StoreConfig
from shopmcp.domain.models import Category, Facets, FacetValue, PickupPoint
from shopmcp.errors import InvalidInput, NotFound, Unauthorized
from shopmcp.i18n import Texts
from shopmcp.tools.catalog import MAX_FACETS, top_facets
from shopmcp.tools.spec import build_tools


def tool(adapter: FakeAdapter, suffix: str, c: StoreConfig | None = None) -> Any:
    c = c or cfg()
    cart = strategies.create(c.cart, adapter, Texts(c.locale))
    return next(s.fn for s in build_tools(c, adapter, cart) if s.suffix == suffix)


async def call(adapter: FakeAdapter, suffix: str, c: StoreConfig | None = None, **kw: Any):
    return (await tool(adapter, suffix, c)(**kw)).structured_content


@pytest.mark.anyio
async def test_search_short_mode_and_meta():
    res = await call(FakeAdapter(), "products_search", q="Молоко")
    assert res["meta"] == {
        "q": "Молоко",
        "page": 1,
        "limit": 10,
        "total": 1,
        "pages": 1,
        "has_more": False,
        "truncated": False,
    }
    [item] = res["items"]
    assert set(item) == {"id", "name", "url", "price", "available"}
    assert res["facets"]["categories"] == [{"id": "10", "name": "10", "count": 1}]


@pytest.mark.anyio
async def test_search_custom_projection():
    res = await call(FakeAdapter(), "products_search", mode="custom", fields=["vendor", "price"])
    assert {k for item in res["items"] for k in item} <= {"id", "name", "vendor", "price"}
    assert all("price" in item for item in res["items"])


@pytest.mark.anyio
async def test_search_custom_without_fields_is_invalid():
    with pytest.raises(InvalidInput):
        await call(FakeAdapter(), "products_search", mode="custom")


@pytest.mark.anyio
async def test_search_price_range_is_checked():
    with pytest.raises(InvalidInput):
        await call(FakeAdapter(), "products_search", price_min=200, price_max=100)


@pytest.mark.anyio
async def test_search_paging_and_has_more():
    products = [make_product(str(i), f"Товар {i}") for i in range(25)]
    res = await call(FakeAdapter(products=products), "products_search", page=2, limit=10)
    assert [i["id"] for i in res["items"]] == [str(i) for i in range(10, 20)]
    assert (res["meta"]["pages"], res["meta"]["has_more"]) == (3, True)


@pytest.mark.anyio
async def test_search_truncates_to_response_budget():
    products = [make_product(str(i), "Очень длинное название товара " * 5) for i in range(20)]
    c = cfg(limits__max_response_bytes=2048)
    fn = tool(FakeAdapter(products=products), "products_search", c)
    res = await fn(limit=20, mode="full")
    assert len(res.content[0].text.encode()) <= 2048
    meta = res.structured_content["meta"]
    assert meta["truncated"] and meta["has_more"] and meta["total"] == 20
    assert 0 < len(res.structured_content["items"]) < 20


def test_facets_are_capped():
    facets = Facets(categories=[FacetValue(id=str(i), name=str(i), count=i) for i in range(30)])
    top = top_facets(facets).categories
    assert len(top) == MAX_FACETS and top[0].count == 29


@pytest.mark.anyio
async def test_details_reports_missing():
    res = await call(FakeAdapter(), "product_details", ids=["2", "__nope__", "2"])
    assert [p["id"] for p in res["items"]] == ["2"] and res["missing"] == ["__nope__"]


@pytest.mark.anyio
async def test_barcode_found_and_not_found():
    res = await call(FakeAdapter(), "product_by_barcode", barcode="4600000000017")
    assert res["id"] == "1"
    with pytest.raises(NotFound):
        await call(FakeAdapter(), "product_by_barcode", barcode="0000000000000")


@pytest.mark.anyio
async def test_analogs_page():
    res = await call(FakeAdapter(), "product_analogs", id="1", limit=5)
    assert [p["id"] for p in res["items"]] == ["2"] and res["meta"]["total"] == 1


@pytest.mark.anyio
async def test_categories_roots_and_children():
    cats = [Category(id="1", name="Еда"), Category(id="2", name="Молочное", parent_id="1")]
    adapter = FakeAdapter(categories=cats)
    assert [c["id"] for c in (await call(adapter, "categories"))["items"]] == ["1"]
    assert [c["id"] for c in (await call(adapter, "categories", parent_id="1"))["items"]] == ["2"]


@pytest.mark.anyio
async def test_promotions_only_discounted():
    res = await call(FakeAdapter(), "promotions")
    assert [p["id"] for p in res["items"]] == ["2"]
    assert res["items"][0]["price"]["old"]["amount"] == 199


@pytest.mark.anyio
async def test_pickup_points_by_city():
    points = [
        PickupPoint(id="a", name="Склад", address="ул. 1", city="Москва"),
        PickupPoint(id="b", name="Склад", address="ул. 2", city="Тула"),
    ]
    res = await call(FakeAdapter(pickup_points=points), "pickup_points", city="Тула")
    assert [p["id"] for p in res["items"]] == ["b"] and res["meta"]["total"] == 1


@pytest.mark.anyio
async def test_orders_without_token_is_unauthorized():
    with pytest.raises(Unauthorized):
        await call(FakeAdapter(), "orders_history")


@pytest.mark.anyio
async def test_store_info_from_config():
    res = await call(FakeAdapter(()), "store_info")
    assert res["brand"] == "Демо-маркет" and res["regions"] == ["Москва", "Московская область"]


def test_unsupported_capability_is_not_registered():
    adapter = FakeAdapter({Capability.SEARCH})
    with pytest.raises(StopIteration):
        tool(adapter, "product_details")

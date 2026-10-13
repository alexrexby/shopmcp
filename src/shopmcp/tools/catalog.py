"""Catalog tools: search, details, barcode lookup, analogs, category tree."""

from decimal import Decimal
from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from shopmcp.adapters.base import SearchQuery, ShopAdapter, Sort
from shopmcp.capabilities import Capability
from shopmcp.config import StoreConfig
from shopmcp.domain.models import (
    CategoryList,
    Facets,
    Product,
    ProductDetails,
    ProductPage,
    ProductSummary,
)
from shopmcp.envelope import ok, size
from shopmcp.errors import InvalidInput, NotFound
from shopmcp.i18n import L10n
from shopmcp.tools.spec import READ_ONLY, ToolSpec, page_meta

MAX_DETAILS_IDS = 20
MAX_FACETS = 10  # top categories in a search; the full tree is the categories tool

Mode = Literal["short", "full", "custom"]
SummaryField = Literal[
    "url", "price", "available", "category_id", "vendor", "picture", "description", "params"
]
SHORT: tuple[SummaryField, ...] = ("url", "price", "available")
FULL: tuple[SummaryField, ...] = (
    "url", "price", "available", "category_id", "vendor", "picture", "description", "params"
)  # fmt: skip


def summarize(p: Product, fields: tuple[SummaryField, ...] | list[SummaryField]) -> ProductSummary:
    """ProductSummary with `id`, `name` and only the requested fields; the rest stay None."""
    values = {
        "url": p.url,
        "price": p.price,
        "available": p.available,
        "category_id": p.category_id,
        "vendor": p.vendor,
        "picture": p.pictures[0] if p.pictures else None,
        "description": p.description,
        "params": p.params or None,
    }
    return ProductSummary(id=p.id, name=p.name, **{f: values[f] for f in fields})


def projection(mode: Mode, fields: list[SummaryField] | None) -> tuple[SummaryField, ...]:
    if mode == "short":
        return SHORT
    if mode == "full":
        return FULL
    if not fields:
        raise InvalidInput(details={"param": "fields", "reason": "required_for_mode_custom"})
    return tuple(dict.fromkeys(fields))


def top_facets(facets: Facets) -> Facets:
    ranked = sorted(facets.categories, key=lambda f: f.count, reverse=True)
    return Facets(categories=ranked[:MAX_FACETS])


def tools(cfg: StoreConfig, adapter: ShopAdapter) -> list[ToolSpec]:
    max_page = cfg.limits.max_page_size
    budget = cfg.limits.max_response_bytes
    Limit = Annotated[int, Field(ge=1, le=max_page)]
    default_limit = min(10, max_page)
    default_analogs = min(5, max_page)

    async def products_search(
        q: str | None = None,
        category_id: str | None = None,
        price_min: Annotated[float, Field(ge=0)] | None = None,
        price_max: Annotated[float, Field(ge=0)] | None = None,
        in_stock: bool | None = None,
        sort: Sort = "relevance",
        page: Annotated[int, Field(ge=1)] = 1,
        limit: Limit = default_limit,
        mode: Mode = "short",
        fields: list[SummaryField] | None = None,
    ) -> Annotated[CallToolResult, ProductPage]:
        if price_min is not None and price_max is not None and price_min > price_max:
            raise InvalidInput(details={"param": "price_min", "reason": "greater_than_price_max"})
        shown = projection(mode, fields)
        query = SearchQuery(
            q=(q.strip() or None) if q else None,
            category_id=category_id,
            price_min=None if price_min is None else Decimal(str(price_min)),
            price_max=None if price_max is None else Decimal(str(price_max)),
            in_stock=in_stock,
            sort=sort,
            page=page,
            limit=limit,
        )
        found, total, facets = await adapter.search(query)
        result = ProductPage(
            meta=page_meta(page, limit, total, query.q),
            items=[summarize(p, shown) for p in found],
            facets=top_facets(facets) if facets.categories else None,
        )
        return ok(result, budget)

    async def product_details(
        ids: Annotated[list[str], Field(min_length=1, max_length=MAX_DETAILS_IDS)],
    ) -> Annotated[CallToolResult, ProductDetails]:
        wanted = list(dict.fromkeys(ids))
        found = await adapter.get_products(wanted)
        if not found:  # nothing to show is an error, a partial answer is not
            raise NotFound(details={"missing": wanted})
        have = {p.id for p in found}
        result = ProductDetails(items=found, missing=[i for i in wanted if i not in have])
        # Full cards are big: over the budget the last ones move to `omitted`.
        while result.items and size(result) > budget:
            result.omitted.insert(0, result.items.pop().id)
        return ok(result)

    async def product_by_barcode(
        barcode: Annotated[str, Field(pattern=r"^(\d{8}|\d{12,14})$")],
    ) -> Annotated[CallToolResult, Product]:
        product = await adapter.by_barcode(barcode)
        if product is None:
            raise NotFound(details={"barcode": barcode})
        return ok(product)

    async def product_analogs(
        id: str, limit: Limit = default_analogs
    ) -> Annotated[CallToolResult, ProductPage]:
        found = await adapter.analogs(id, limit)
        result = ProductPage(
            meta=page_meta(1, limit, len(found)), items=[summarize(p, SHORT) for p in found]
        )
        return ok(result, budget)

    async def categories(parent_id: str | None = None) -> Annotated[CallToolResult, CategoryList]:
        return ok(CategoryList(items=await adapter.categories(parent_id)))

    fields_doc = ", ".join(FULL)
    return [
        ToolSpec(
            suffix="products_search",
            requires=frozenset({Capability.SEARCH}),
            scopes=(),
            annotations=READ_ONLY,
            output_model=ProductPage,
            fn=products_search,
            title=L10n(ru="Поиск товаров", en="Product search"),
            description=L10n(
                ru="Ищет товары в каталоге магазина по запросу и фильтрам. Возвращает страницу "
                "товаров, метаданные пагинации (total, pages, has_more) и топ категорий "
                "найденного. Все параметры необязательны.",
                en="Searches the store catalog by query and filters. Returns a page of products, "
                "pagination metadata (total, pages, has_more) and the top categories of the "
                "results. Every parameter is optional.",
            ),
            params={
                "q": L10n(ru="Поисковый запрос", en="Search query"),
                "category_id": L10n(
                    ru="Только эта категория и её подкатегории",
                    en="Only this category and its subcategories",
                ),
                "price_min": L10n(ru="Минимальная цена", en="Minimum price"),
                "price_max": L10n(ru="Максимальная цена", en="Maximum price"),
                "in_stock": L10n(ru="Только товары в наличии", en="Only products in stock"),
                "sort": L10n(ru="Порядок сортировки", en="Sort order"),
                "page": L10n(ru="Номер страницы, с 1", en="Page number, from 1"),
                "limit": L10n(
                    ru=f"Товаров на странице, от 1 до {max_page}",
                    en=f"Products per page, 1 to {max_page}",
                ),
                "mode": L10n(
                    ru="short — цена, наличие, ссылка; full — все поля; custom — поля из fields",
                    en="short: price, availability, link; full: every field; "
                    "custom: the fields listed in fields",
                ),
                "fields": L10n(
                    ru=f"Поля для mode=custom: {fields_doc}",
                    en=f"Fields for mode=custom: {fields_doc}",
                ),
            },
        ),
        ToolSpec(
            suffix="product_details",
            requires=frozenset({Capability.DETAILS}),
            scopes=(),
            annotations=READ_ONLY,
            output_model=ProductDetails,
            fn=product_details,
            title=L10n(ru="Карточки товаров", en="Product details"),
            description=L10n(
                ru=f"Полные карточки товаров по id, от 1 до {MAX_DETAILS_IDS} за вызов. "
                "Ненайденные id перечислены в missing; если не найден ни один — ошибка "
                "not_found. Карточки, не поместившиеся в ответ, перечислены в omitted: "
                "запросите их отдельным вызовом.",
                en=f"Full product cards by id, 1 to {MAX_DETAILS_IDS} per call. "
                "Ids that were not found are listed in missing; if none is found, the result "
                "is a not_found error. Cards that did not fit into the response are listed in "
                "omitted: request them in another call.",
            ),
            params={
                "ids": L10n(
                    ru=f"id товаров, от 1 до {MAX_DETAILS_IDS}",
                    en=f"Product ids, 1 to {MAX_DETAILS_IDS}",
                )
            },
        ),
        ToolSpec(
            suffix="product_by_barcode",
            requires=frozenset({Capability.BARCODE}),
            scopes=(),
            annotations=READ_ONLY,
            output_model=Product,
            fn=product_by_barcode,
            title=L10n(ru="Товар по штрихкоду", en="Product by barcode"),
            description=L10n(
                ru="Полная карточка товара по штрихкоду EAN-8, UPC-A, EAN-13 или GTIN-14.",
                en="Full product card by an EAN-8, UPC-A, EAN-13 or GTIN-14 barcode.",
            ),
            params={
                "barcode": L10n(
                    ru="Штрихкод: 8 или от 12 до 14 цифр", en="Barcode: 8 or 12 to 14 digits"
                )
            },
        ),
        ToolSpec(
            suffix="product_analogs",
            requires=frozenset({Capability.ANALOGS}),
            scopes=(),
            annotations=READ_ONLY,
            output_model=ProductPage,
            fn=product_analogs,
            title=L10n(ru="Аналоги товара", en="Product analogs"),
            description=L10n(
                ru="Похожие товары из той же категории, ближайшие по цене.",
                en="Similar products from the same category, closest in price first.",
            ),
            params={
                "id": L10n(ru="id исходного товара", en="Id of the original product"),
                "limit": L10n(
                    ru=f"Сколько аналогов вернуть, от 1 до {max_page}",
                    en=f"How many analogs to return, 1 to {max_page}",
                ),
            },
        ),
        ToolSpec(
            suffix="categories",
            requires=frozenset({Capability.CATEGORIES}),
            scopes=(),
            annotations=READ_ONLY,
            output_model=CategoryList,
            fn=categories,
            title=L10n(ru="Категории каталога", en="Catalog categories"),
            description=L10n(
                ru="Дочерние категории указанной категории; без parent_id — корневые. "
                "id категории подходит в фильтр category_id поиска.",
                en="Child categories of the given category; without parent_id, the root ones. "
                "A category id fits the category_id filter of the search.",
            ),
            params={
                "parent_id": L10n(ru="id родительской категории", en="Id of the parent category")
            },
        ),
    ]

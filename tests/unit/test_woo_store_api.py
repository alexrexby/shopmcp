"""Store API reader over a recorded shop: mapping, prices, pages, failures."""

from decimal import Decimal

import pytest
from woo_shop import FAULTS, SITE, Response, WooShop

from shopmcp.adapters.woocommerce import store_api
from shopmcp.adapters.yml.feed import FeedError, FeedStats
from shopmcp.domain.models import Category, Product


@pytest.fixture
def shop(monkeypatch):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0)
    return WooShop().install(monkeypatch)


def read(max_pages: int = 50) -> tuple[list[Category], dict[str, Product], FeedStats]:
    stats = FeedStats()
    items = list(store_api.read(SITE, stats, timeout=5, max_pages=max_pages, max_seconds=60))
    categories = [i for i in items if isinstance(i, Category)]
    products = {i.id: i for i in items if isinstance(i, Product)}
    return categories, products, stats


# --- mapping ------------------------------------------------------------------


def test_one_product_per_purchasable_unit(shop):
    _, products, stats = read()
    # 103 is variable: its variations 201/202 are the products. 104 grouped, 105 external,
    # 106 without a price, 108 variable without variations and 299 (its parent is not in
    # the catalog) are skipped.
    assert sorted(products) == ["101", "102", "107", "201", "202"]
    assert (stats.offers, stats.skipped) == (10, 5)


def test_prices_are_minor_units_of_the_currency(shop):
    _, products, _ = read()
    bottle = products["101"].price
    assert bottle.current.amount == Decimal("1299.00") and bottle.current.currency == "RUB"
    assert bottle.old is not None and bottle.old.amount == Decimal("1499.00")
    assert bottle.discount_percent == 13
    assert products["107"].price.current.amount == Decimal("450.50")
    assert products["102"].price.old is None  # regular == price: not a discount


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        ({"price": "115", "regular_price": "115", "currency_minor_unit": 0}, ("115", None)),
        ({"price": "1899", "regular_price": "2499", "currency_minor_unit": 2}, ("18.99", "24.99")),
        ({"price": "1500", "regular_price": "1000", "currency_minor_unit": 3}, ("1.500", None)),
        ({"price": "0", "regular_price": "0", "currency_minor_unit": 2}, ("0", None)),
    ],
)
def test_price(prices, expected):
    current, old, currency = store_api._price({**prices, "currency_code": "usd"})
    assert (current, old) == (Decimal(expected[0]), expected[1] and Decimal(expected[1]))
    assert currency == "USD"


@pytest.mark.parametrize(
    "bad",
    [
        {"price": ""},
        {"price": "-5"},
        {"price": "12.50"},
        {"price": "1e3"},
        {"price": 1899},
        {"currency_minor_unit": None},
        {"currency_minor_unit": True},
        {"currency_minor_unit": 7},
        {"currency_code": "RU"},
        {"currency_code": None},
    ],
)
def test_invalid_price_skips_the_product(bad):
    good = {"price": "1899", "regular_price": "1899", "currency_minor_unit": 2,
            "currency_code": "RUB"}  # fmt: skip
    assert store_api._price(good) is not None
    assert store_api._price({**good, **bad}) is None


def test_variation_takes_name_categories_and_text_from_its_product(shop):
    _, products, _ = read()
    red = products["201"]
    assert red.name == "Футболка — Цвет: Красный"
    assert red.group_id == "103"
    assert red.category_id == "12" and red.category_path == ["Аксессуары"]
    assert red.description == "Футболка из хлопка."
    assert [(p.name, p.value) for p in red.params] == [("Материал", "Хлопок"), ("Цвет", "Красный")]
    assert str(red.pictures[0]).endswith("/futbolka.jpg")
    # `&#038;` in a permalink is an escaped `&`, as the Store API writes it.
    assert str(red.url) == "https://shop.example/product/futbolka/?attribute_pa_cvet=krasnyj&utm=x"
    assert red.price.discount_percent == 11 and red.available is True


def test_variation_keeps_its_own_text_and_picture(shop):
    _, products, _ = read()
    blue = products["202"]
    assert blue.description == "Синяя, как море."
    assert str(blue.pictures[0]).endswith("/futbolka-sinyaya.jpg")
    assert [(p.name, p.value) for p in blue.params][-1] == ("Цвет", "Синий")


def test_out_of_stock(shop):
    _, products, _ = read()
    assert products["102"].available is False
    assert products["202"].available is False
    assert products["101"].available is True


def test_html_and_entities_are_cleaned(shop):
    categories, products, _ = read()
    bottle = products["101"]
    assert bottle.name == "Бутылка для воды – 1 л"
    assert bottle.description == "Прочная бутылка для воды."
    assert bottle.vendor == "Аква & Ко"
    assert [(p.name, p.value) for p in bottle.params] == [("Объём", "1 л")]
    assert products["102"].description == "Держит тепло 6 часов"  # short_description
    assert {c.id: c.name for c in categories}["13"] == "Чай & кофе"


def test_categories_tree_and_the_deepest_category_wins(shop):
    categories, products, _ = read()
    by_id = {c.id: c for c in categories}
    assert by_id["11"].parent_id == "10" and by_id["11"].path == ["Посуда", "Бутылки"]
    assert by_id["10"].parent_id is None
    # 101 is in "Посуда" and in its subcategory "Бутылки".
    assert products["101"].category_id == "11"
    assert products["101"].category_path == ["Посуда", "Бутылки"]


# --- pages --------------------------------------------------------------------


def test_every_page_is_read(shop, monkeypatch):
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    _, products, _ = read()
    assert sorted(products) == ["101", "102", "107", "201", "202"]
    # 4 categories, 8 products, 3 variations by 2: X-WP-TotalPages ends each listing.
    assert shop.pages("categories") == [1, 2]
    assert shop.pages("products") == [1, 2, 3, 4]
    assert shop.pages("variations") == [1, 2]


def test_pages_without_total_header_end_on_a_short_page(monkeypatch):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop = WooShop(total_pages_header=False).install(monkeypatch)
    _, products, _ = read()
    assert len(products) == 5
    assert shop.pages("products") == [1, 2, 3, 4, 5]  # 8 by 2 + an empty page
    assert shop.pages("categories") == [1, 2, 3]  # 2 + 2 + an empty page


def test_empty_page_mid_listing_ends_it(shop, monkeypatch):
    # The catalog shrank while it was read: page 2 of 4 is already past the end.
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop.faults[("products", 2)] = lambda url: Response(b"[]")
    _, products, _ = read()
    assert shop.pages("products") == [1, 2]
    assert sorted(products) == ["101", "102"]
    assert shop.pages("variations") == []  # their product was on the lost pages


def test_empty_catalog(shop):
    shop.listings["products"] = []
    _, products, stats = read()
    assert products == {} and stats.offers == 0
    assert shop.pages("variations") == []  # no variable product, nothing to ask


def test_too_many_pages_fails_before_reading_them(shop, monkeypatch):
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    with pytest.raises(FeedError, match="4 pages of 2, adapter.max_pages is 3"):
        read(max_pages=3)
    assert shop.pages("products") == [1]


def test_too_many_pages_without_total_header(monkeypatch):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    WooShop(total_pages_header=False).install(monkeypatch)
    with pytest.raises(FeedError, match="more than adapter.max_pages = 2"):
        read(max_pages=2)


# --- failures -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("500", "HTTP 500"),
        ("html", "expected application/json, got text/html"),
        ("cut", "IncompleteRead"),
        ("not_json", "invalid JSON"),
        ("object", "expected a JSON list"),
    ],
)
def test_a_failed_page_fails_the_whole_read(shop, monkeypatch, fault, message):
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop.faults[("products", 2)] = FAULTS[fault]
    with pytest.raises(FeedError, match=message) as e:
        read()
    # Logged as is: path and page, never the query string.
    assert "/wp-json/wc/store/v1/products" in str(e.value) and "?" not in str(e.value)


def test_page_size_is_capped(shop, monkeypatch):
    monkeypatch.setattr(store_api, "MAX_PAGE_BYTES", 1000)
    with pytest.raises(FeedError, match="exceeds 1000 bytes"):
        read()


def test_whole_read_is_bounded_in_time(shop, monkeypatch):
    clock = iter([0.0, 0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(store_api.time, "monotonic", lambda: next(clock, 100.0))
    with pytest.raises(FeedError, match="exceeded 60 s"):
        read()


def test_only_paced_keyless_gets(shop, monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0.5)
    monkeypatch.setattr(store_api.time, "sleep", pauses.append)
    read()
    assert {r.get_method() for r in shop.requests} == {"GET"}
    assert all(
        r.get_header("Cookie") is None and r.get_header("Authorization") is None
        for r in shop.requests
    )
    assert pauses == [0.5] * (len(shop.requests) - 1)
    assert len(shop.requests) == 3  # categories, products, variations

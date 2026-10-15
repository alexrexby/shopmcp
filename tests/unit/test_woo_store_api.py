"""Store API reader over a recorded shop: mapping, prices, pages, failures."""

import json
import urllib.request
from decimal import Decimal

import pytest
from woo_shop import FAULTS, SITE, Response, WooShop

from shopmcp.adapters.woocommerce import store_api
from shopmcp.adapters.yml.feed import FeedError, FeedStats
from shopmcp.domain.models import Category, Product


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    """Every pause of a read, none of them slept."""
    out: list[float] = []
    monkeypatch.setattr(store_api, "sleep", out.append)
    return out


@pytest.fixture
def shop(monkeypatch, pauses):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0)
    return WooShop().install(monkeypatch)


def read(
    max_pages: int = 50, max_seconds: float = 60, max_bytes: int = 2**30
) -> tuple[list[Category], dict[str, Product], FeedStats]:
    stats = FeedStats()
    items = list(
        store_api.read(
            SITE,
            stats,
            timeout=5,
            max_pages=max_pages,
            max_seconds=max_seconds,
            max_bytes=max_bytes,
        )
    )
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


def test_pages_without_total_header_end_on_a_short_page(monkeypatch, pauses):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop = WooShop(total_pages_header=False).install(monkeypatch)
    _, products, _ = read()
    assert len(products) == 5
    assert shop.pages("products") == [1, 2, 3, 4, 5]  # 8 by 2 + an empty page
    assert shop.pages("categories") == [1, 2, 3]  # 2 + 2 + an empty page


def test_empty_page_mid_listing_fails_the_read(shop, monkeypatch):
    # Review H1: page 2 of 4 empty is a cache or a plugin, not the end of the catalog;
    # taken as the end, it replaced the index with 2 products of 8 and no variations.
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop.faults[("products", 2)] = lambda url: Response(b"[]")
    with pytest.raises(FeedError, match="products page 2: 0 items, X-WP-TotalPages is 4"):
        read()
    assert shop.pages("variations") == []


def test_listing_short_of_x_wp_total_fails_the_read(monkeypatch, pauses):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop = WooShop(total_pages_header=False).install(monkeypatch)
    shop.faults[("products", 2)] = lambda url: Response(b"[]", headers={"X-WP-Total": "8"})
    with pytest.raises(FeedError, match="products: 2 items read, X-WP-Total is 8"):
        read()


def test_a_product_deleted_while_reading_is_not_a_failure(shop, monkeypatch):
    # Deleted from a page already read: the next ones shift, the last but one comes short.
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    page3 = json.dumps(shop.listings["products"][4:5]).encode()
    shop.faults[("products", 3)] = lambda url: Response(page3)
    _, products, _ = read()
    assert shop.pages("products") == [1, 2, 3]
    assert {"101", "102", "201", "202"} <= set(products)


def test_empty_catalog_fails_the_read(shop):
    # Not an empty shop served over the previous index (review H1).
    shop.listings["products"] = []
    with pytest.raises(FeedError, match="/products: the catalog is empty"):
        read()
    assert shop.pages("variations") == []


def test_too_many_pages_fails_before_reading_them(shop, monkeypatch):
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    with pytest.raises(FeedError, match="4 pages of 2, adapter.max_pages is 3"):
        read(max_pages=3)
    assert shop.pages("products") == [1]


def test_too_many_pages_without_total_header(monkeypatch, pauses):
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


@pytest.mark.parametrize("fault", ["503", "429", "timeout", "reset", "cut"])
def test_a_transient_failure_is_tried_again(shop, monkeypatch, pauses, fault):
    # Review M1: one 503 among hundreds of pages failed the whole read.
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop.flaky[("products", 2)] = [FAULTS[fault]]
    _, products, _ = read()
    assert len(products) == 5
    assert shop.pages("products") == [1, 2, 2, 3, 4]
    # Retry-After is honoured; otherwise the first backoff.
    assert [p for p in pauses if p] == [7.0 if fault == "429" else store_api.RETRY_PAUSE]


def test_transient_failures_are_tried_a_bounded_number_of_times(shop, monkeypatch, pauses):
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop.faults[("products", 2)] = FAULTS["503"]
    with pytest.raises(FeedError, match="products page 2: HTTP 503"):
        read()
    assert shop.pages("products") == [1, 2, 2, 2]
    assert [p for p in pauses if p] == [store_api.RETRY_PAUSE, 2 * store_api.RETRY_PAUSE]


@pytest.mark.parametrize("fault", ["500", "404", "html", "not_json"])
def test_other_failures_are_not_tried_again(shop, monkeypatch, fault):
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop.faults[("products", 2)] = FAULTS[fault]
    with pytest.raises(FeedError):
        read()
    assert shop.pages("products") == [1, 2]


def test_a_long_retry_after_is_not_waited_for(shop, monkeypatch, pauses):
    from woo_shop import status

    shop.faults[("products", 1)] = status(429, retry_after="3600")
    with pytest.raises(FeedError, match="HTTP 429"):
        read()
    assert shop.pages("products") == [1] and not any(pauses)


def test_page_size_is_capped(shop, monkeypatch):
    monkeypatch.setattr(store_api, "MAX_PAGE_BYTES", 1000)
    with pytest.raises(FeedError, match="exceeds 1000 bytes"):
        read()


def test_default_page_cap_is_a_few_megabytes(shop):
    # Review V1: 16 MB of `[[],[],...]` held 416 MB after json.loads.
    huge = [{"id": i, "name": "x" * 1000, "parent": 0} for i in range(1, 5001)]
    shop.faults[("categories", 1)] = lambda url: Response(json.dumps(huge).encode())
    with pytest.raises(FeedError, match="exceeds 4194304 bytes"):
        read()


def test_whole_read_is_capped_in_bytes(shop, monkeypatch):
    # Review V1: a shop can send max_pages pages of bloated items; the cap is per read.
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    shop.listings["categories"] = [
        {"id": i, "name": "x" * 100_000, "parent": 0} for i in range(1, 41)
    ]
    with pytest.raises(FeedError, match="exceeds 1 MB, raise adapter.max_feed_mb"):
        read(max_bytes=2**20)
    assert len(shop.pages("categories")) <= 6  # 200 KB a page: stopped at the cap


def test_what_is_kept_between_pages_is_capped(shop):
    # Review V1: a variable product's summary is kept until its variations are read.
    long = "y" * 1000
    product = {
        "id": 1,
        "name": long,
        "attributes": [
            {
                "name": f"{a}{long}",
                "has_variations": a == 0,
                "terms": [{"slug": f"{t}{long}", "name": f"{t}{long}"} for t in range(300)],
            }
            for a in range(40)
        ],
        "variations": [
            {"id": v, "attributes": [{"name": f"a{x}", "value": long} for x in range(40)]}
            for v in range(1, 1201)
        ],
    }
    parent = store_api._parent(product)
    assert parent is not None and len(parent.name) == store_api.MAX_NAME
    assert len(parent.terms) == store_api.MAX_ATTRIBUTES
    assert all(len(t) <= store_api.MAX_TERMS for t in parent.terms.values())
    assert all(len(k) <= store_api.MAX_NAME for t in parent.terms.values() for k in t)
    assert len(parent.axes) == store_api.MAX_VARIATIONS
    axes = parent.axes[1]
    assert len(axes) == store_api.MAX_ATTRIBUTES
    assert all(len(v) == store_api.MAX_NAME for _, v in axes)
    assert all(len(p.value) <= store_api.MAX_PARAM for p in parent.params)
    shop.listings["categories"] = [{"id": 1, "name": long, "parent": 0}]
    categories, _, _ = read()
    assert len(categories[0].name) == store_api.MAX_NAME


def test_whole_read_is_bounded_in_time(shop, monkeypatch):
    clock = Clock(monkeypatch, shop, step=100.0)
    with pytest.raises(FeedError, match="exceeded 60 s"):
        read()
    assert clock.now == 100.0  # the first answer


def test_a_listing_that_cannot_fit_the_budget_fails_before_it_is_read(shop, monkeypatch):
    # Review M2: at 25 s an answer the 4 product pages need 75 s, 45 s are left of 120.
    monkeypatch.setattr(store_api, "PER_PAGE", 2)
    Clock(monkeypatch, shop, step=25.0)
    with pytest.raises(FeedError, match="too large for the read budget, raise adapter.max_minutes"):
        read(max_seconds=120)
    assert shop.pages("products") == [1]


class Clock:
    """`time.monotonic` that moves `step` seconds with each answer of the shop."""

    def __init__(self, monkeypatch, shop: WooShop, step: float) -> None:
        self.now = 0.0
        monkeypatch.setattr(store_api.time, "monotonic", lambda: self.now)

        def urlopen(req, *args, **kwargs):
            self.now += step
            return shop.urlopen(req, *args, **kwargs)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_only_paced_keyless_gets(shop, monkeypatch, pauses):
    monkeypatch.setattr(store_api, "REQUEST_INTERVAL", 0.5)
    read()
    assert {r.get_method() for r in shop.requests} == {"GET"}
    assert all(
        r.get_header("Cookie") is None and r.get_header("Authorization") is None
        for r in shop.requests
    )
    assert pauses == [0.5] * (len(shop.requests) - 1)
    assert len(shop.requests) == 3  # categories, products, variations


def test_variation_with_an_attribute_left_at_any_is_skipped(shop):
    # Review M3: `?add-to-cart=<id>` alone likely does not add a variation whose colour is
    # "Any Цвет" (not checked live: stage D). The live API sends `value: null`.
    [tee] = [p for p in shop.listings["products"] if p["id"] == 103]
    tee["variations"].append({"id": 203, "attributes": [{"name": "Цвет", "value": None}]})
    shop.listings["variations"].append({**shop.listings["variations"][0], "id": 203})
    _, products, stats = read()
    assert "203" not in products and {"201", "202"} <= set(products)
    assert (stats.offers, stats.skipped) == (11, 6)

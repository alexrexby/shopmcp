"""Tool set assembly: capability filter, disabled tools, texts in both languages."""

import inspect
import re
from pathlib import Path

import pytest
from fakes import FakeAdapter, cart_strategy

from shopmcp.capabilities import Capability
from shopmcp.config import StoreConfig, load
from shopmcp.tools.spec import build_tools, page_meta

DEMO = Path(__file__).resolve().parents[2] / "stores/demo/store.toml"
CYRILLIC = re.compile("[а-яё]", re.IGNORECASE)
ALL = {
    "products_search",
    "product_details",
    "product_by_barcode",
    "product_analogs",
    "categories",
    "promotions",
    "pickup_points",
    "cart_link_create",
    "orders_history",
    "store_info",
}


def cfg(**changes: object) -> StoreConfig:
    data = load(DEMO).model_dump()
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        node = data
        for p in parents:
            node = node[p]
        node[leaf] = value
    return StoreConfig.model_validate(data)


def suffixes(c: StoreConfig, adapter: FakeAdapter) -> set[str]:
    cart = cart_strategy(c, adapter)
    return {s.suffix for s in build_tools(c, adapter, cart)}


def test_all_capabilities_give_the_canonical_ten():
    assert suffixes(cfg(), FakeAdapter()) == ALL


def test_search_only_gives_search_and_info():
    # No DETAILS: the cart could not check a single id, so it is not offered at all.
    assert suffixes(cfg(), FakeAdapter({Capability.SEARCH})) == {"products_search", "store_info"}


def test_cart_comes_with_details():
    assert suffixes(cfg(), FakeAdapter({Capability.DETAILS})) == {
        "product_details",
        "cart_link_create",
        "store_info",
    }


def test_no_capabilities_still_give_info():
    assert suffixes(cfg(), FakeAdapter(())) == {"store_info"}


def test_disabled_tools_are_dropped():
    got = suffixes(cfg(tools__disabled=["promotions", "categories"]), FakeAdapter())
    assert got == ALL - {"promotions", "categories"}


def test_every_tool_text_has_both_languages():
    c = cfg()
    adapter = FakeAdapter()
    specs = build_tools(c, adapter, cart_strategy(c, adapter))
    keys = [key for spec in specs for key, _ in spec.texts()]
    assert len(keys) == len(set(keys))
    for spec in specs:
        for key, text in spec.texts():
            assert CYRILLIC.search(text.ru), f"{key}: ru has no Cyrillic"
            assert not CYRILLIC.search(text.en), f"{key}: en contains Cyrillic"


def test_every_documented_param_exists():
    # with_param_docs raises on unknown names at registration; this pins it earlier.
    c = cfg()
    adapter = FakeAdapter()
    for spec in build_tools(c, adapter, cart_strategy(c, adapter)):
        params = set(inspect.signature(spec.fn).parameters)
        assert set(spec.params) == params, spec.suffix


def test_orders_scope_comes_from_config():
    c = cfg(auth__tool_scopes={"orders_history": ["account.orders"]})
    adapter = FakeAdapter()
    specs = build_tools(c, adapter, cart_strategy(c, adapter))
    scopes = {s.suffix: s.scopes for s in specs if s.scopes}
    assert scopes == {"orders_history": ("account.orders",)}


@pytest.mark.parametrize(
    "page, limit, total, pages, has_more",
    [(1, 10, 0, 0, False), (1, 10, 10, 1, False), (1, 10, 11, 2, True), (2, 10, 11, 2, False)],
)
def test_page_meta(page, limit, total, pages, has_more):
    meta = page_meta(page, limit, total)
    assert (meta.pages, meta.has_more) == (pages, has_more)

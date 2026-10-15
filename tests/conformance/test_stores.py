"""Every store in stores/ passes the conformance checks, served locally from its config."""

from pathlib import Path

import pytest

from shopmcp.config import StoreConfig, load
from shopmcp.conformance.runner import check_config, report

STORES = sorted((Path(__file__).resolve().parents[2] / "stores").glob("*/store.toml"))


def _remote(path: Path) -> bool:
    extra = load(path).adapter.model_extra or {}
    return any(str(v).startswith("https://") for v in extra.values())


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(p, id=p.parent.name, marks=[pytest.mark.online] if _remote(p) else [])
        for p in STORES
    ],
)
def test_store_conforms(path):
    cfg = load(path)
    results = check_config(cfg, path.parent)
    failed = {r.id: r.detail for r in results if r.status == "fail"}
    assert failed == {}
    skipped = {r.id for r in results if r.status == "skip"}
    assert skipped <= ({"C13"} if not cfg.auth.enabled else set())


def test_demo_is_found():
    assert "demo" in [p.parent.name for p in STORES]


def test_url_without_config_infers_expectations():
    # `shopmcp check --url` on a live store: no config, so prefix, tool set and personal
    # tools are inferred; the budget is unknown and the rate limit is not probed.
    from broken_store.store import build

    from shopmcp.conformance.runner import check_url, serve

    with serve(build()) as base:
        results = {r.id: r for r in check_url(f"{base}/mcp")}
    assert {i for i, r in results.items() if r.status == "fail"} == {"C13"}
    assert {i for i, r in results.items() if r.status == "skip"} == {"C14"}
    # The store declares public_url http://localhost:8000/mcp but is reached on a random
    # port: without a config the resource must be the URL the client used, as behind a
    # misconfigured proxy. Everything else about auth passed.
    assert results["C13"].detail.startswith("metadata resource 'http://localhost:8000/mcp'")


SHOP_SCRIPT_CHAIN = {
    "strategy": "form_post",
    "mode": "chain",
    "action": "{site}/cart/add/",
    "fields": [["product_id", "{product_id}"], ["sku_id", "{variant_id}"], ["quantity", "{qty}"]],
    "finish_url": "{site}/cart/",
    "secret": "c" * 32,
}
OTHER_CHAIN = {**SHOP_SCRIPT_CHAIN, "fields": [["id", "{id}"], ["qty", "{qty}"]]}
INSALES_SINGLE = {
    "strategy": "form_post",
    "mode": "single",
    "action": "{site}/cart_items",
    "fields": [["variant_ids[{id}]", "{qty}"]],
    "secret": "c" * 32,
}


@pytest.mark.parametrize(
    ("cart", "site", "public_url", "status"),
    [
        (SHOP_SCRIPT_CHAIN, "https://demo.example", "https://mcp.demo.example/mcp", "ok"),
        (SHOP_SCRIPT_CHAIN, "https://demo.example", "https://mcp.other.example/mcp", "warn"),
        (SHOP_SCRIPT_CHAIN, "http://demo.example", "http://mcp.other.example/mcp", "fail"),
        (INSALES_SINGLE, "https://demo.example", "https://mcp.other.example/mcp", "fail"),
        # Review M3: SameSite=None is known for Webasyst only; another chain form fails.
        (OTHER_CHAIN, "https://demo.example", "https://mcp.other.example/mcp", "fail"),
    ],
)
def test_form_post_trampoline_off_the_store_site_is_flagged(cart, site, public_url, status):
    # Check V1: from another site a store whose cart cookie is not SameSite=None starts a
    # new cart on every POST, and the trampoline cannot see it. C11 says so from the config.
    path = next(p for p in STORES if p.parent.name == "demo")
    data = load(path).model_dump(mode="json")
    data["site"] = site
    data["cart"] = {
        k: v.replace("{site}", site) if k in ("action", "finish_url") else v
        for k, v in cart.items()
    }
    data["server"]["public_url"] = public_url
    results = {r.id: r for r in check_config(StoreConfig.model_validate(data), path.parent)}
    c11 = results["C11"]
    assert (c11.status, "subdomain" in c11.detail) == (status, status != "ok"), c11.detail
    others = {i: r.detail for i, r in results.items() if r.status in ("fail", "warn")}
    assert set(others) == ({"C11"} if status != "ok" else set()), others
    assert f"{status == 'warn':d} warnings" in report(list(results.values()))

"""Every store in stores/ passes the conformance checks, served locally from its config."""

from pathlib import Path

import pytest

from shopmcp.config import load
from shopmcp.conformance.runner import check_config

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

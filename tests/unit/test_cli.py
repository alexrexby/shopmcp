"""CLI, scaffold template and stub adapters (stage 1D)."""

import inspect
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from fakes import FakeAdapter

from shopmcp import cli
from shopmcp.adapters import registry
from shopmcp.adapters.base import AdapterContext, ShopAdapter
from shopmcp.config import AdapterConfig, load

STUB_KINDS = ["bitrix", "insales", "http_json"]
FEED = "https://shop.example/feed.xml"
SITE = "https://shop.example"


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def scaffold(*extra: str) -> int:
    return cli.main(["scaffold", "my-shop", "--feed", FEED, "--site", SITE, *extra])


# --- scaffold -----------------------------------------------------------------


def test_scaffold_creates_loadable_config(workdir):
    assert scaffold() == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert (cfg.id, cfg.prefix, cfg.locale) == ("my-shop", "myshop", "ru")
    assert cfg.adapter.kind == "yml" and cfg.adapter.model_extra["feed"] == FEED
    assert str(cfg.site).rstrip("/") == SITE
    assert cfg.cart.strategy == "link_list"
    assert cfg.texts == {}
    assert cfg.conformance.sample_query == cli.SAMPLE_QUERY_PLACEHOLDER


def test_scaffold_locale_and_prefix(workdir):
    assert scaffold("--locale", "en", "--prefix", "shop") == 0
    cfg = load(workdir / "stores/my-shop/store.toml")
    assert (cfg.locale, cfg.prefix) == ("en", "shop")


def test_scaffold_template_has_texts_table_and_both_languages(workdir):
    assert scaffold() == 0
    text = (workdir / "stores/my-shop/store.toml").read_text("utf-8")
    assert "\n[texts]\n" in text and "\nlocale = " in text
    assert "Edit every line" in text and "Отредактируйте" in text
    assert "${VAR}" in text  # the escaped `$${VAR}` reached the file as a literal


def test_scaffold_does_not_overwrite(workdir, capsys):
    path = workdir / "stores/my-shop/store.toml"
    path.parent.mkdir(parents=True)
    path.write_text("keep me", "utf-8")
    assert scaffold() == 1
    assert path.read_text("utf-8") == "keep me"
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("store_id", ["../evil", "Bad", "a"])
def test_scaffold_rejects_bad_id_before_touching_disk(workdir, store_id, capsys):
    assert cli.main(["scaffold", store_id, "--feed", FEED, "--site", SITE]) == 1
    assert "id:" in capsys.readouterr().err
    assert not (workdir / "stores").exists()
    assert not (workdir.parent / "evil").exists()


def test_scaffold_escapes_values(workdir):
    feed = 'https://shop.example/feed.xml?a="b"\\c'
    assert cli.main(["scaffold", "my-shop", "--feed", feed, "--site", SITE]) == 0
    assert load(workdir / "stores/my-shop/store.toml").adapter.model_extra["feed"] == feed


def test_scaffold_bad_site_is_an_error(workdir, capsys):
    assert cli.main(["scaffold", "my-shop", "--feed", FEED, "--site", "not a url"]) == 1
    assert "site:" in capsys.readouterr().err


# --- check (stage 2) ------------------------------------------------------------


def test_check_is_a_clear_stub(capsys):
    assert cli.main(["check", "stores/demo/store.toml"]) == 2
    assert "stage 2" in capsys.readouterr().err
    assert cli.main(["check", "--url", "http://localhost:8000/mcp", "--online"]) == 2


def test_check_needs_a_target():
    with pytest.raises(SystemExit):
        cli.main(["check"])


# --- stub adapters ----------------------------------------------------------------

CTX = AdapterContext(data_dir=Path("."), http_timeout=5, store_id="t")
PROTOCOL_METHODS = [
    n for n, v in vars(ShopAdapter).items() if callable(v) and not n.startswith("_")
]


@pytest.mark.parametrize("kind", STUB_KINDS)
def test_stub_adapter_declares_nothing(kind):
    adapter = registry.create(AdapterConfig(kind=kind), CTX)
    assert isinstance(adapter, ShopAdapter)
    assert adapter.capabilities() == frozenset()
    for name in PROTOCOL_METHODS:
        got, want = getattr(adapter, name), getattr(ShopAdapter, name)
        assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(want)


# --- index and serve (call into 1A/1B through their contracts) ------------------------


def _config_with_kind(workdir: Path, kind: str) -> Path:
    path = workdir / "store.toml"
    text = cli.render_store_toml("cli-test", feed=FEED, site=SITE, prefix="pp")
    path.write_text(text.replace('kind = "yml"', f'kind = "{kind}"'), "utf-8")
    return path


def test_index_starts_and_closes_the_adapter(workdir, capsys):
    fake = FakeAdapter()
    registry.register("fakecli", lambda cfg, ctx: fake)
    assert cli.main(["index", str(_config_with_kind(workdir, "fakecli"))]) == 0
    assert fake.started and fake.closed
    assert json.loads(capsys.readouterr().out) == {"products": len(fake.products)}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("kind", STUB_KINDS)
def test_stub_store_serves_only_the_always_present_tools(workdir, kind):
    import uvicorn

    from shopmcp.server.app import build_app

    server = uvicorn.Server(
        uvicorn.Config(
            build_app(load(_config_with_kind(workdir, kind))),
            host="127.0.0.1",
            port=_free_port(),
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.config.port}"
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as r:
            assert r.status == 200
        req = urllib.request.Request(
            f"{base}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            tools = {t["name"] for t in json.loads(r.read())["result"]["tools"]}
        assert tools == {"pp_cart_link_create", "pp_store_info"}
    finally:
        server.should_exit = True
        thread.join(timeout=5)

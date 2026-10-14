"""The assembled ASGI app over Streamable HTTP: the SDK client talks to it in-process
(httpx2 ASGITransport), the lifespan is driven by hand, as uvicorn would."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2
import pytest
from fakes import FakeAdapter, make_product
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from pydantic import ValidationError
from starlette.types import ASGIApp
from test_tools_spec import ALL, cfg

from shopmcp.capabilities import Capability
from shopmcp.config import ConfigError, StoreConfig
from shopmcp.server.app import build_app
from shopmcp.server.cors import origin_allowed
from shopmcp.server.ratelimit import RateLimit

BASE = "http://localhost:8000"


@asynccontextmanager
async def lifespan(app: ASGIApp) -> AsyncIterator[None]:
    to_app, app_inbox = anyio.create_memory_object_stream[dict[str, Any]](4)
    app_outbox, from_app = anyio.create_memory_object_stream[dict[str, Any]](4)
    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    async with anyio.create_task_group() as tg:
        tg.start_soon(app, scope, app_inbox.receive, app_outbox.send)
        await to_app.send({"type": "lifespan.startup"})
        assert (await from_app.receive())["type"] == "lifespan.startup.complete"
        try:
            yield
        finally:
            await to_app.send({"type": "lifespan.shutdown"})
            assert (await from_app.receive())["type"] == "lifespan.shutdown.complete"


@asynccontextmanager
async def serve(app: ASGIApp, base: str = BASE) -> AsyncIterator[httpx2.AsyncClient]:
    async with (
        lifespan(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url=base) as http,
    ):
        yield http


@asynccontextmanager
async def connect(c: StoreConfig, adapter: FakeAdapter) -> AsyncIterator[Client]:
    async with serve(build_app(c, adapter=adapter)) as http:
        transport = streamable_http_client(f"{BASE}{c.server.path}", http_client=http)
        async with Client(transport) as client:
            yield client


def error_of(res: Any) -> dict[str, Any]:
    assert res.is_error
    return json.loads(res.content[0].text)["error"]


@pytest.mark.anyio
async def test_search_only_adapter_lists_two_tools():
    async with connect(cfg(), FakeAdapter({Capability.SEARCH})) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"demo_products_search", "demo_store_info"}


@pytest.mark.anyio
async def test_details_with_no_id_found_is_not_found():
    async with connect(cfg(), FakeAdapter()) as client:
        none = await client.call_tool("demo_product_details", {"ids": ["__nope__"]})
        some = await client.call_tool("demo_product_details", {"ids": ["1", "__nope__"]})
    err = error_of(none)
    assert err["code"] == "not_found" and err["details"] == {"missing": ["__nope__"]}
    assert not some.is_error and some.structured_content["missing"] == ["__nope__"]


@pytest.mark.anyio
async def test_details_over_budget_lists_omitted_ids():
    long = "Очень подробное описание товара. " * 40
    products = [make_product(str(i), f"Товар {i}", description=long) for i in range(20)]
    ids = [str(i) for i in range(20)]
    c = cfg(limits__max_response_bytes=4096)
    async with connect(c, FakeAdapter(products=products)) as client:
        res = await client.call_tool("demo_product_details", {"ids": ids})
    assert not res.is_error and len(res.content[0].text.encode()) <= 4096
    shown = [p["id"] for p in res.structured_content["items"]]
    assert shown and shown + res.structured_content["omitted"] == ids


@pytest.mark.anyio
async def test_cart_quantity_is_capped_by_config():
    async with connect(cfg(limits__max_quantity=5), FakeAdapter()) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["demo_cart_link_create"]
        res = await client.call_tool(
            "demo_cart_link_create", {"lines": [{"id": "1", "quantity": 6}]}
        )
    line = tool.input_schema["$defs"]["LineIn"]["properties"]["quantity"]
    assert (
        line["maximum"] == 5
        and "не больше 5" in tool.input_schema["properties"]["lines"]["description"]
    )
    assert res.is_error


@pytest.mark.anyio
async def test_all_capabilities_list_ten_tools_with_schemas_and_annotations():
    async with connect(cfg(), FakeAdapter()) as client:
        tools = (await client.list_tools()).tools
        info, instructions = client.server_info, client.instructions
    assert {t.name for t in tools} == {f"demo_{s}" for s in ALL}
    for t in tools:
        assert t.output_schema, t.name
        assert t.annotations and t.annotations.read_only_hint is not None, t.name
        assert t.description and t.title, t.name
    assert info and info.name == "Демо-маркет"
    assert instructions and "demo_cart_link_create" in instructions


@pytest.mark.anyio
async def test_optional_params_are_not_required_and_limits_match_descriptions():
    async with connect(cfg(), FakeAdapter()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    search = tools["demo_products_search"].input_schema
    assert "required" not in search or not search["required"]
    assert "до 20" in search["properties"]["limit"]["description"]
    lines = tools["demo_cart_link_create"].input_schema["properties"]["lines"]
    assert lines["maxItems"] == 30 and "до 30" in lines["description"]
    assert "до 30" in tools["demo_cart_link_create"].description


@pytest.mark.anyio
async def test_success_is_compact_structured_and_validated():
    async with connect(cfg(), FakeAdapter()) as client:
        res = await client.call_tool("demo_products_search", {"q": "Молоко"})
    assert not res.is_error
    text = res.content[0].text
    assert "\n" not in text and '": ' not in text
    assert json.loads(text) == res.structured_content
    assert res.structured_content["items"][0]["id"] == "1"


@pytest.mark.anyio
async def test_not_found_is_an_error_result():
    async with connect(cfg(), FakeAdapter()) as client:
        res = await client.call_tool("demo_product_by_barcode", {"barcode": "0000000000000"})
    err = error_of(res)
    assert err["code"] == "not_found" and err["retryable"] is False
    assert res.structured_content is None


@pytest.mark.anyio
async def test_cart_unknown_id_is_an_error_with_the_list():
    async with connect(cfg(), FakeAdapter()) as client:
        res = await client.call_tool(
            "demo_cart_link_create",
            {"lines": [{"id": "1", "quantity": 2}, {"id": "__nope__", "quantity": 1}]},
        )
    err = error_of(res)
    assert err["code"] == "invalid_input" and err["details"] == {"missing": ["__nope__"]}


@pytest.mark.anyio
async def test_cart_url_template_over_http():
    async with connect(cfg(), FakeAdapter()) as client:
        res = await client.call_tool(
            "demo_cart_link_create",
            {"lines": [{"id": "1", "quantity": 2}, {"id": "2", "quantity": 1}]},
        )
    assert not res.is_error
    assert res.structured_content["url"] == (
        "https://demo.example/cart/add?id[]=1&qty[]=2&id[]=2&qty[]=1"
    )


@pytest.mark.anyio
async def test_response_over_budget_is_truncated():
    products = [make_product(str(i), "Очень длинное название товара " * 5) for i in range(20)]
    c = cfg(limits__max_response_bytes=2048)
    async with connect(c, FakeAdapter(products=products)) as client:
        res = await client.call_tool("demo_products_search", {"limit": 20, "mode": "full"})
    assert not res.is_error
    assert len(res.content[0].text.encode()) <= 2048
    meta = res.structured_content["meta"]
    assert meta["truncated"] is True and meta["has_more"] is True


class Broken(FakeAdapter):
    async def search(self, q):
        raise RuntimeError("secret trace /etc/passwd")


@pytest.mark.anyio
async def test_unexpected_exception_is_internal_error_without_trace():
    async with connect(cfg(), Broken()) as client:
        res = await client.call_tool("demo_products_search", {})
    err = error_of(res)
    assert err["code"] == "internal_error" and "secret" not in res.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("locale", ["ru", "en"])
async def test_error_message_follows_locale(locale):
    async with connect(cfg(locale=locale), FakeAdapter()) as client:
        res = await client.call_tool("demo_product_by_barcode", {"barcode": "0000000000000"})
    assert error_of(res)["message"].isascii() == (locale == "en")


@pytest.mark.anyio
async def test_text_override_reaches_tools_list():
    c = cfg(texts={"tool.products_search.description": "Поиск по Демо-маркету."})
    async with connect(c, FakeAdapter()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["demo_products_search"].description == "Поиск по Демо-маркету."


def test_unknown_text_key_fails_the_start():
    c = cfg(texts={"tool.products_serch.description": "опечатка"})
    with pytest.raises(ConfigError, match="products_serch"):
        build_app(c, adapter=FakeAdapter())


def test_text_key_of_a_disabled_tool_is_known():
    c = cfg(tools__disabled=["promotions"], texts={"tool.promotions.title": "Скидки"})
    build_app(c, adapter=FakeAdapter())


@pytest.mark.anyio
async def test_healthz_and_adapter_lifecycle():
    adapter = FakeAdapter()
    async with serve(build_app(cfg(), adapter=adapter)) as http:
        assert adapter.started and not adapter.closed
        res = await http.get("/healthz")
    assert res.status_code == 200 and res.json() == {"status": "ok", "store": "demo", "products": 3}
    assert adapter.closed


class StaleAdapter(FakeAdapter):
    async def health(self) -> dict[str, Any]:
        return {"products": 3, "stale": True, "index_age_seconds": 99999}


@pytest.mark.anyio
async def test_healthz_is_503_on_a_stale_index():
    # Review P2: the Docker HEALTHCHECK reacts to non-2xx only.
    async with serve(build_app(cfg(), adapter=StaleAdapter())) as http:
        res = await http.get("/healthz")
    assert res.status_code == 503
    assert res.json()["status"] == "stale" and res.json()["index_age_seconds"] == 99999


@pytest.mark.anyio
@pytest.mark.parametrize("accept", ["text/event-stream", "application/json", None])
async def test_get_on_mcp_path_is_405_at_once(accept):
    # SDK 2.3.0 opens a never-ending SSE stream on GET even in stateless mode; without
    # the PostOnly gate this request hangs and fail_after turns that into a failure.
    headers = {"accept": accept} if accept else {}
    async with serve(build_app(cfg(), adapter=FakeAdapter())) as http:
        with anyio.fail_after(3):
            res = await http.get("/mcp", headers=headers)
    assert res.status_code == 405 and res.headers["allow"] == "POST"
    assert res.json()["error"]["code"] == -32000


@pytest.mark.anyio
async def test_public_host_from_config_is_accepted():
    # The SDK default would accept only localhost; behind a proxy Host is the public name.
    c = cfg(server__allowed_hosts=["shop.example"])
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"accept": "application/json, text/event-stream"}
    async with serve(build_app(c, adapter=FakeAdapter()), "https://shop.example") as http:
        assert (await http.post("/mcp", json=body, headers=headers)).status_code != 421
    async with serve(build_app(c, adapter=FakeAdapter())) as http:
        assert (await http.post("/mcp", json=body, headers=headers)).status_code == 421


@pytest.mark.anyio
async def test_rate_limit_answers_429_with_retry_after():
    c = cfg(limits__rate_per_minute=3)
    async with serve(build_app(c, adapter=FakeAdapter())) as http:
        codes = [(await http.post("/mcp", content=b"{}")).status_code for _ in range(4)]
        last = await http.post("/mcp", content=b"{}")
        health = await http.get("/healthz")
    assert 429 not in codes[:3] and codes[3] == 429
    assert int(last.headers["retry-after"]) >= 1
    assert health.status_code == 200  # the health probe is exempt


def test_rate_limit_refills_over_time():
    now = [0.0]
    limit = RateLimit(lambda *a: None, rate_per_minute=60, clock=lambda: now[0])
    assert all(limit.take("1.1.1.1") == 0 for _ in range(60))
    assert limit.take("1.1.1.1") == pytest.approx(1.0)
    assert limit.take("2.2.2.2") == 0  # buckets are per IP
    now[0] = 1.0
    assert limit.take("1.1.1.1") == 0


@pytest.mark.parametrize(
    "peer, forwarded, trusted, want",
    [
        ("10.0.0.1", "203.0.113.5", [], "10.0.0.1"),  # untrusted peer: header ignored
        ("10.0.0.1", "203.0.113.5", ["10.0.0.0/8"], "203.0.113.5"),
        ("10.0.0.1", "6.6.6.6, 203.0.113.5", ["10.0.0.0/8"], "203.0.113.5"),  # spoofed head
        ("10.0.0.1", "203.0.113.5, 10.0.0.2", ["10.0.0.0/8"], "203.0.113.5"),
        ("10.0.0.1", None, ["10.0.0.0/8"], "10.0.0.1"),
    ],
)
def test_client_ip_trusts_only_configured_proxies(peer, forwarded, trusted, want):
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    limit = RateLimit(lambda *a: None, rate_per_minute=60, trusted_proxies=trusted)
    assert limit.client_ip({"type": "http", "client": (peer, 1), "headers": headers}) == want


# --- auth: one source of scopes -----------------------------------------------------


def auth_cfg(**changes: object) -> StoreConfig:
    # No token is ever sent here, so the JWKS is never fetched.
    return cfg(
        auth__enabled=True,
        auth__issuer="https://id.demo.example",
        auth__jwks_uri="http://127.0.0.1:9/jwks",
        **changes,
    )


async def call_raw(http: httpx2.AsyncClient, name: str) -> httpx2.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
    headers = {"accept": "application/json, text/event-stream"}
    return await http.post("/mcp", json=body, headers=headers)


@pytest.mark.anyio
async def test_personal_tool_is_gated_with_default_scope():
    async with serve(build_app(auth_cfg(auth__tool_scopes={}), adapter=FakeAdapter())) as http:
        personal = await call_raw(http, "demo_orders_history")
        public = await call_raw(http, "demo_store_info")
    assert personal.status_code == 401
    assert 'scope="orders.read"' in personal.headers["www-authenticate"]
    assert public.status_code == 200


@pytest.mark.anyio
async def test_config_scope_overrides_the_default_everywhere():
    c = auth_cfg(auth__tool_scopes={"orders_history": ["orders.mine"]})
    app = build_app(c, adapter=FakeAdapter())
    async with serve(app) as http:
        res = await call_raw(http, "demo_orders_history")
        prm = await http.get("/.well-known/oauth-protected-resource/mcp")
    assert 'scope="orders.mine"' in res.headers["www-authenticate"]
    assert prm.json()["scopes_supported"] == ["orders.mine"]


def test_personal_tool_without_scope_is_impossible():
    with pytest.raises(ValidationError, match="tool_scopes"):
        auth_cfg(auth__tool_scopes={"orders_history": []})


def test_unknown_tool_in_tool_scopes_fails_the_start():
    with pytest.raises(ConfigError, match="orders_histroy"):
        build_app(auth_cfg(auth__tool_scopes={"orders_histroy": ["x"]}), adapter=FakeAdapter())


# --- rate limit keys (security Н3) ------------------------------------------------------


async def _hit(limit: RateLimit, ip: str) -> int:
    status = 0

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    limit.app = app
    scope = {"type": "http", "path": "/mcp", "client": (ip, 1), "headers": []}
    await limit(scope, None, send)
    return status


@pytest.mark.anyio
async def test_ipv6_rotation_inside_a_64_shares_one_bucket():
    limit = RateLimit(None, rate_per_minute=60, clock=lambda: 0.0)
    codes = [await _hit(limit, f"2001:db8::{i:x}") for i in range(1, 101)]
    assert codes.count(429) == 40  # 100 addresses of one /64, 60 allowed
    assert await _hit(limit, "2001:db8:0:1::1") == 200  # the next /64 is someone else


@pytest.mark.anyio
async def test_ipv4_mapped_counts_as_its_ipv4():
    limit = RateLimit(None, rate_per_minute=1, clock=lambda: 0.0)
    assert await _hit(limit, "203.0.113.5") == 200
    assert await _hit(limit, "::ffff:203.0.113.5") == 429


@pytest.mark.anyio
async def test_subscriptions_listen_does_not_open_a_stream():
    # Protocol 2026-07-28 opens an endless SSE stream over POST too; nothing is published
    # here, so the method is off. Without that, fail_after turns the hang into a failure.
    body = {
        "jsonrpc": "2.0",
        "id": "listen-1",
        "method": "subscriptions/listen",
        "params": {
            "notifications": {"toolsListChanged": True},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2026-07-28",
        "mcp-method": "subscriptions/listen",
    }
    async with serve(build_app(cfg(), adapter=FakeAdapter())) as http:
        with anyio.fail_after(3):
            res = await http.post("/mcp", json=body, headers=headers)
    assert "event-stream" not in res.headers.get("content-type", "")
    assert res.json()["error"]["code"] == -32601


# --- CORS: opt-in by server.allowed_origins (review, PostOnly/CORS question) -------------

APP_ORIGIN = "https://app.example"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"accept": "application/json, text/event-stream"}


def preflight(origin: str, headers: str = "content-type, authorization") -> dict[str, str]:
    return {
        "origin": origin,
        "access-control-request-method": "POST",
        "access-control-request-headers": headers,
    }


@pytest.mark.anyio
async def test_preflight_from_an_allowed_origin_is_204():
    c = cfg(server__allowed_origins=[APP_ORIGIN])
    requested = "Content-Type, Authorization, MCP-Protocol-Version, mcp-param-q, cookie, x-evil"
    async with serve(build_app(c, adapter=FakeAdapter())) as http:
        res = await http.options("/mcp", headers=preflight(APP_ORIGIN, requested))
    assert res.status_code == 204
    assert res.headers.get_list("access-control-allow-origin") == [APP_ORIGIN]
    assert res.headers["access-control-allow-methods"] == "POST"
    assert res.headers["access-control-allow-headers"] == (
        "content-type, authorization, mcp-protocol-version, mcp-param-q"
    )
    assert "access-control-allow-credentials" not in res.headers


@pytest.mark.anyio
@pytest.mark.parametrize(
    "origins, origin",
    [
        ([APP_ORIGIN], "https://evil.example"),
        ([APP_ORIGIN], "https://app.example.evil"),
        ([], APP_ORIGIN),
    ],
    ids=["foreign", "suffix", "cors-off"],
)
async def test_preflight_otherwise_stays_closed(origins, origin):
    c = cfg(server__allowed_origins=origins)
    async with serve(build_app(c, adapter=FakeAdapter())) as http:
        res = await http.options("/mcp", headers=preflight(origin))
    assert res.status_code == 405
    assert "access-control-allow-origin" not in res.headers


@pytest.mark.anyio
async def test_post_from_an_allowed_origin_carries_acao():
    c = cfg(server__allowed_origins=[APP_ORIGIN])
    async with serve(build_app(c, adapter=FakeAdapter())) as http:
        ok = await http.post("/mcp", json=LIST, headers={**ACCEPT, "origin": APP_ORIGIN})
        foreign = await http.post(
            "/mcp", json=LIST, headers={**ACCEPT, "origin": "https://evil.example"}
        )
    assert ok.status_code == 200
    assert ok.headers.get_list("access-control-allow-origin") == [APP_ORIGIN]
    assert "www-authenticate" in ok.headers["access-control-expose-headers"]
    assert foreign.status_code == 403 and "access-control-allow-origin" not in foreign.headers


@pytest.mark.anyio
async def test_rate_limit_and_auth_answers_are_readable_by_the_browser():
    c = auth_cfg(server__allowed_origins=[APP_ORIGIN], limits__rate_per_minute=2)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    body["params"] = {"name": "demo_orders_history"}
    headers = {**ACCEPT, "origin": APP_ORIGIN}
    async with serve(build_app(c, adapter=FakeAdapter())) as http:
        metadata = await http.get(
            "/.well-known/oauth-protected-resource/mcp", headers={"origin": APP_ORIGIN}
        )
        gated = await http.post("/mcp", json=body, headers=headers)
        limited = await http.post("/mcp", json=body, headers=headers)
    assert gated.status_code == 401 and gated.headers["access-control-allow-origin"] == APP_ORIGIN
    assert limited.status_code == 429
    assert limited.headers["access-control-allow-origin"] == APP_ORIGIN
    # The SDK's own CORS on the metadata route; a second header would break the browser.
    assert len(metadata.headers.get_list("access-control-allow-origin")) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example",
        "https://app.example:8443",
        "http://localhost:5173",
        "http://localhost",
        "https://app.example.evil",
        "http://localhost.evil:1",
        "null",
        "*",
    ],
)
def test_origin_rule_is_the_sdk_rule(origin):
    allowed = ["https://app.example", "http://localhost:*"]
    sdk = TransportSecurityMiddleware(TransportSecuritySettings(allowed_origins=allowed))
    assert origin_allowed(origin, allowed) is sdk._validate_origin(origin)

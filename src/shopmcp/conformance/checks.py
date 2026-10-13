"""Conformance checks C1-C14 (plan §7): one set for a local build and a live URL.

Every check talks to the endpoint over HTTP with raw JSON-RPC (`urllib`), not through the
SDK client: the client validates structuredContent against outputSchema and would raise
where a check has to report what is actually on the wire. Expectations come from the
store config when there is one; without it they are inferred from the server and the
canonical tool set, and checks that need the config say so instead of guessing.
"""

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from shopmcp import __version__
from shopmcp.config import StoreConfig

PROTOCOL_VERSION = "2025-06-18"  # handshake revision: the plain JSON-RPC transport path
LISTEN_VERSION = "2026-07-28"  # revision that added subscriptions/listen
TIMEOUT = 10.0  # seconds per request
STREAM_TIMEOUT = 3.0  # seconds a request may take before it counts as a held-open stream
MISSING_ID = "__nope__"
UNKNOWN_BARCODE = "0000000000000"
MAX_FACETS = 10
_MARKUP = re.compile(r"&[a-zA-Z]+;|&#\d+;|<[a-zA-Z/]")

Status = Literal["ok", "fail", "skip"]


class CheckError(Exception):
    """The check cannot go on: the reason is its failure detail."""


@dataclass(frozen=True)
class Expect:
    """What the store promises. `None` = unknown (no config), the check infers or skips."""

    brand: str | None = None
    prefix: str | None = None
    site: str | None = None
    sample_query: str | None = None
    sample_barcode: str | None = None
    max_page_size: int | None = None
    max_cart_lines: int | None = None
    max_response_bytes: int | None = None
    rate_per_minute: int | None = None
    cart_strategy: str | None = None
    public_url: str | None = None
    tools: frozenset[str] | None = None  # full names
    personal: Mapping[str, tuple[str, ...]] | None = None  # full name -> scopes
    auth_enabled: bool | None = None

    @classmethod
    def from_config(cls, cfg: StoreConfig) -> "Expect":
        """Tool set and scopes exactly as `build_app` derives them from this config."""
        from shopmcp.adapters import registry
        from shopmcp.adapters.base import AdapterContext
        from shopmcp.cart import strategies
        from shopmcp.i18n import Texts
        from shopmcp.tools.spec import build_tools

        # create() only reads the config: nothing is fetched or indexed here.
        ctx = AdapterContext(data_dir=Path("."), http_timeout=TIMEOUT, store_id=cfg.id)
        adapter = registry.create(cfg.adapter, ctx)
        cart = strategies.create(cfg.cart, adapter, Texts(cfg.locale, cfg.texts))
        specs = build_tools(cfg, adapter, cart)
        return cls(
            brand=cfg.brand,
            prefix=cfg.prefix,
            site=str(cfg.site),
            sample_query=cfg.conformance.sample_query,
            sample_barcode=cfg.conformance.sample_barcode,
            max_page_size=cfg.limits.max_page_size,
            max_cart_lines=cfg.limits.max_cart_lines,
            max_response_bytes=cfg.limits.max_response_bytes,
            rate_per_minute=cfg.limits.rate_per_minute,
            cart_strategy=cart.kind,
            public_url=str(cfg.server.public_url),
            tools=frozenset(f"{cfg.prefix}_{s.suffix}" for s in specs),
            personal={f"{cfg.prefix}_{s.suffix}": s.scopes for s in specs if s.scopes},
            auth_enabled=cfg.auth.enabled,
        )


def canonical_scopes() -> dict[str, tuple[str, ...]]:
    """Suffix -> default scopes of every tool the framework knows (no config needed)."""
    from shopmcp.adapters.base import AdapterBase
    from shopmcp.cart.strategies import LinkListStrategy
    from shopmcp.tools.spec import canonical_tools

    probe = StoreConfig.model_validate(
        {
            "id": "probe",
            "prefix": "probe",
            "brand": "probe",
            "site": "https://probe.example",
            "adapter": {"kind": "none"},
            "server": {"public_url": "https://probe.example/mcp"},
            "conformance": {"sample_query": "x"},
        }
    )
    return {s.suffix: s.scopes for s in canonical_tools(probe, AdapterBase(), LinkListStrategy())}


@dataclass
class Reply:
    status: int | None  # None: no answer within the timeout
    headers: Mapping[str, str]
    body: bytes
    error: str | None = None

    def json(self) -> Any:
        return json.loads(self.body)


def http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = TIMEOUT,
) -> Reply:
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Reply(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Reply(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
    except TimeoutError:  # headers sent, the body never ends: a held-open stream
        return Reply(None, {}, b"", f"no answer in {timeout:g} s")
    except (urllib.error.URLError, OSError) as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            return Reply(None, {}, b"", f"no answer in {timeout:g} s")
        raise CheckError(f"{method} {url}: {e}") from e


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    result: dict[str, Any]

    @property
    def text(self) -> str | None:
        content = self.result.get("content") or []
        first = content[0] if content else {}
        return first.get("text") if isinstance(first, dict) else None

    @property
    def is_error(self) -> bool:
        return self.result.get("isError") is True

    @property
    def structured(self) -> Any:
        return self.result.get("structuredContent")

    def error(self) -> dict[str, Any] | None:
        """The envelope error `{code, message, retryable}`, if the text carries one."""
        try:
            doc = json.loads(self.text or "")
        except ValueError:
            return None
        err = doc.get("error") if isinstance(doc, dict) else None
        return err if isinstance(err, dict) else None


@dataclass
class Session:
    """One run against one endpoint. Every tool call is recorded for C8-C10."""

    url: str
    expect: Expect
    local: bool  # the runner started the server: checks that need a quiet server may run
    calls: list[ToolCall] = field(default_factory=list)
    _ids: Iterator[int] = field(default_factory=lambda: iter(range(1, 1 << 30)))
    _init: dict[str, Any] | None = None
    _tools: list[dict[str, Any]] | None = None
    _swept = False
    _product_ids: list[str] | None = None

    @property
    def origin(self) -> str:
        parts = urllib.parse.urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path or "/"

    def post(self, message: Any, headers: Mapping[str, str] | None = None, **kw: Any) -> Reply:
        h = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        return http("POST", self.url, json.dumps(message).encode(), {**h, **(headers or {})}, **kw)

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            message["params"] = params
        reply = self.post(message)
        if reply.status == 429:
            raise CheckError(f"{method}: 429 rate limited; rerun in a minute")
        if reply.status != 200:
            raise CheckError(f"{method}: HTTP {reply.status} {reply.error or reply.body[:200]!r}")
        try:
            doc = reply.json()
        except ValueError as e:
            raise CheckError(f"{method}: response is not JSON: {reply.body[:200]!r}") from e
        if "error" in doc:
            raise CheckError(f"{method}: JSON-RPC error {doc['error']}")
        return doc.get("result")

    def initialize(self) -> dict[str, Any]:
        if self._init is None:
            self._init = self.rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "shopmcp-check", "version": __version__},
                },
            )
        return self._init or {}

    def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            tools: list[dict[str, Any]] = []
            cursor = None
            while True:
                result = self.rpc("tools/list", {"cursor": cursor} if cursor else None) or {}
                tools += result.get("tools", [])
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self._tools = tools
        return self._tools

    def names(self) -> list[str]:
        return [t.get("name", "") for t in self.tools()]

    def prefix(self) -> str:
        if self.expect.prefix:
            return self.expect.prefix
        heads = {n.split("_", 1)[0] for n in self.names()}
        if len(heads) != 1:
            raise CheckError(f"no common tool prefix: {sorted(heads)}")
        return heads.pop()

    def tool(self, suffix: str) -> str | None:
        """Full name of a listed tool by suffix, or None."""
        name = f"{self.prefix()}_{suffix}"
        return name if name in self.names() else None

    def personal(self) -> dict[str, tuple[str, ...]]:
        if self.expect.personal is not None:
            return dict(self.expect.personal)
        p = self.prefix()
        return {f"{p}_{s}": v for s, v in canonical_scopes().items() if v and self.tool(s)}

    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolCall:
        result = self.rpc("tools/call", {"name": name, "arguments": args or {}}) or {}
        call = ToolCall(name, args or {}, result)
        self.calls.append(call)
        return call

    def product_ids(self, n: int) -> list[str]:
        """Up to `n` product ids from the search: the sample query's first, then any."""
        if self._product_ids is None:
            ids: list[str] = []
            search = self.tool("products_search")
            q = self.expect.sample_query
            for args in ({"q": q}, {}) if q else ({},):
                if search is None:
                    break
                res = self.call(search, {**args, "limit": self.page_size()})
                items = (res.structured or {}).get("items", []) if not res.is_error else []
                ids += [i["id"] for i in items if isinstance(i, dict) and i.get("id") not in ids]
            self._product_ids = ids
        return self._product_ids[:n]

    def page_size(self) -> int:
        """Largest `limit` the search accepts: from the config, else from its schema."""
        if self.expect.max_page_size:
            return self.expect.max_page_size
        search = next((t for t in self.tools() if t["name"] == self.tool("products_search")), {})
        limit = (search.get("inputSchema") or {}).get("properties", {}).get("limit", {})
        return int(limit.get("maximum", 10))

    def sweep(self) -> None:
        """Call every listed non-personal tool once with plausible arguments."""
        if self._swept:
            return
        self._swept = True
        ids = self.product_ids(3)
        personal = self.personal()
        args: dict[str, dict[str, Any]] = {
            "products_search": {"mode": "full", "limit": self.page_size()},
            "product_details": {"ids": ids or [MISSING_ID]},
            "product_by_barcode": {"barcode": self.expect.sample_barcode or UNKNOWN_BARCODE},
            "product_analogs": {"id": ids[0] if ids else MISSING_ID},
            "cart_link_create": {"lines": [{"id": ids[0] if ids else MISSING_ID, "quantity": 1}]},
        }
        p = self.prefix()
        for name in self.names():
            if name in personal:
                continue
            suffix = name[len(p) + 1 :] if name.startswith(f"{p}_") else name
            self.call(name, args.get(suffix, {}))


@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    status: Status
    detail: str = ""


Outcome = tuple[Status, str]


def _verdict(problems: list[str], ok: str = "") -> Outcome:
    return ("fail", "; ".join(problems)) if problems else ("ok", ok)


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


def _mentions(text: str, number: Any) -> bool:
    return re.search(rf"(?<![\d.]){re.escape(str(number))}(?![\d.])", text) is not None


def _compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# --- the checks -------------------------------------------------------------------------


def check_initialize(s: Session) -> Outcome:
    """C1: initialize works, serverInfo.name is the brand, instructions are not empty."""
    init = s.initialize()
    name = (init.get("serverInfo") or {}).get("name")
    problems = []
    if s.expect.brand is not None and name != s.expect.brand:
        problems.append(f"serverInfo.name {name!r} != brand {s.expect.brand!r}")
    if not name:
        problems.append("serverInfo.name is empty")
    if not str(init.get("instructions") or "").strip():
        problems.append("instructions are empty")
    if not init.get("protocolVersion"):
        problems.append("no protocolVersion")
    return _verdict(problems, f"{name}, protocol {init.get('protocolVersion')}")


def check_names(s: Session) -> Outcome:
    """C2: names match ^{prefix}_[a-z_]+$, are at most 64 characters and unique."""
    names = s.names()
    pattern = re.compile(rf"^{re.escape(s.prefix())}_[a-z_]+$")
    problems = [f"bad name {n!r}" for n in names if not pattern.match(n) or len(n) > 64]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        problems.append(f"duplicates {dupes}")
    return _verdict(problems, f"{len(names)} tools")


def check_tool_set(s: Session) -> Outcome:
    """C3: the tool set is the one the capabilities and tools.disabled imply, and no
    listed tool answers not_supported."""
    names = set(s.names())
    problems = []
    if s.expect.tools is not None:
        if extra := sorted(names - s.expect.tools):
            problems.append(f"extra {extra}")
        if missing := sorted(s.expect.tools - names):
            problems.append(f"missing {missing}")
    else:
        known = {f"{s.prefix()}_{suffix}" for suffix in canonical_scopes()}
        if extra := sorted(names - known):
            problems.append(f"not canonical {extra}")
    s.sweep()
    dead = sorted({c.name for c in s.calls if (c.error() or {}).get("code") == "not_supported"})
    if dead:
        problems.append(f"listed but not_supported {dead}")
    how = "config" if s.expect.tools is not None else "canonical set, no config"
    return _verdict(problems, f"{len(names)} tools match ({how})")


def check_schemas(s: Session) -> Outcome:
    """C4: every tool has description, inputSchema, outputSchema and annotations with an
    explicit readOnlyHint."""
    problems = []
    for t in s.tools():
        name = t.get("name")
        if not str(t.get("description") or "").strip():
            problems.append(f"{name}: no description")
        for key in ("inputSchema", "outputSchema"):
            if not isinstance(t.get(key), dict) or t[key].get("type") != "object":
                problems.append(f"{name}: no {key}")
        if not isinstance((t.get("annotations") or {}).get("readOnlyHint"), bool):
            problems.append(f"{name}: no annotations.readOnlyHint")
    return _verdict(problems, f"{len(s.tools())} tools")


def check_params(s: Session) -> Outcome:
    """C5: no parameter with a default is required; every maxItems/maximum appears in the
    description, and the cart's maxItems is the configured limit."""
    problems = []
    for t in s.tools():
        name, schema = t.get("name"), t.get("inputSchema") or {}
        props: dict[str, Any] = schema.get("properties") or {}
        defs: dict[str, Any] = schema.get("$defs") or {}
        for p in schema.get("required") or []:
            if "default" in props.get(p, {}):
                problems.append(f"{name}.{p}: has a default but is required")
        for p, prop in props.items():
            text = f"{prop.get('description', '')} {t.get('description', '')}"
            bounds = [("maxItems", prop.get("maxItems")), ("maximum", prop.get("maximum"))]
            ref = (prop.get("items") or {}).get("$ref", "")
            item = defs.get(ref.rsplit("/", 1)[-1], {}) if ref else {}
            for sub, sp in (item.get("properties") or {}).items():
                bounds.append((f"{sub}.maximum", sp.get("maximum")))
            for what, bound in bounds:
                if bound is not None and not _mentions(text, bound):
                    problems.append(f"{name}.{p}: {what}={bound} not in its description")
    cart = s.tool("cart_link_create")
    if cart and s.expect.max_cart_lines is not None:
        lines = next(t for t in s.tools() if t["name"] == cart)["inputSchema"]["properties"]
        got = lines.get("lines", {}).get("maxItems")
        if got != s.expect.max_cart_lines:
            problems.append(f"{cart}: maxItems {got} != limits.max_cart_lines")
    return _verdict(problems)


def _page_problems(where: str, page: dict[str, Any]) -> list[str]:
    meta, items = page.get("meta") or {}, page.get("items") or []
    try:
        total, limit, pg, pages = meta["total"], meta["limit"], meta["page"], meta["pages"]
    except KeyError as e:
        return [f"{where}: meta without {e}"]
    problems = []
    if total < len(items):
        problems.append(f"{where}: total {total} < {len(items)} items")
    if pages != math.ceil(total / limit):
        problems.append(f"{where}: pages {pages} != ceil({total}/{limit})")
    if meta.get("has_more") != (pg < pages) and not meta.get("truncated"):
        problems.append(f"{where}: has_more {meta.get('has_more')} with page {pg} of {pages}")
    return problems


def check_search(s: Session) -> Outcome:
    """C6: the sample query finds something, paging metadata is consistent, and a limit
    above 10 is honoured where max_page_size allows it."""
    search = s.tool("products_search")
    if search is None:
        return "skip", "no search tool"
    q = s.expect.sample_query
    first = s.call(search, {"q": q} if q else {})
    if first.is_error:
        return "fail", f"search({q!r}): {first.text}"
    page = first.structured or {}
    problems = _page_problems(f"search({q!r})", page)
    if not page.get("items"):
        problems.append(f"search({q!r}) found nothing")
    size = s.page_size()
    if size > 10:
        big = s.call(search, {"limit": size})
        if big.is_error:
            problems.append(f"limit={size} refused: {big.text}")
        else:
            data = big.structured or {}
            meta, items = data.get("meta") or {}, data.get("items") or []
            problems += _page_problems(f"search(limit={size})", data)
            want = min(size, meta.get("total", 0))
            if meta.get("limit") != size:
                problems.append(f"limit={size} answered with meta.limit {meta.get('limit')}")
            if len(items) != want and not meta.get("truncated"):
                problems.append(f"limit={size}: {len(items)} items of {want} expected")
    return _verdict(problems, f"limit up to {size}")


def check_lookup(s: Session) -> Outcome:
    """C7: details returns the asked id and lists missing ones; nothing found and an
    unknown barcode are isError results with code not_found."""
    problems = []
    details = s.tool("product_details")
    ids = s.product_ids(1)
    if details and ids:
        got = s.call(details, {"ids": ids})
        items = (got.structured or {}).get("items") or []
        if got.is_error or not items or items[0].get("id") != ids[0]:
            problems.append(f"details({ids}) did not return {ids[0]}: {got.text}")
        part = s.call(details, {"ids": [ids[0], MISSING_ID]})
        if part.is_error or MISSING_ID not in (part.structured or {}).get("missing", []):
            problems.append(f"details([{ids[0]}, {MISSING_ID}]) does not list it as missing")
    if details:
        none = s.call(details, {"ids": [MISSING_ID]})
        if not none.is_error or (none.error() or {}).get("code") != "not_found":
            problems.append(f"details([{MISSING_ID}]) is not a not_found error: {none.text}")
    barcode = s.tool("product_by_barcode")
    if barcode:
        miss = s.call(barcode, {"barcode": UNKNOWN_BARCODE})
        if not miss.is_error or (miss.error() or {}).get("code") != "not_found":
            problems.append(f"barcode {UNKNOWN_BARCODE} is not a not_found error: {miss.text}")
        if s.expect.sample_barcode:
            hit = s.call(barcode, {"barcode": s.expect.sample_barcode})
            if hit.is_error or s.expect.sample_barcode not in (hit.structured or {}).get(
                "barcodes", []
            ):
                problems.append(f"sample_barcode {s.expect.sample_barcode} not found")
    if not details and not barcode:
        return "skip", "no details or barcode tool"
    return _verdict(problems)


def check_markup(s: Session) -> Outcome:
    """C8: no HTML entity or tag in any string of any structuredContent."""
    s.sweep()
    problems = [
        f"{c.name}{path}: {text[:60]!r}"
        for c in s.calls
        for path, text in _strings(c.structured)
        if _MARKUP.search(text)
    ]
    return _verdict(problems[:5], f"{len(s.calls)} responses clean")


def check_compact(s: Session) -> Outcome:
    """C9: content[0].text is compact JSON equal to structuredContent (errors: compact
    JSON with no structuredContent)."""
    s.sweep()
    problems = []
    for c in s.calls:
        text = c.text or ""
        try:
            doc = json.loads(text)
        except ValueError:
            if not c.is_error:
                problems.append(f"{c.name}: text is not JSON")
            continue  # SDK argument errors are plain text: not ours to shape
        if text != _compact(doc):
            problems.append(f"{c.name}: text is not compact")
        if not c.is_error and doc != c.structured:
            problems.append(f"{c.name}: text != structuredContent")
    return _verdict(sorted(set(problems))[:5], f"{len(s.calls)} responses")


def check_budget(s: Session) -> Outcome:
    """C10: every response fits max_response_bytes, the biggest pages included; search
    carries at most 10 facets and no dictionaries (categories is its own tool)."""
    s.sweep()
    budget = s.expect.max_response_bytes
    search, details = s.tool("products_search"), s.tool("product_details")
    if search:
        s.call(search, {"mode": "full", "limit": s.page_size()})
    if details:
        ids = s.product_ids(20)
        if ids:
            s.call(details, {"ids": ids})
    problems = []
    for c in s.calls:
        size = len((c.text or "").encode())
        if budget is not None and size > budget:
            problems.append(f"{c.name}({_compact(c.args)[:40]}): {size} > {budget} bytes")
        if c.name == search and not c.is_error:
            data = c.structured or {}
            if extra := sorted(set(data) - {"meta", "items", "facets"}):
                problems.append(f"{search} carries {extra}")
            facets = (data.get("facets") or {}).get("categories") or []
            if len(facets) > MAX_FACETS:
                problems.append(f"{search}: {len(facets)} facets > {MAX_FACETS}")
    biggest = max((len((c.text or "").encode()) for c in s.calls), default=0)
    note = f"max {biggest} bytes" + (f" of {budget}" if budget else ", budget unknown (no config)")
    return _verdict(sorted(set(problems))[:5], note)


def check_cart(s: Session) -> Outcome:
    """C11: a cart of two found products hands off by the store's strategy; an unknown
    id is an isError result."""
    cart = s.tool("cart_link_create")
    if cart is None:
        return "skip", "no cart tool"
    ids = s.product_ids(2)
    if len(ids) < 2:
        return "fail", f"search gives {len(ids)} products, a cart needs 2"
    res = s.call(cart, {"lines": [{"id": ids[0], "quantity": 2}, {"id": ids[1], "quantity": 1}]})
    if res.is_error:
        return "fail", f"cart of {ids}: {res.text}"
    data = res.structured or {}
    kind, problems = data.get("kind"), []
    expected = s.expect.cart_strategy
    degraded = expected == "url_template" and kind == "link_list" and data.get("note")
    if expected and kind != expected and not degraded:
        problems.append(f"kind {kind!r} != cart.strategy {expected!r}")
    if kind in ("url_template", "share_link"):
        url = urllib.parse.urlsplit(str(data.get("url") or ""))
        site = urllib.parse.urlsplit(s.expect.site or "").hostname
        if url.scheme != "https":
            problems.append(f"url {data.get('url')!r} is not https")
        if kind == "url_template":
            if site and url.hostname != site:
                problems.append(f"url host {url.hostname!r} is not the site {site!r}")
            full = urllib.parse.unquote(data.get("url") or "")
            problems += [f"url lacks id {i!r}" for i in ids if i not in full]
    else:
        got = [link.get("product_id") for link in data.get("links") or []]
        if got != ids:
            problems.append(f"links {got} != {ids}")
    bad = s.call(cart, {"lines": [{"id": MISSING_ID, "quantity": 1}]})
    if not bad.is_error:
        problems.append(f"unknown id {MISSING_ID} is not an error: {bad.text}")
    return _verdict(problems, f"{kind}")


def check_transport(s: Session) -> Outcome:
    """C12: no stream is held open (GET on the path, POST subscriptions/listen) and junk
    POSTs are not a 500."""
    problems = []
    get = http("GET", s.url, headers={"accept": "text/event-stream"}, timeout=STREAM_TIMEOUT)
    if get.status != 405:
        problems.append(f"GET {s.path}: {get.status or get.error}, expected 405")
    listen = s.post(
        {
            "jsonrpc": "2.0",
            "id": "listen-1",
            "method": "subscriptions/listen",
            "params": {
                "notifications": {"toolsListChanged": True},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": LISTEN_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
        {"mcp-protocol-version": LISTEN_VERSION, "mcp-method": "subscriptions/listen"},
        timeout=STREAM_TIMEOUT,
    )
    if listen.status is None or "event-stream" in listen.headers.get("content-type", ""):
        problems.append(f"POST subscriptions/listen opens a stream ({listen.error or 'SSE'})")
    for url, body in ((f"{s.origin}/", b"hello"), (s.url, b"hello")):
        r = http("POST", url, body, {"content-type": "text/plain"}, timeout=STREAM_TIMEOUT)
        if r.status is None or r.status >= 500:
            problems.append(f"POST {url} junk: {r.status or r.error}")
    return _verdict(problems, "GET 405, no listen stream")


def check_auth(s: Session) -> Outcome:
    """C13 (auth): a personal tool without a token is 401 with resource_metadata and
    scope; the RFC 9728 metadata names the public URL; a public tool needs no token."""
    personal = s.personal()
    if s.expect.auth_enabled is False:
        return "skip", "auth disabled"
    if not personal:
        return "skip", "no personal tools"
    problems = []
    for name, scopes in personal.items():
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
        r = s.post(call)
        challenge = r.headers.get("www-authenticate", "")
        if r.status != 401:
            problems.append(f"{name} without a token: HTTP {r.status}, expected 401")
            continue
        if 'resource_metadata="' not in challenge:
            problems.append(f"{name}: no resource_metadata in WWW-Authenticate")
        if f'scope="{" ".join(scopes)}"' not in challenge:
            problems.append(f"{name}: WWW-Authenticate lacks scope={' '.join(scopes)!r}")
    prm = http("GET", f"{s.origin}/.well-known/oauth-protected-resource{s.path}")
    resource = s.expect.public_url or s.url
    if prm.status != 200:
        problems.append(f"protected resource metadata: HTTP {prm.status}")
    elif prm.json().get("resource") != resource:
        problems.append(f"metadata resource {prm.json().get('resource')!r} != {resource!r}")
    public = s.tool("store_info") or next((n for n in s.names() if n not in personal), None)
    if public:
        r = s.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": public}})
        if r.status != 200:
            problems.append(f"public {public} without a token: HTTP {r.status}")
    return _verdict(problems, f"{sorted(personal)} gated")


def check_rate_limit(s: Session) -> Outcome:
    """C14 (local only): the (rate_per_minute + 1)-th request within a minute is 429 with
    Retry-After."""
    if not s.local:
        return "skip", "only against a server the runner started (would throttle a live one)"
    rate = s.expect.rate_per_minute
    if rate is None:
        return "skip", "rate_per_minute unknown (no config)"
    for n in range(1, rate + 2):
        r = http("POST", s.url, b"{}", {"content-type": "application/json"})
        if r.status == 429:
            retry = r.headers.get("retry-after", "")
            if not retry.isdigit() or int(retry) < 1:
                return "fail", f"429 without a valid Retry-After ({retry!r})"
            return "ok", f"429 at request {n} (earlier checks spent part of the bucket)"
    return "fail", f"{rate + 1} requests without a 429"


Check = Callable[[Session], Outcome]
CHECKS: tuple[tuple[str, str, Check], ...] = (
    ("C1", "initialize, brand, instructions", check_initialize),
    ("C2", "tool names", check_names),
    ("C3", "tool set = capabilities", check_tool_set),
    ("C4", "schemas and annotations", check_schemas),
    ("C5", "optional params, limits in descriptions", check_params),
    ("C6", "search and paging", check_search),
    ("C7", "details, missing, not_found", check_lookup),
    ("C8", "no HTML in data", check_markup),
    ("C9", "compact text = structuredContent", check_compact),
    ("C10", "response budget, facets", check_budget),
    ("C11", "cart handoff", check_cart),
    ("C12", "no held-open streams, no 500", check_transport),
    ("C13", "auth: 401, RFC 9728", check_auth),
    ("C14", "rate limit", check_rate_limit),
)


def run(url: str, expect: Expect, *, local: bool) -> list[CheckResult]:
    session = Session(url, expect, local)
    results = []
    for cid, title, check in CHECKS:
        try:
            status, detail = check(session)
        except CheckError as e:
            status, detail = "fail", str(e)
        except Exception as e:  # noqa: BLE001 - a crashing check is a failed check
            status, detail = "fail", f"{type(e).__name__}: {e}"
        results.append(CheckResult(cid, title, status, detail))
    return results

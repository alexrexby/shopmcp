# Conformance checks

**English** · [Русский](../ru/conformance.md)

`shopmcp check` runs 15 checks (C1-C15) against an MCP endpoint and prints one line per check. They encode the defects the framework refuses to ship, see [design-notes.md](../design-notes.md). The same set runs against a local build of a store and against a live URL.

Contents: [Modes](#modes) · [Reading the report](#reading-the-report) · [The checks](#the-checks) · [Run against a live URL](#run-against-a-live-url) · [Common failures](#common-failures)

## Modes

| Command | What happens |
|---|---|
| `shopmcp check <store.toml>` | Builds the store's own app, serves it on a free `127.0.0.1` port with a temporary index and checks it. Expectations (prefix, brand, limits, tool set, scopes) come from the config |
| `shopmcp check --url <URL>` | Checks a running endpoint, no expectations: they are inferred from the server and from the framework's canonical tool set |
| `shopmcp check --url <URL> <store.toml>` | Checks the live endpoint and holds it to the config |

A local run adds `127.0.0.1:*` to `server.allowed_hosts` for its own request, so a production config that lists only the public host still works. If an `adapter` value in the config is an `https://` URL (the feed, for example), a local run refuses to start without `--online`:

```text
error: the store reads adapter.feed over the network; rerun with --online
```

`--online` is the permission to read the store over the network: a local run fetches the https sources of `[adapter]` (a feed URL, or `site` of the `woocommerce` adapter) and so reads the live store. Without it such a config is refused, so a check never touches a shop by accident. With `--online` the local server waits for its first index up to **1860 s** (31 minutes), because a catalog API is read politely, page by page; without it, up to **60 s**. `--start-timeout SECONDS` sets the wait explicitly (a number above 0) in either case; when it runs out the run stops with a `RuntimeError`: `the local server did not start in N s (… a big catalog takes longer: --start-timeout)`:

```bash
uv run shopmcp check --online stores/my-shop/store.toml
uv run shopmcp check --online --start-timeout 600 stores/my-shop/store.toml
```

Both flags apply to a local run only; with `--url` nothing is started.

Exit codes: `0` nothing failed (skips do not count), `1` at least one check failed or the config is broken, `2` wrong usage (neither a config nor `--url`).

The checks talk to the endpoint with raw JSON-RPC over HTTP, not through the MCP client SDK: the SDK would validate and hide what is actually on the wire.

## Reading the report

```text
C1   ok   initialize, brand, instructions          Демо-маркет, protocol 2025-06-18
...
C6   fail search and paging                        search('CHANGE-ME') found nothing
...
C13  skip auth: 401, RFC 9728                      auth disabled
...
12 ok, 1 failed, 1 skipped
```

Columns: id, status (`ok`, `fail`, `skip`), title, detail. A check that crashes counts as failed, with the exception in the detail. Some checks list at most five problems.

## The checks

| Id | Verifies |
|---|---|
| C1 | `initialize` works; `serverInfo.name` equals `brand` (with a config); `instructions` are not empty; a protocol version is returned |
| C2 | Every tool name matches `^<prefix>_[a-z_]+$`, is at most 64 characters, names are unique. Without a config the prefix must be common to all tools |
| C3 | The tool set equals what the adapter's capabilities and `tools.disabled` imply (without a config: every tool is from the framework's canonical set); no listed tool answers `not_supported` |
| C4 | Every tool has a description, an object `inputSchema` and `outputSchema`, and an explicit boolean `annotations.readOnlyHint` |
| C5 | No parameter with a default is required; every `maxItems`/`maximum` in a schema appears as a number in the parameter or tool description; the cart's `maxItems` equals `limits.max_cart_lines` |
| C6 | `sample_query` finds something; `total`, `pages`, `has_more` are consistent; a `limit` up to `max_page_size` is honoured. Skipped if there is no search tool |
| C7 | `product_details` returns the asked id and lists a missing id in `missing`; all ids missing, and an unknown barcode, are `isError` results with code `not_found`; `sample_barcode` resolves |
| C8 | No HTML entity or tag in any string of any `structuredContent` |
| C9 | `content[0].text` is compact JSON equal to `structuredContent`; error results are compact JSON without `structuredContent` |
| C10 | Every response fits `max_response_bytes` (the biggest pages are requested on purpose); search returns only `meta`, `items`, `facets`, with at most 10 facets |
| C11 | A cart of two found products is handed off by the configured strategy (a `url_template` degraded to `link_list` with a note is accepted); URLs are `https`, a `url_template` URL points at the store site and contains the ids; an unknown id is an `isError` result. Skipped if there is no cart tool |
| C12 | `GET` on the MCP path is `405`; `subscriptions/listen` does not open a stream; junk `POST` bodies do not produce a 5xx |
| C13 | Auth. A personal tool called without a token is `401` with `resource_metadata` and the right `scope` in `WWW-Authenticate`; `/.well-known/oauth-protected-resource<path>` answers `200` and names the public URL as `resource`; a public tool needs no token. Skipped if `auth.enabled = false` or there are no personal tools |
| C14 | Rate limit: request number `rate_per_minute + 1` gets `429` with a valid `Retry-After`. Local runs only (a live server would be throttled), and it needs a config. Runs last, after C15: it empties the bucket |
| C15 | CORS. A preflight (`OPTIONS`) from a foreign origin gets no `Access-Control-Allow-Origin`. With `server.allowed_origins` set, a preflight from the first listed origin (a `:*` entry gets port `8443`) is `2xx` with exactly that origin, `POST` in `Access-Control-Allow-Methods` and `content-type`, `authorization`, `mcp-protocol-version` in `Access-Control-Allow-Headers`, and a `POST` from it carries the same `Access-Control-Allow-Origin`. Without a config only the foreign side is checked |

Limits of the suite:

- Personal tools are called without a token only; the authorized path (a valid token, scope accepted) is not exercised. See [auth.md](auth.md) for how to check it by hand.
- With the `yml` adapter there are no personal tools, so C13 is always skipped in practice.
- The checks call read-only tools; with `cart.strategy = "share_link"`, C11 creates a real shared cart at the store.

## Run against a live URL

```bash
uv run shopmcp check --url https://shop.example/mcp stores/shop/store.toml
```

The report for the demo store running in Docker (`docker compose up -d --build`), with the config:

```text
C1   ok   initialize, brand, instructions          Демо-маркет, protocol 2025-06-18
C2   ok   tool names                               8 tools
C3   ok   tool set = capabilities                  8 tools match (config)
C4   ok   schemas and annotations                  8 tools
C5   ok   optional params, limits in descriptions  
C6   ok   search and paging                        limit up to 20
C7   ok   details, missing, not_found              
C8   ok   no HTML in data                          17 responses clean
C9   ok   compact text = structuredContent         17 responses
C10  ok   response budget, facets                  max 8828 bytes of 24000
C11  ok   cart handoff                             url_template
C12  ok   no held-open streams, no 500             GET 405, no listen stream
C13  skip auth: 401, RFC 9728                      auth disabled
C15  ok   CORS by allowed_origins                  CORS off
C14  skip rate limit                               only against a server the runner started (would throttle a live one)
13 ok, 0 failed, 2 skipped
```

Without the config the same server gives `C3 ... (canonical set, no config)`, `C10 ... budget unknown (no config)` `C13 skip ... no personal tools` and `C15 ... allowed_origins unknown`:

```bash
uv run shopmcp check --url http://localhost:8000/mcp
```

Practical points:

- **Pass the config when you have it.** Without it the checks cannot hold the server to its promises (brand, limits, tool set, cart strategy, scopes).
- **The live run spends the server's rate limit.** A run makes dozens of requests (on the demo store a local run reaches the limit at request 33 in C14, after earlier checks spent the rest) and a bucket holds `rate_per_minute` (60 by default). If another client or a previous run used it up, a check fails with `429 rate limited; rerun in a minute`.
- **The `Host` header must be accepted by the server.** For a public URL the host must be in `server.allowed_hosts` of the deployed config.
- **Run it after every deploy and config change**; it is the closest thing to an acceptance test of the endpoint a client will actually talk to.

## Common failures

| Report | Cause |
|---|---|
| `C6 fail ... search('CHANGE-ME') found nothing` | `conformance.sample_query` still has the scaffold placeholder, or the feed has no such product |
| `C3 fail ... extra [...]` / `missing [...]` | The live server's tool set differs from the config: another config version is deployed, or `tools.disabled` changed |
| `C3 fail ... listed but not_supported [...]` | The adapter declares a capability whose method raises `NotSupported` |
| `C5 fail ... maximum=N not in its description` | A parameter bound is not mentioned in the description. Descriptions built by the framework always mention it; check your `[texts]` override |
| `C8 fail ... '&nbsp;...'` | Markup leaks from the source; pass the value through `clean_text()` in the adapter |
| `C10 fail ... N > M bytes` | A response is bigger than `limits.max_response_bytes`; check what the adapter returns in one item |
| `C11 fail ... kind 'x' != cart.strategy 'y'` | The live server runs another cart strategy than the config |
| `C13 fail ... HTTP 200, expected 401` | `auth.enabled` is `false` on the deployed server, or the tool has no scope |
| `C12 fail GET /mcp: ... expected 405` | The path does not answer `GET` with `405`: the endpoint is not this framework, or a proxy in front changes the answer |

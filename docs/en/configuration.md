# Configuration: store.toml

**English** · [Русский](../ru/configuration.md)

A store is described by one TOML file. It is loaded in three steps: parse TOML, substitute `${ENV}`, validate against the models in `src/shopmcp/config.py`. Unknown keys are rejected in every section except `[adapter]`, so a typo fails the start instead of being ignored. A working example: [`stores/demo/store.toml`](../../stores/demo/store.toml); the generated template: `src/shopmcp/templates/store.toml.tmpl`.

Contents: [Environment variables](#environment-variables) · [Top level](#top-level) · [adapter](#adapter) · [cart](#cart) · [limits](#limits) · [server](#server) · [auth](#auth) · [tools](#tools) · [texts](#texts) · [conformance](#conformance) · [Errors](#errors)

## Environment variables

Secrets never go into the file. Write `${VAR}` inside any string value (nested tables and lists included) and set `VAR` in the process environment. A variable that is not set fails the start with the config path in the message, e.g. `error: store.toml.adapter.feed: environment variable FEED_TOKEN is not set`; it is never replaced by an empty string.

```toml
[adapter]
kind = "yml"
feed = "https://feeds.example/export.xml?token=${FEED_TOKEN}"
```

Besides the config, the process reads one variable:

| Variable | Purpose | Default |
|---|---|---|
| `SHOPMCP_DATA_DIR` | Directory of the search index (`<store id>.sqlite`); `serve` and `index` must agree on it | `data` (`/data` in the Docker image) |

## Top level

| Field | Purpose | Default |
|---|---|---|
| `id` | Store id, `^[a-z][a-z0-9-]{1,31}$`. Names the index file and the `stores/<id>/` directory | required |
| `prefix` | Tool name prefix, `^[a-z][a-z0-9]{1,15}$`: tools are called `<prefix>_products_search` etc. | required |
| `brand` | Store name the model sees; becomes `serverInfo.name` | required |
| `site` | Store website URL | required |
| `locale` | `ru` or `en`: language of every model-facing text (tool and parameter descriptions, instructions, errors) | `ru` |
| `instructions` | What the store sells, prices, regions. Added to the server instructions after the generated intro | empty |
| `phone`, `delivery`, `payment` | Free text, returned by the `store_info` tool | not set |
| `regions` | List of service regions, returned by `store_info` | `[]` |

The server `instructions` are assembled from: an intro (`instructions.intro`), your `instructions`, a cart rule if the cart tool exists, and a sign-in rule if any tool is personal. `locale` decides the language of the generated parts; your own text is used as written.

## adapter

`kind` selects the adapter; every other key in the table belongs to that adapter and is validated by it. Registered kinds: `yml`, `bitrix`, `insales`, `http_json` (the last three are stubs, see [adapters.md](adapters.md)). An unknown kind fails with the list of known ones.

`yml` keys:

| Key | Purpose | Default |
|---|---|---|
| `feed` | `https://` URL or a path; a relative path starts at the directory of `store.toml`, not the current directory | required |
| `refresh_minutes` | How often the index is rebuilt in the background; `0` disables the refresh | `60` |
| `max_feed_mb` | Feed size limit in megabytes; a larger feed is refused | `200` |
| `stale_minutes` | An index older than this is stale: `/healthz` answers `503`. Must be above `refresh_minutes`; ignored when `refresh_minutes = 0` | `3 × refresh_minutes` |

Any other key under `[adapter]` with `kind = "yml"` is an error.

## cart

How the cart is handed to the store site. The tool is `<prefix>_cart_link_create`; it checks every id against the catalog first, so an unknown id is an error that lists the missing ids.

| Field | Purpose | Default |
|---|---|---|
| `strategy` | `link_list`, `url_template` or `share_link` | `link_list` |
| `url_template` | URL with the `{items}` placeholder. Required for `url_template` | not set |
| `item_template` | One cart line with `{id}` and `{qty}`. Required for `url_template` (must contain `{id}`) | not set |
| `item_separator` | String between lines | `&` |

Strategies:

- `link_list` works for any store: links to product pages with quantities.
- `url_template` builds one URL, e.g. `https://shop.example/cart/add?id[]=1&qty[]=2&id[]=5&qty[]=1`. Ids are URL-quoted. If the URL would exceed 2000 characters, the answer degrades to `link_list` and carries a note.
- `share_link` asks the adapter to create a shared cart on the store side. The adapter must declare the `share_link` capability, otherwise the start fails. The tool is then marked as non-read-only (it creates an object at the store).

## limits

| Field | Purpose | Range | Default |
|---|---|---|---|
| `max_page_size` | Largest `limit` a paged tool accepts | 1-100 | `20` |
| `max_cart_lines` | Lines in one cart; becomes `maxItems` of the cart schema | 1-200 | `30` |
| `max_quantity` | Quantity per cart line | 1-10000 | `99` |
| `max_response_bytes` | Size budget of a tool response. A paged answer over the budget loses items from the end and gets `meta.truncated = true` | >= 1024 | `24000` |
| `rate_per_minute` | Requests per minute per client IP (token bucket); excess gets `429` with `Retry-After`. `/healthz` is not counted | >= 1 | `60` |

The limits are written into the tool schemas and descriptions, so the model sees the same numbers the server enforces (check C5 verifies it).

## server

| Field | Purpose | Default |
|---|---|---|
| `path` | MCP endpoint path, `^/[a-z0-9/_-]*$` | `/mcp` |
| `host` | Bind address | `0.0.0.0` |
| `port` | Port, 1-65535 | `8000` |
| `public_url` | Public URL of the endpoint. It is the protected resource in RFC 9728 and the required token audience | required |
| `allowed_hosts` | Accepted `Host` header values (DNS-rebinding protection); `*` as the port wildcard | `["localhost:*", "127.0.0.1:*"]` |
| `allowed_origins` | Accepted `Origin` header values; `*` as the port wildcard (`http://localhost:*`). A non-empty list also turns on CORS on the MCP path for these origins, for browser clients | `[]` |
| `trusted_proxies` | Addresses or networks whose `X-Forwarded-For` is trusted for the rate limit | `[]` |

Behind a reverse proxy: add the public host (e.g. `"mcp.shop.example"`) to `allowed_hosts`, set `public_url` to the public address, and list the proxy in `trusted_proxies`. Without `trusted_proxies` all requests look like they come from the proxy and share one rate-limit bucket. `X-Forwarded-For` is read right to left up to the first address that is not trusted. IPv6 clients are limited per /64.

CORS: with `allowed_origins` empty (the default) a browser on another origin cannot call the endpoint; server-side clients do not need CORS. A listed origin gets `204` on the preflight and `Access-Control-Allow-Origin` with its own value on every answer of the MCP path, `401`, `403` and `429` included. Allowed request headers are `content-type`, `authorization`, `last-event-id` and `mcp-*`. `*` is never sent and credentials (cookies) are not allowed: the token goes in `Authorization`.

The server answers `POST` only on `path`; `GET` returns `405`, and no stream is ever held open.

## auth

| Field | Purpose | Default |
|---|---|---|
| `enabled` | Turn token verification on | `false` |
| `issuer` | Expected `iss` of the token and the authorization server advertised in the metadata. A bare host matches with and without the trailing slash, see [auth.md](auth.md) | empty |
| `jwks_uri` | URL of the authorization server's key set | empty |
| `tool_scopes` | Table `tool suffix = ["scope", ...]`: scopes required to call the tool; overrides the tool's default. A list must not be empty | `{}` |

`enabled = true` requires both `issuer` and `jwks_uri`. A key in `tool_scopes` that is not the suffix of a known tool fails the start. Details, error answers and the model of trust: [auth.md](auth.md).

```toml
[auth]
enabled = true
issuer = "https://auth.shop.example"
jwks_uri = "https://auth.shop.example/.well-known/jwks.json"
[auth.tool_scopes]
orders_history = ["orders.read"]
```

## tools

| Field | Purpose | Default |
|---|---|---|
| `disabled` | Tool suffixes to hide (`"product_analogs"`), whatever the adapter can do | `[]` |

A suffix that matches no tool is ignored without a message.

## texts

Every text the model reads exists in two languages inside the framework; `locale` picks one. `[texts]` replaces any of them, in the store's language, by key:

```toml
locale = "en"

[texts]
"tool.products_search.description" = "Search the Example Market catalog."
"tool.products_search.param.q" = "Search words, e.g. a product name"
"instructions.intro" = "You work with the catalog of {brand} ({site})."
```

Key forms:

| Key | Text |
|---|---|
| `tool.<suffix>.title` | Tool title |
| `tool.<suffix>.description` | Tool description |
| `tool.<suffix>.param.<name>` | Description of one tool parameter |
| `instructions.intro`, `instructions.cart`, `instructions.sign_in` | Parts of the server instructions |
| `cart.note.url_too_long` | Note when a `url_template` cart degrades to a link list |
| `error.<code>` | Default message of an error code: `not_found`, `invalid_input`, `upstream_unavailable`, `not_supported`, `unauthorized`, `internal_error` |

Rules:

- Keys match `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`. A key that no text uses fails the start, so a typo cannot silently do nothing.
- Keys of a disabled tool stay valid: the check runs against the full set of framework tools.
- Placeholders in the instructions are substituted by plain replacement: `{brand}`, `{site}` (intro), `{cart_tool}` (cart), `{personal_tools}` (sign-in). An override may omit or repeat them.
- Tool names, schemas and field names are not texts and cannot be overridden.
- The exact parameter names and built-in texts are in `src/shopmcp/tools/*.py` and `src/shopmcp/server/instructions.py`.

## conformance

Input of `shopmcp check`, see [conformance.md](conformance.md).

| Field | Purpose | Default |
|---|---|---|
| `sample_query` | A query that must find at least one product | required |
| `sample_barcode` | A barcode that must resolve to a product (checked if the barcode tool exists) | not set |

## Errors

A broken config stops the command with exit code `1` and a message with the field path. Input values are left out of the message because after `${ENV}` substitution they may be secrets. Real examples:

```text
error: bad.toml: server.public_url: Field required
error: bad.toml.adapter.feed: environment variable NOPE_FEED is not set
error: bad.toml: auth: Value error, auth.enabled requires auth.issuer and auth.jwks_uri
error: texts: unknown keys ['tool.nope.description']
```

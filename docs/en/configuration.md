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

`kind` selects the adapter; every other key in the table belongs to that adapter and is validated by it. Registered kinds: `yml` and `feed` (one adapter, see below), `woocommerce`, `bitrix`, `insales`, `http_json` (the last three are stubs, see [adapters.md](adapters.md)). An unknown kind fails with the list of known ones.

`yml` and `feed` keys (`feed` is the same adapter under the name scaffold writes for engine presets):

| Key | Purpose | Default |
|---|---|---|
| `feed` | `https://` URL or a path; a relative path starts at the directory of `store.toml`, not the current directory | required |
| `format` | `auto`, `yml` or `gmc`. `auto` tells the formats apart by the root element: `yml_catalog` is YML, `rss` and Atom `feed` are Google Merchant. An explicit value that disagrees with the root fails the read | `auto` |
| `refresh_minutes` | How often the index is rebuilt in the background; `0` disables the refresh | `60` |
| `max_feed_mb` | Feed size limit in megabytes; a larger feed is refused | `200` |
| `stale_minutes` | An index older than this is stale: `/healthz` answers `503`. Must be above `refresh_minutes`; ignored when `refresh_minutes = 0` | `3 x refresh_minutes` |
| `cart_ref` | Table `[adapter.cart_ref]`, see below | not set |

Any other key under `[adapter]` with these kinds is an error.

`woocommerce` keys:

| Key | Purpose | Default |
|---|---|---|
| `site` | `https://` URL of the WordPress site, without query: `https://shop.example` or `https://shop.example/shop` | required |
| `refresh_minutes`, `stale_minutes` | As for `yml` | `60`, `3 x refresh_minutes` |
| `max_pages` | Most pages (100 products each) read from one listing; a bigger shop is refused with this key named | `200` |
| `max_minutes` | Time budget of one whole read, in minutes | `30` |
| `max_feed_mb` | Size limit of the bytes of the whole read, every page counted | `200` |

The `woocommerce` adapter has no `cart_ref` and no `feed`. How it reads the shop: [adapters.md](adapters.md#the-woocommerce-adapter).

### adapter.cart_ref

The cart link needs the key the store's cart knows, which is not always the feed `id`. `[adapter.cart_ref]` says how to get it from a feed id. Without the table the default applies: an offer with a group (`group_id` in YML, `g:item_group_id` in GMC) is a variant of that group (`{product_id}` is the group, `{variant_id}` and `{id}` are the offer id); an offer without a group is a product (`{id}` is its id).

| Key | Purpose |
|---|---|
| `pattern` | A regular expression matched against the whole feed id (full match) with the named group `product_id` and, optionally, `variant_id`; no other named groups. An id it does not match falls back to the default |
| `options` | Table `store option name = "feed param name"`: the value of the offer's `param` (YML) or attribute (GMC: `color`, `size`, `material`, `pattern`, `gender`, `age_group`) goes to the cart as that option. Up to 20 entries |

```toml
[adapter.cart_ref]
pattern = "^(?P<product_id>[0-9]+)-(?P<variant_id>[0-9]+)$"   # feed id "12-34"
options = { Size = "size" }
```

An unknown key, a pattern that does not compile, or wrong group names fail the start with the path of the key.

## cart

How the cart is handed to the store site. The tool is `<prefix>_cart_link_create`; it checks every id against the catalog first, so an unknown id is an error that lists the missing ids. `scaffold --engine` writes a ready `[cart]` for each engine, see [engines.md](engines.md).

`strategy` selects the strategy and decides which other keys exist; a key of another strategy is an error. Without `[cart]`, or without `strategy`, it is `link_list`.

| Strategy | Who fills the cart | Keys |
|---|---|---|
| `link_list` | the customer, from product page links | `line_template` |
| `url_template` | the store, from one GET link | `url_template`, `item_template`, `item_separator` |
| `json_fragment` | the store script, from the URL fragment (Ecwid) | `url_template`, `payload_encoding` |
| `form_post` | our trampoline page, by POST to the store (beta) | `action`, `mode`, `fields`, `finish_url`, `secret`, `ttl_minutes` |
| `receiver` | our module on the store site, from a signed link | `url_template`, `secret`, `key_id`, `ttl_minutes` |
| `share_link` | the store API, a shared cart | none |

| Key | Strategy | Purpose | Default |
|---|---|---|---|
| `line_template` | `link_list` | One add-to-cart link per line (returned as `add_url`); must contain `{id}` or `{product_id}`. Without it only product page links are returned | not set |
| `url_template` | `url_template`, `json_fragment`, `receiver` | The cart URL; must contain `{items}`, `{payload}` or `{token}` respectively | required |
| `item_template` | `url_template` | One cart line; must contain `{id}` or `{product_id}` | required |
| `item_separator` | `url_template` | String between lines | `&` |
| `payload_encoding` | `json_fragment` | `uri` (percent-encoded JSON) or `base64` (Ecwid on Wix) | `uri` |
| `action` | `form_post` | URL the form is posted to | required |
| `mode` | `form_post` | `single`: one form with all lines; `chain`: one form per line, the store sends the browser back | required |
| `fields` | `form_post` | List of `[name, value]` pairs, 1-10, both are templates; a field that renders empty is not sent. At least one must contain `{id}` or `{product_id}` | required |
| `finish_url` | `form_post` | Where a chain ends: the cart page. Required for `chain` | not set |
| `secret` | `form_post`, `receiver` | Signing key: exactly one `${ENV}` reference (a literal in the file is refused), at least 32 bytes after substitution | required |
| `ttl_minutes` | `form_post`, `receiver` | Lifetime of a signed link, 5-10080 | `1440` |
| `key_id` | `receiver` | Key id in the token (`kid`), for rotation: `^[A-Za-z0-9._-]{1,32}$` | `1` |

Placeholders of the templates. They are replaced in one pass (a substituted value is never read as a placeholder again), and every value is percent-encoded whole, so it is safe in a path, a query or a fragment:

| Placeholder | Value |
|---|---|
| `{id}` | the most specific cart id: the variant id if there is a variant, else the product id |
| `{product_id}` | the product id of the cart key |
| `{variant_id}` | the variant id; empty without a variant |
| `{qty}` | quantity |
| `{url}` | product page URL; not encoded where it starts the template (`{url}?action=...`), encoded in a query value |
| `{items}` | `url_template`: the lines, each filled from `item_template`, joined by `item_separator` |
| `{payload}` | `json_fragment`: the cart as JSON in the chosen encoding |
| `{token}` | `receiver`: the signed token |

In a `line_template` a query parameter whose placeholder rendered empty is left out (`id_product_attribute=` of a product with no combinations). In an `item_template` it stays, because parallel arrays (`product[]`, `variant[]`) pair lines by position.

Strategies:

- `link_list` works for any store: links to product pages with quantities. With `line_template`, e.g. `https://shop.example/cart/?add-to-cart={id}&quantity={qty}`, every line also carries a link that adds that line.
- `url_template` builds one URL, e.g. `https://shop.example/cart/add?id[]=1&qty[]=2&id[]=5&qty[]=1`. If the URL would exceed 2000 characters, the answer degrades to `link_list` and carries a note.
- `json_fragment`: a `url_template` such as `https://shop.example/#!/~/cart/create={payload}`; the cart is `{"products":[{"id":…,"quantity":…,"options":{…}}]}`. The same 2000-character degradation.
- `form_post` is **beta**: the keys are validated, but the trampoline page is not in the code yet and a store with this strategy does not start. See [engines.md](engines.md#form_post-beta).
- `receiver` builds a link to a module on the store site; the token format and the module contract are in [cart-receiver.md](cart-receiver.md). The same 2000-character degradation; the answer carries `expires_at`.
- `share_link` asks the adapter to create a shared cart on the store side. The adapter must declare the `share_link` capability, otherwise the start fails. The tool is then marked as non-read-only (it creates an object at the store).

Every URL of the section (`url_template`, `line_template`, `action`, `finish_url`) must point at the host of `site` or its subdomain, otherwise the start fails: a cart link to another host would make the store an open redirect. A `line_template` that starts with `{url}` is exempt, it begins at the product page the feed owns.

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

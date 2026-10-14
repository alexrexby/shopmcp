# Auth: hybrid access and OAuth resource server

**English** · [Русский](../ru/auth.md)

Why the access model is hybrid, in one line: browsing a catalog needs no account, an order history does. See [design-notes.md](../design-notes.md).

Contents: [Model](#model) · [Configuration](#configuration) · [Per-tool scope](#per-tool-scope) · [What the server checks in a token](#what-the-server-checks-in-a-token) · [What the client sees](#what-the-client-sees) · [Checking it by hand](#checking-it-by-hand) · [Limits](#limits)

## Model

- **Public tools** (catalog, cart link, store info, pickup points) work without a token.
- **Personal tools** require a token with a scope. In the framework today that is `orders_history`, default scope `orders.read`; it exists only if the adapter declares the `orders` capability. The `yml` adapter does not, so a `yml` store has no personal tools and auth has nothing to protect.
- **shopmcp is a resource server only.** It does not issue tokens and has no `/authorize` or `/token` endpoints. Tokens come from the store's own authorization server (`auth.issuer`); the server verifies them against that server's public keys (`auth.jwks_uri`).
- The decision is made per tool call, not per endpoint: `initialize`, `tools/list` and public tool calls pass without a token. A personal tool is listed like any other and refuses the call without a token.
- The server instructions tell the model what to do on `401`: ask the customer to sign in through the client and retry, and never ask for account credentials (text `instructions.sign_in`, see [configuration.md](configuration.md#texts)).

## Configuration

```toml
[server]
public_url = "https://mcp.shop.example/mcp"   # also the required token audience

[auth]
enabled = true
issuer = "https://auth.shop.example"
jwks_uri = "https://auth.shop.example/.well-known/jwks.json"
[auth.tool_scopes]
orders_history = ["orders.read"]
```

| Field | Purpose |
|---|---|
| `auth.enabled` | `false` (default): no verification at all. `true` requires `issuer` and `jwks_uri` |
| `auth.issuer` | Expected `iss` claim, compared as the exact string you write here |
| `auth.jwks_uri` | Where the signing keys are fetched from |
| `auth.tool_scopes` | Scopes per tool, see below |
| `server.public_url` | Token `aud` must equal it; also published as the `resource` of the metadata |

With `enabled = false` there is no gate and no metadata route. A personal tool that is still listed refuses the call itself with `unauthorized`, because no token ever reaches it; turn auth on, or do not declare the `orders` capability.

## Per-tool scope

Every tool has default scopes in code; an empty list means public. `[auth.tool_scopes]` overrides them by tool suffix (the part after `<prefix>_`).

- `orders_history = ["orders.read"]`: the default, written out.
- `orders_history = ["orders.read", "profile"]`: all listed scopes are required.
- `products_search = ["catalog.read"]`: makes a public tool personal.
- An empty list is invalid; to remove a tool from the store use `tools.disabled`. A personal tool cannot be made public through `tool_scopes`.
- A key that is not the suffix of a framework tool fails the start (`auth.tool_scopes: unknown tools [...]`), so a typo cannot leave the real tool on its default scope.
- A scope is an RFC 6749 token: no spaces, no quotes, no backslashes.

The set of scopes is also published as `scopes_supported` in the metadata, and the tools made personal are named in the server instructions.

## What the server checks in a token

A `Bearer` token must be a JWT that passes all of these:

| Check | Rule |
|---|---|
| Algorithm | Asymmetric only: `RS256` or `ES256` are accepted. `none` and `HS*` are rejected before any key lookup |
| Signature | By the key from `jwks_uri` selected by the token's `kid` |
| `iss` | Equal to `auth.issuer` |
| `aud` | Equal to `server.public_url` |
| `exp` | Required, with a clock leeway of 30 seconds |
| Scopes | From `scope` (space-separated string) or `scp` (list); a missing claim means no scopes |
| Client | One of `client_id`, `azp`, `sub` must be present as a string |

Operational details:

- The key set is cached for 5 minutes; an unknown `kid` forces a refetch, at most once per 30 seconds. A JWKS request times out after 5 seconds.
- If the JWKS is unreachable, tokens fail closed, and the fetch is not retried for 10 seconds.
- Verification runs in a small separate worker pool, so a slow JWKS does not stall catalog searches.
- A token that fails any check is treated as no token. On a public tool that changes nothing: a stray bad token does not break a public call.
- Token checks count against the rate limit like any request.

## What the client sees

Without a token, a personal tool call gets `401`; with a token that lacks the scope, `403`. Both carry `WWW-Authenticate` with the metadata URL and the scope, so a client can start the OAuth flow:

```text
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="http://localhost:8001/.well-known/oauth-protected-resource/mcp", scope="orders.read"

{"error": "invalid_token", "error_description": "Authentication required"}
```

```text
HTTP/1.1 403 Forbidden
www-authenticate: Bearer error="insufficient_scope", error_description="Required scope: orders.read", resource_metadata="http://localhost:8001/.well-known/oauth-protected-resource/mcp", scope="orders.read"

{"error": "insufficient_scope", "error_description": "Required scope: orders.read"}
```

The metadata, [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728), is public and lives at `/.well-known/oauth-protected-resource<server.path>`:

```json
{"resource":"http://localhost:8001/mcp","authorization_servers":["https://auth.mini.example/"],"scopes_supported":["orders.read"],"bearer_methods_supported":["header"]}
```

Note the trailing slash: URLs in the metadata are normalized, while the `iss` check uses `auth.issuer` exactly as written. If your authorization server publishes an issuer without a slash, write it without a slash in the config.

The gate reads the request body to find the tool name, for single messages and for batches. A body over 256 KiB is refused with `413` before it reaches the server.

## Checking it by hand

[Check C13](conformance.md#the-checks) verifies the unauthenticated side: `401` with `resource_metadata` and `scope`, the metadata names the public URL, and a public tool answers without a token. It does not send a valid token. To check the authorized path, take a token from your authorization server and call the tool:

```bash
curl -s -X POST https://mcp.shop.example/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -H "authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"shop_orders_history","arguments":{}}}'
```

For the three outcomes above (401, 403, passes the gate) this was run against a test server with a generated key pair and a local JWKS: a token with `orders.read` reached the adapter, a token with another scope got `403`. Failures to look for first: `aud` does not equal `public_url`, `iss` differs from `auth.issuer` by a character, the token is signed with `HS256` or an algorithm other than `RS256`/`ES256`.

## Limits

- Only `RS256` and `ES256` are accepted; the list of algorithms is not configurable.
- Without `orders` in the adapter (every adapter in the repository today), no tool is personal, and C13 is skipped.
- The framework does not map the token to a customer in the store: the adapter's `orders(token, page, limit)` receives the verified `AccessToken` (subject, scopes, all claims) and decides whose orders to return.
- Defence in depth: `orders_history` also refuses a call that reaches it without an authenticated user (`unauthorized`), in case the gate is bypassed.

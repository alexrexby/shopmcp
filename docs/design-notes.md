**English** · [Русский](design-notes.ru.md)

# Design notes

Why the framework looks the way it does. These conclusions come from studying production MCP servers of large online retailers in 2026. No third-party schemas or data are included here.

## Patterns we adopt

1. **Hybrid access.** Catalog tools (search, product details, discounts, store locator, cart link) work without a token. Personal tools (order history, personal offers) sit behind OAuth 2.1. Without a token they return HTTP `401` with `WWW-Authenticate: Bearer resource_metadata="…", scope="<tool scope>"`. The server publishes RFC 9728 `/.well-known/oauth-protected-resource`. The store's own authorization server issues tokens; the framework only verifies them as a resource server.
2. **Cart handoff, not checkout.** The agent builds a list, and the server returns a link that fills the cart on the store's site. Payment and checkout stay with the store, so the MCP server has no payment flow and no PCI scope.
3. **Brand-prefixed tool names** (`<store>_products_search`), so several stores can coexist in one client.
4. **Field projection and explicit pagination** (`mode: short|full|custom`, `fields[]`, `has_more`) keep responses cheap in tokens.
5. **Server `instructions`** tell the model the store's rules, including what to do on `401`.

## Defects we refuse to ship

Every item below is a conformance check: a store that passes `shopmcp check` is free of it.

1. Business errors returned with `isError: false`.
2. HTML entities and markup (`&nbsp;`, `<br>`) leaking into text the model reads.
3. Pretty-printed JSON wasting tokens. We use compact JSON plus `structuredContent` and `outputSchema`.
4. Missing tool `annotations` (`readOnlyHint`, `openWorldHint`).
5. Huge first pages. Filter dictionaries are served by a separate tool, and every response has a byte budget.
6. Tool descriptions that contradict their schemas, such as "1..20 items" alongside `maxItems: 30`. Limits are generated from a single constant.
7. Optional filters declared as `required`.
8. Page size that cannot be changed where it matters.

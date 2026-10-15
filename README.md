# shopmcp

**English** · [Русский](README.ru.md)

A framework for MCP servers of online stores: a store needs only a TOML config and, if its data source is not a YML feed, an adapter.

## Features

- **Config-driven.** One `store.toml` describes the store, its limits, cart hand-off, auth and texts; `shopmcp scaffold` generates it.
- **Capability-driven tool set.** A tool exists only if the adapter declares the matching capability (search, details, barcode, analogs, categories, promotions, pickup points, orders).
- **Cart hand-off, not checkout.** The agent builds a list; the server returns a link (or product page links) and the customer pays on the store site.
- **Hybrid access.** Catalog tools are public; personal tools sit behind OAuth 2.1 as a resource server with a per-tool scope ([docs/en/auth.md](docs/en/auth.md)).
- **Bilingual model-facing texts.** `locale = "ru" | "en"` plus per-store overrides in `[texts]`.
- **Conformance suite.** `shopmcp check` runs checks C1-C15 against a local build or a live URL.

## Stack

| Part | Version |
|---|---|
| Python | >= 3.12 |
| MCP Python SDK (`mcp`) | 2.3.0 (pinned) |
| PyJWT (`pyjwt[crypto]`) | >= 2.10 |
| snowballstemmer | >= 3 |
| Package manager / build | [uv](https://docs.astral.sh/uv/), hatchling |
| Dev tools | pytest >= 8, ruff >= 0.6 |

Ready-made adapters: `yml` (a YML / Yandex Market Language or Google Merchant feed, indexed into SQLite) and `woocommerce` (a snapshot of the keyless Store API). The `bitrix`, `insales` and `http_json` adapters are stubs. See [docs/en/adapters.md](docs/en/adapters.md).

## Store engines

`shopmcp scaffold <id> --site URL` detects the engine of the store with a few GET requests (or takes `--engine`) and writes a ready `[adapter]` and `[cart]` for it. Ten engines have presets: Shopify, WooCommerce, Ecwid, 1C-Bitrix, PrestaShop, OpenCart, Magento 2, Tilda, InSales and Shop-Script. For InSales and Shop-Script the cart is product page links for now; their `form_post` cart (a trampoline page on the server that POSTs the lines to the store) is BETA and is written commented out: it is checked in Chromium on a local Shop-Script stand, not in Safari, not on a real HTTPS store and not on a live InSales. Host the server on a subdomain of the store; `shopmcp check` warns or fails when the cart could be replaced. The table, what to fill in by hand and how detection works: [docs/en/engines.md](docs/en/engines.md).

## Quick start

```bash
uv sync
uv run shopmcp check stores/demo/store.toml   # conformance run of the demo store
uv run shopmcp serve stores/demo/store.toml   # MCP endpoint: http://localhost:8000/mcp
```

Docker: `docker compose up -d --build`. Step-by-step with real output: [docs/en/quickstart.md](docs/en/quickstart.md).

## Commands

| Command | Purpose |
|---|---|
| `shopmcp serve <store.toml>` | Run the MCP server over HTTP |
| `shopmcp index <store.toml>` | Build the search index once and exit; prints the adapter health as JSON |
| `shopmcp check [store.toml] [--url URL] [--online] [--start-timeout SECONDS]` | Conformance checks, local or against a live endpoint |
| `shopmcp scaffold <store_id> --site URL [--engine X] [--feed F] [--prefix …] [--locale ru\|en]` | Create `stores/<store_id>/store.toml`; the engine is detected from the site unless `--engine` is given |

## Tools

Names are `<prefix>_<suffix>`. The demo store (prefix `demo`) exposes 8 of them:

| Suffix | Requires capability | Access |
|---|---|---|
| `products_search` | `search` | public |
| `product_details` | `details` | public |
| `product_by_barcode` | `barcode` | public |
| `product_analogs` | `analogs` | public |
| `categories` | `categories` | public |
| `promotions` | `promotions` | public |
| `pickup_points` | `pickup_points` | public |
| `cart_link_create` | `details` | public |
| `orders_history` | `orders` | scope `orders.read` |
| `store_info` | none, always present | public |

The `yml` adapter declares `search`, `details`, `barcode`, `analogs`, `categories` and `promotions`; with it a store gets `store_info` plus the six matching tools and `cart_link_create`.

## Documentation

| Document | Contents |
|---|---|
| [docs/en/quickstart.md](docs/en/quickstart.md) | Run the demo store locally and in Docker; add your own store |
| [docs/en/configuration.md](docs/en/configuration.md) | `store.toml` field by field, `${ENV}`, `locale`, `[texts]`, `[limits]`, `[auth]` |
| [docs/en/engines.md](docs/en/engines.md) | Ten supported engines: catalog, cart, status; detection; what to fill in |
| [docs/en/adapters.md](docs/en/adapters.md) | Adapter contract, capabilities, writing an adapter, the `yml` adapter, Google Merchant feed, the `woocommerce` adapter |
| [docs/en/conformance.md](docs/en/conformance.md) | Checks C1-C15, running against a live URL |
| [docs/en/auth.md](docs/en/auth.md) | Hybrid access, OAuth resource server, per-tool scope |
| [docs/design-notes.md](docs/design-notes.md) | Why the framework looks the way it does |

## Project layout

```text
src/shopmcp/
  cli.py            serve | index | check | scaffold
  config.py         store.toml -> ${ENV} -> pydantic models
  i18n.py           ru/en texts and [texts] overrides
  capabilities.py   capability enum
  adapters/         base contract, registry, yml (YML + Google Merchant), woocommerce, bitrix / insales / http_json (stubs)
  tools/            tool definitions
  cart/             hand-off strategies
  engines.py        presets of the ten engines for scaffold
  detect.py         engine detection by GET (scaffold)
  server/           ASGI app, auth, rate limit, instructions
  conformance/      checks C1-C15 and the runner
  templates/        store.toml template for scaffold
stores/demo/        demo store: store.toml + feed.xml
tests/              pytest suite
Dockerfile, compose.yaml
```

## Tests

```bash
uv run pytest
uv run ruff check
```

Tests marked `online` need network access to a remote source and are not meant for CI.

## License

[Apache License 2.0](LICENSE).

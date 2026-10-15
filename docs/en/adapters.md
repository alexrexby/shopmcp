# Adapters

**English** · [Русский](../ru/adapters.md)

An adapter is the only store-specific code. It reads the store's data source and maps it to the framework's domain model (`src/shopmcp/domain/models.py`). The core, the tools, the cart hand-off and the server never see raw source data.

Contents: [Status](#status) · [Capabilities](#capabilities) · [The contract](#the-contract) · [Writing an adapter](#writing-an-adapter) · [Registering it](#registering-it) · [The yml adapter](#the-yml-adapter) · [Google Merchant feed](#google-merchant-feed) · [The woocommerce adapter](#the-woocommerce-adapter)

## Status

| `adapter.kind` | State |
|---|---|
| `yml` | Ready. YML (Yandex Market Language) or Google Merchant feed from a file or an `https://` URL |
| `feed` | Ready. The same adapter as `yml`; scaffold writes this name for engine presets |
| `woocommerce` | Ready. Snapshot of the keyless WooCommerce Store API |
| `bitrix` | Stub: declares no capabilities |
| `insales` | Stub: declares no capabilities |
| `http_json` | Stub: declares no capabilities |

A store on a stub starts, but exposes only `<prefix>_store_info`: every other tool needs a capability (`cart_link_create` needs `details`). The module docstrings of the stubs describe the intended source and the `[adapter]` keys. Presets for ten engines: [engines.md](engines.md).

## Capabilities

An adapter declares what it can do with `capabilities()`, a `frozenset` of `shopmcp.capabilities.Capability`. A tool is registered only if its required capability is declared and its suffix is not in `tools.disabled`. A capability is a promise: if it is declared but the method raises `not_supported`, check C3 fails.

| Capability | Adapter method | Tool it enables |
|---|---|---|
| `search` | `search(query)` | `products_search` |
| `details` | `get_products(ids)` | `product_details`, `cart_link_create` |
| `barcode` | `by_barcode(barcode)` | `product_by_barcode` |
| `analogs` | `analogs(product_id, limit)` | `product_analogs` |
| `categories` | `categories(parent_id)` | `categories` |
| `promotions` | `promotions(page, limit, sort)` | `promotions` |
| `pickup_points` | `pickup_points(city, page, limit)` | `pickup_points` |
| `share_link` | `create_share_link(lines)` | no tool; allows `cart.strategy = "share_link"` |
| `orders` | `orders(token, page, limit)` | `orders_history`, needs a token with scope `orders.read` ([auth.md](auth.md)) |

`cart_link_create` requires `details` because the cart is checked against the catalog before the link is built. `store_info` needs nothing: it is served from `store.toml`.

## The contract

The interface is `ShopAdapter` in `src/shopmcp/adapters/base.py`. Subclass `AdapterBase`: it declares no capabilities and every data method raises `NotSupported`, so you override only what you declare.

| Method | Contract |
|---|---|
| `capabilities()` | The declared set |
| `start()` | Load or index the data. Called once from the server lifespan; raise `Unavailable` if the adapter cannot serve at all |
| `close()` | Release resources |
| `health()` | `dict` for `/healthz` (index size, feed age, last error) |
| `search(query)` | Returns `(products, total, facets)`. `SearchQuery` carries `q`, `category_id`, `price_min`, `price_max`, `in_stock`, `sort` (`relevance`, `price_asc`, `price_desc`, `new`, `discount`), `page`, `limit` |
| `get_products(ids)` | Products for the ids; absent ids are skipped, not an error |
| `by_barcode(barcode)` | A product or `None` |
| `analogs(product_id, limit)` | Similar products |
| `categories(parent_id)` | Categories under a parent; `None` means the top level |
| `promotions(page, limit, sort)` | `(products, total)` |
| `pickup_points(city, page, limit)` | `(points, total)` |
| `create_share_link(lines)` | Link to a cart created on the store side |
| `orders(token, page, limit)` | Orders of the user behind the `AccessToken`, `(orders, total)` |

Rules:

- **Raise only `shopmcp.errors` exceptions** (`NotFound`, `InvalidInput`, `Unavailable`, `NotSupported`, `Unauthorized`). Anything else is turned into `internal_error` without a stack trace for the model.
- **Pass every string from the source through `clean_text()`** (`shopmcp.domain.text`): it strips markup and HTML entities. Check C8 fails on any `&nbsp;`, `<br>` or similar in a response.
- **Ids are strings.** Prices are `Decimal`; the currency defaults to `RUB`.
- **Do not paginate beyond the query.** The tools compute `pages` and `has_more` from your `total`; the response size budget (`limits.max_response_bytes`) is enforced by the framework.
- **Secrets only via `${ENV}`** in `[adapter]`; never log them (the `yml` adapter redacts feed URLs in logs).
- The adapter receives `AdapterContext`: `data_dir` (index directory), `http_timeout` (10 s), `store_id`, `config_dir` (directory of `store.toml`; resolve relative paths against it).

## Writing an adapter

A complete minimal adapter with two capabilities, `search` and `details`. This is the code that was run through the conformance suite (13 ok, 1 skipped: auth is off) with `sample_query = "milk"`.

```python
"""A minimal adapter: three hard-coded products, search and details only."""

from collections.abc import Sequence
from decimal import Decimal

from pydantic import HttpUrl

from shopmcp.adapters.base import AdapterBase, AdapterContext, SearchQuery
from shopmcp.capabilities import Capability
from shopmcp.config import AdapterConfig
from shopmcp.domain.models import Facets, Money, Price, Product
from shopmcp.domain.text import clean_text

_SOURCE = [
    ("1", "Milk 3.2%", "89.90"),
    ("2", "Kefir 1%", "74.00"),
    ("3", "Cottage cheese", "120.00"),
]


def _product(id: str, name: str, price: str) -> Product:
    return Product(
        id=id,
        name=clean_text(name),  # every string from the source goes through clean_text
        url=HttpUrl(f"https://mini.example/p/{id}"),
        price=Price(current=Money(amount=Decimal(price), currency="RUB")),
        available=True,
    )


class MiniAdapter(AdapterBase):
    def __init__(self, cfg: AdapterConfig, ctx: AdapterContext) -> None:
        self.products = [_product(*row) for row in _SOURCE]

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.SEARCH, Capability.DETAILS})

    async def search(self, q: SearchQuery) -> tuple[list[Product], int, Facets]:
        word = (q.q or "").lower()
        found = [p for p in self.products if word in p.name.lower()]
        start = (q.page - 1) * q.limit
        return found[start : start + q.limit], len(found), Facets()

    async def get_products(self, ids: Sequence[str]) -> list[Product]:
        by_id = {p.id: p for p in self.products}
        return [by_id[i] for i in ids if i in by_id]  # absent ids are skipped


def create(cfg: AdapterConfig, ctx: AdapterContext) -> MiniAdapter:
    return MiniAdapter(cfg, ctx)
```

A factory `create(cfg, ctx)` is the module's entry point. `cfg` is `AdapterConfig`: `cfg.kind` plus the other keys of `[adapter]` in `cfg.model_extra`. Validate them there and raise `ConfigError` (from `shopmcp.config`) with a message naming the key, like the `yml` adapter does for unknown keys.

## Registering it

The kind-to-factory map is in `src/shopmcp/adapters/registry.py`.

- **Built-in kind.** Put the module in `src/shopmcp/adapters/` and add a line to `_MODULES`, e.g. `"mini": "shopmcp.adapters.mini"`. The module is imported lazily, so a broken adapter breaks only the stores that use its kind. After that `kind = "mini"` works in `store.toml` and the CLI.
- **From code** (tests, a third-party package): `registry.register("mini", factory)` before building the app. The `shopmcp` command line does not load external modules by itself, so this path is for your own entry point; the conformance runner can be driven from Python the same way:

  ```python
  from pathlib import Path
  from shopmcp.adapters import registry
  from shopmcp.conformance import runner

  registry.register("mini", mini_adapter.create)
  print(runner.report(runner.check_config(cfg, Path("."))))   # cfg: StoreConfig
  ```

An unknown kind fails with the list of kinds known at that moment.

## The yml adapter

The feed is the source of truth, a SQLite index is its cache.

- `start()` builds the index once. A background task rebuilds it every `adapter.refresh_minutes`. A failed refresh, whatever it raises, keeps the previous index and shows up as `last_error` in `/healthz`; the next refresh runs on schedule. Only a first start with no usable index is fatal (`upstream_unavailable`); an index left by another version of the framework does not count as usable.
- `/healthz` answers `503` with `"stale": true` when the index is older than `stale_minutes` (default three refresh intervals), three refreshes in a row failed, or the refresh task stopped. One feed read is capped at 30 minutes in total, so a host trickling bytes fails the refresh instead of holding it.
- The feed is read as a stream, so memory stays flat on feeds of hundreds of megabytes. `max_feed_mb` caps the size; a DTD with internal entities is refused.
- An invalid or repeated offer is skipped and counted in `skipped`; it does not stop the feed.
- The index file is `$SHOPMCP_DATA_DIR/<store id>.sqlite`. If the directory is not writable, the index goes to a temporary directory and `/healthz` carries a `warning`.
- Capabilities: `search`, `details`, `barcode`, `analogs`, `categories`, `promotions`. Not provided: `pickup_points`, `share_link`, `orders`.
- Search normalizes the query and the index with Russian and English snowball stemmers, so inflected forms match (the Russian `молока` finds `молоко`).
- Keys: `feed`, `format`, `refresh_minutes`, `max_feed_mb`, `stale_minutes`, `cart_ref` ([configuration.md](configuration.md#adapter)). Google Merchant feeds: [below](#google-merchant-feed).

Feed format: a standard `yml_catalog` with `shop/categories` and `shop/offers`; the demo feed [`stores/demo/feed.xml`](../../stores/demo/feed.xml) shows the supported elements (`url`, `price`, `oldprice`, `currencyId`, `categoryId`, `picture`, `name` or `typePrefix`+`vendor`+`model`, `vendor`, `barcode`, `description`, `param`, the `available` attribute).

## Google Merchant feed

The `yml` adapter (kind `yml` or `feed`) also reads a Google Merchant feed. `adapter.format = "auto"` (the default) picks the reader by the root element: `rss` or an Atom `feed` is Merchant, `yml_catalog` is YML. The index, search, refresh and `/healthz` are the same; only the reader differs.

What is read (`src/shopmcp/adapters/yml/gmc.py`):

- **Items**: RSS `item` or Atom `entry`; the `g:` namespace (`http://base.google.com/ns/1.0`) must be declared before the first item. An Atom feed with no `g:` (a Shopify `s:` feed) is not a Merchant feed and is refused as a whole.
- **Required per item**: `g:id`, a link (`g:link` or the plain `link`), a title and `g:price`. An item without any of them is skipped and counted in `skipped`.
- **Price** is `1299.00 RUB` (the currency may come first). Digit groups and a decimal comma are read (`1 299,00`, `1.299,00`, `1,299.00`); a lone comma before exactly three digits (`1,299`) is ambiguous, so the item is skipped rather than guessed. `RUR` is read as `RUB`.
- **Sale**: `g:sale_price` below `g:price` in the same currency makes the price the current one and `g:price` the old one, with `discount_percent`. If `g:sale_price_effective_date` (`start/end`, ISO 8601, either side may be empty) is set, the sale counts only inside it; a date that cannot be read means no sale. The period is checked at the index build time, not at request time: a sale starts and ends with a delay of up to one refresh.
- **Variants**: `g:item_group_id` becomes the group of the product; with the default `cart_ref` the item is a variant of that group ([configuration.md](configuration.md#adaptercart_ref)).
- **Availability**: `in_stock` is available; `out_of_stock`, `preorder` and `backorder` are not. Anything else leaves it unknown.
- **Categories**: Merchant has no category tree, so `g:product_type` (`A > B > C`) becomes categories with synthetic ids, one parent per path prefix.
- **Other fields**: `g:brand` is the vendor, `g:gtin` the barcode (8-14 digits), `g:image_link` and `g:additional_image_link` the pictures, `g:color`, `g:size`, `g:material`, `g:pattern`, `g:gender`, `g:age_group` the params.

Limits: a category path is cut at 10 levels, `g:product_type` at 750 characters, an item with over 10 000 nodes is skipped, a feed with an internal DTD is refused, the size is capped by `max_feed_mb`. The feed is read as a stream like YML.

The demo feed [`stores/demo/feed-gmc.xml`](../../stores/demo/feed-gmc.xml) shows the supported elements.

## The woocommerce adapter

`kind = "woocommerce"`, `site = "https://shop.example"`. The catalog is read from the public Store API (`/wp-json/wc/store/v1`): GET only, no key, no cookie. Capabilities: `search`, `details`, `analogs`, `categories`, `promotions`. Not provided: `barcode` (the Store API does not publish GTINs), `pickup_points`, `share_link`, `orders`.

- **A snapshot, not live requests.** The adapter reads the whole catalog page by page into the same SQLite index as a feed and rebuilds it every `adapter.refresh_minutes`, so search, filters and sorting behave as on a feed. The first build in `start()`, the previous index kept when a refresh fails, `/healthz` `503` when stale: as for `yml`.
- **Polite crawl.** 100 products per page, one request per second, a response page is capped at 4 MB. A request that fails with `429`, `502`, `503` or `504` is retried twice with a growing pause (honouring `Retry-After` up to 60 s; a longer wait fails the read). The limits of one read: `max_pages` per listing, `max_minutes` for the whole read and `max_feed_mb` for all bytes. A shop that does not fit is refused with the key to raise.
- **Prices are minor units**: the Store API sends `"1899"` with `currency_minor_unit: 2`, the adapter reads it as 18.99; `"115"` with `0` is 115. A regular price above the sale price gives the old price and the discount.
- **Variations.** A variable product is not one purchasable unit, its variations are. Each variation becomes a product named after the parent, in the parent's categories, with the parent as its group; its own link and price are used. `{id}` in a cart template is the variation id, the one `?add-to-cart=` and `/checkout-link/` take.
- **Skipped**: `grouped` products (a set of links), `external` products (sold elsewhere), a `variable` product left without variations, and a variation that leaves an attribute at "Any" (its cart link by id alone likely adds nothing; not checked live). Each skip is counted in `skipped`.
- **An incomplete crawl never replaces the index.** If a listing ends before its own `X-WP-TotalPages` / `X-WP-Total` say (a few items, or 1%, are allowed for deletions during the read), if the product listing is empty, or if a limit is hit, the read fails: the catalog is a snapshot, and a short answer from a cache, a firewall or a visibility plugin would otherwise replace all of it. The previous index is served and the reason shows as `last_error` in `/healthz`. Only a first start with no index is fatal.
- **Local check.** The first read of a big shop takes minutes: `shopmcp check --online --start-timeout …` ([conformance.md](conformance.md#modes)).

Keys: `site`, `refresh_minutes`, `stale_minutes`, `max_pages`, `max_minutes`, `max_feed_mb` ([configuration.md](configuration.md#adapter)).

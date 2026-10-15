# Store engines

**English** · [Русский](../ru/engines.md)

For whom: the owner of a store on one of the supported engines who wants a ready `store.toml` without writing an adapter. `shopmcp scaffold` recognizes the engine (or takes it from `--engine`) and writes the matching `[adapter]` and `[cart]` from a preset (`src/shopmcp/engines.py`).

Contents: [Supported engines](#supported-engines) · [Scaffold](#scaffold) · [Engine detection](#engine-detection) · [What you fill in by hand](#what-you-fill-in-by-hand) · [form_post: beta](#form_post-beta) · [Notes per engine](#notes-per-engine)

## Supported engines

Ten presets. Every preset is the default that needs nothing installed on the store side; a module or a snippet on the store gives a better cart (the `receiver` strategy, see [cart-receiver.md](cart-receiver.md)) and is written into the config as an `Upgrade:` comment.

| `--engine` | Catalog (adapter / feed) | Cart (strategy) | Status |
|---|---|---|---|
| `shopify` | no adapter of its own: a Google Merchant feed from an app (`kind = "feed"`) | `url_template`, `https://<host>/cart/{items}?storefront=true`, lines `{id}:{qty}` | Ready; the feed id form `shopify_<CC>_<product>_<variant>` is not verified live |
| `woocommerce` | `woocommerce` adapter (keyless Store API), no feed | `link_list` with an add-to-cart link per line; on WooCommerce 10+ the commented `url_template` `/checkout-link/?products={items}` | Ready |
| `ecwid` | Google Merchant feed from the Ecwid settings | `json_fragment`, cart in the URL fragment; `base64` variant for a store on a Wix site | Ready; the Ecwid feed format is not verified |
| `bitrix` | YML feed | `link_list` with `{url}?action=ADD2BASKET&id={id}&quantity={qty}` | Ready |
| `prestashop` | Google Merchant feed (a feed module) | `link_list` with `?add=1&id_product=…&id_product_attribute=…&qty=…` | Ready |
| `opencart` | YML feed (a module's) | `link_list`, product page links | Ready |
| `magento` | Google Merchant feed (an extension's) | `link_list`, product page links | Ready |
| `tilda` | YML feed (`/tstore/yml/<hash>.yml`) | `link_list`, product page links | Ready |
| `insales` | YML feed | `link_list`, product page links; `form_post` (mode `single`) as a commented alternative | Ready; `form_post` **BETA**, see [below](#form_post-beta) |
| `shop_script` | YML feed | `link_list`, product page links; `form_post` (mode `chain`) as a commented alternative | Ready; `form_post` **BETA**, see [below](#form_post-beta) |

OpenCart and Magento are not on `form_post`: their carts take no cross-site POST (OpenCart answers raw JSON, Magento needs a `form_key`), so the preset gives product page links, and the module upgrade is `receiver`.

"Ready" means the code and tests exist; the live check of cart links on real stores is a separate stage and is not claimed here.

## Scaffold

```bash
uv run shopmcp scaffold my-shop --site https://shop.example
uv run shopmcp scaffold my-shop --site https://shop.example --engine woocommerce
uv run shopmcp scaffold my-shop --site https://shop.example --engine prestashop --feed https://shop.example/feed.xml
```

| Option | Purpose |
|---|---|
| `--site URL` | Store website, required. Becomes `site`; the cart links are built on its host. For an engine installed in a subdirectory (`https://shop.example/shop`) the path is kept in the links |
| `--engine` | One of the ten names above. Skips detection |
| `--feed` | Feed path or `https://` URL for the `[adapter]`. Ignored (with a note) by engines whose adapter reads no feed (`woocommerce`) |
| `--prefix`, `--locale` | As in [quickstart.md](quickstart.md#add-your-store) |

The command writes `stores/<store_id>/store.toml` in the current directory and refuses to overwrite an existing file. The comments in the file are in both languages; lines marked `EDIT` are yours. Without `--engine` and when the engine is not determined, the file is the generic one: a YML feed and product page links.

## Engine detection

Without `--engine`, scaffold looks at the site (`src/shopmcp/detect.py`):

- **GET only**, no cookies, no body sent. At most 8 requests, 10 s each, a response body is cut at 512 KB, at most 3 redirects. The user agent is `shopmcp-scaffold/<version>`.
- It reads the home page and then a few cheap confirmation probes (for example the WooCommerce Store API and the Magento GraphQL endpoint).
- A sign is either decisive (only this engine answers so) or a hint (seen on the engine, but a theme or a CDN could show it too). One decisive sign gives the engine. A hint alone gives the engine with a warning.
- **Two engines with decisive signs, or several hints, give candidates and no choice**: the summary lists them, choose one with `--engine`.
- **"Not determined"** is a normal answer, not an error. It is printed with a reason when the site is unreachable, when the home page is closed by an antibot or an access rule (HTTP 401, 403, 429, 503, or a challenge page), or when no signs are found. It means "unknown", not "this is not a supported engine": set `--engine` by hand. Ecwid is the typical blind spot, its widget is often loaded by a script that a plain GET does not run.
- Warnings are printed for known traps, e.g. Bitrix behind a Next.js storefront (add-to-cart links do not work, `line_template` is dropped), Tilda without a store block, PrestaShop without `var prestashop` (1.6 or a custom theme).

The summary after the run lists the engine, the signs, and what to fill in:

```text
engine: Shopify (--engine)
cart: url_template; Shopify publishes its own store MCP: no catalog adapter, only the cart link here. …
fill in by hand (lines marked EDIT): brand, instructions, server.public_url,
  conformance.sample_query, adapter.feed, adapter.cart_ref
created stores/demo2/store.toml; edit the lines marked EDIT
```

## What you fill in by hand

Always: `brand`, `instructions`, `server.public_url`, `conformance.sample_query` (the scaffold writes `CHANGE-ME`, so the check stays red until you replace it). Per engine, the lines marked `EDIT`:

| Engine | Also fill in |
|---|---|
| `shopify` | `adapter.feed` (Google Merchant feed from an app); check `[adapter.cart_ref]` `pattern` against the `g:id` of your feed |
| `woocommerce` | nothing more; confirm the WooCommerce version if you want the one-link cart (uncomment the alternative `[cart]` block) |
| `ecwid` | `adapter.feed`; product options for the cart in `[adapter.cart_ref]` `options` |
| `insales` | `adapter.feed`; for `form_post` later, `SHOPMCP_CART_SECRET` in the environment |
| `shop_script` | `adapter.feed`, `[adapter.cart_ref]` (how a feed id splits into product and SKU); for `form_post` later, `SHOPMCP_CART_SECRET` |
| `bitrix` | `adapter.feed`; on a headless storefront remove `line_template` (scaffold does it when it detects one) |
| `prestashop` | `adapter.feed`, `[adapter.cart_ref]` for ids like `12-34`, `cart.line_template`: the cart path depends on the storefront language (`cart`, `panier`, …) |
| `opencart`, `magento`, `tilda` | `adapter.feed` |

`cart.secret` must be a `${ENV}` reference, never a literal, and at least 32 bytes long. Field reference: [configuration.md](configuration.md#cart).

## form_post: beta

`form_post` is the cart for InSales and Shop-Script, whose carts take POST only. It is **BETA**, so scaffold writes product page links (`link_list`) as the working cart and the `form_post` block commented out:

- It is **not verified in live browsers** yet (live check V1 of the plan). Browsers treat a cross-site POST differently depending on cookie settings.
- **From another site the engine can replace the customer's cart** instead of adding to it (observed on InSales). The risk is lower on a subdomain of the store (same site), so **host the shopmcp server on a subdomain of the store**, e.g. `mcp.shop.example` for `shop.example`.
- Shop-Script posts the lines one by one and relies on the store returning the browser to the trampoline page; this is not verified either.
- **The trampoline page itself is not in the code yet.** The `form_post` keys are validated, but a store with `strategy = "form_post"` does not start: `error: cart.strategy form_post: shopmcp.cart.form_post.create is unavailable`. Keep `link_list` or use `receiver` until it lands and passes check V1.

Scaffold puts the same warning as a comment next to the `form_post` block.

## Notes per engine

- **Shopify** publishes its own store MCP, so there is no catalog adapter. The cart tool still needs a catalog to check ids, so a Google Merchant feed goes into `[adapter]`. An Atom feed with the `s:` namespace and no `g:` is refused as a whole. The id in the cart link is the `variant_id`, which `cart_ref.pattern` takes from the `g:id`.
- **WooCommerce**: the catalog is a snapshot of the public Store API, see [adapters.md](adapters.md#the-woocommerce-adapter). The add-to-cart link works on any version.
- **Ecwid**: the cart is read from the URL fragment by the storefront script. The Ecwid feed format and the `Wix` variant are documentation-based and not verified live.
- **Bitrix**: the add-to-cart link puts one line; the `id` is the trade offer.
- **Tilda**: the cart lives in the browser. A snippet in a T123 block gives a one-link cart (`url_template` with `#mcpcart=…`, unsigned).
- **OpenCart**: a module's YML may be in `windows-1251`.

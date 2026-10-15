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
| `insales` | `adapter.feed`; for `form_post`, `SHOPMCP_CART_SECRET` in the environment |
| `shop_script` | `adapter.feed`, `[adapter.cart_ref]` (how a feed id splits into product and SKU); for `form_post`, `SHOPMCP_CART_SECRET` |
| `bitrix` | `adapter.feed`; on a headless storefront remove `line_template` (scaffold does it when it detects one) |
| `prestashop` | `adapter.feed`, `[adapter.cart_ref]` for ids like `12-34`, `cart.line_template`: the cart path depends on the storefront language (`cart`, `panier`, …) |
| `opencart`, `magento`, `tilda` | `adapter.feed` |

`cart.secret` must be a `${ENV}` reference, never a literal, and at least 32 bytes long. Field reference: [configuration.md](configuration.md#cart).

## form_post: beta

`form_post` is the cart for InSales and Shop-Script, whose carts take POST only. It is **BETA**, so scaffold writes product page links (`link_list`) as the working cart and the `form_post` block commented out. To switch, uncomment the block, set `SHOPMCP_CART_SECRET` (at least 32 bytes) and run `shopmcp check`.

### How it works

The cart link is `<origin of server.public_url>/cart/h/<signed token>` ([token format](cart-receiver.md#format)). The server keeps no carts: the lines are in the token. The page behind the link sends the lines to the store by POST from the customer's browser, so the store sees the customer's own cookies:

- `single`: one form with every line, submitted on load (InSales `/cart_items`).
- `chain`: one form per line. The store answers each POST with a redirect to the page the browser came from, and the page submits the next line; after the last one it goes to `finish_url` (Shop-Script `/cart/add/`). This works because the page asks for the full `Referer` (`Referrer-Policy: unsafe-url`) and the store returns the browser to it. No fallback path is needed. The page's CSP allows the store and `'self'` in `form-action`: without `'self'` Chromium blocks the redirect back to the page and the chain stops after one line.
- Without JavaScript the same forms are buttons ("Add line 2 of 3").
- `action` and `finish_url` come from `store.toml` only, never from the token; an expired or damaged link gives a `400` page with a link to the site.

Opening the same link again (another tab, a back button) does not add the lines twice: the page finds its mark in the browser storage, says that the products were already sent, and offers a link to the cart and an **Add them again** button. With storage blocked (private mode) the page sends the lines on every open.

### Where to host the server

What happens depends on whether the browser sends the store's cart cookie with the POST. That is decided by the cookie's `SameSite` attribute and by whether the page is on the store's site. A site is the registrable domain by the Public Suffix List, so `mcp.shop.example`, `www.shop.example` and `shop.example` are one site, while `a.github.io` and `b.github.io` are two.

**Host the shopmcp server on a subdomain of the store** (`server.public_url = "https://mcp.shop.example/mcp"` for `shop.example`). Then the cookie always goes with the POST.

Live check V1 (15.10.2026, a local Shop-Script stand, Chromium; a cart of three lines was added to a customer cart that already held another line):

| Server | Store cart cookie | Result |
|---|---|---|
| a subdomain of the store (same site) | any | The cart is built, earlier lines are kept |
| another site | `SameSite=None` (Webasyst on HTTPS) | The same: the cart is built, earlier lines are kept |
| another site | `Lax` (a store on http; InSales by reconnaissance, not checked live) | The cart is **replaced**: every POST starts a new cart, only the last line stays. The page still reaches `finish_url`, so the customer sees no error |

The age of the cart or of the cookie made no difference. `single` mode was not run live: the rule for InSales (another site replaces the cart) rests on reconnaissance.

Not checked: a real HTTPS connection in a browser (the `SameSite=None; Secure` cookie was checked by substituting the attribute on a local stand), Safari, InSales on a live store. This is why `form_post` stays **BETA** and the InSales and Shop-Script presets default to `link_list`.

`shopmcp check` applies this table as C11 (see [conformance.md](conformance.md#how-c11-judges-form_post)): it fails where the cart would be replaced and warns where it depends on the store's cookie.

Scaffold puts the same warning as a comment next to the `form_post` block; for a store on http it adds that `form_post` works only from a subdomain of the store.

## Notes per engine

- **Shopify** publishes its own store MCP, so there is no catalog adapter. The cart tool still needs a catalog to check ids, so a Google Merchant feed goes into `[adapter]`. An Atom feed with the `s:` namespace and no `g:` is refused as a whole. The id in the cart link is the `variant_id`, which `cart_ref.pattern` takes from the `g:id`.
- **WooCommerce**: the catalog is a snapshot of the public Store API, see [adapters.md](adapters.md#the-woocommerce-adapter). The add-to-cart link works on any version.
- **Ecwid**: the cart is read from the URL fragment by the storefront script. The Ecwid feed format and the `Wix` variant are documentation-based and not verified live.
- **Bitrix**: the add-to-cart link puts one line; the `id` is the trade offer.
- **Tilda**: the cart lives in the browser. A snippet in a T123 block gives a one-link cart (`url_template` with `#mcpcart=…`, unsigned).
- **OpenCart**: a module's YML may be in `windows-1251`.

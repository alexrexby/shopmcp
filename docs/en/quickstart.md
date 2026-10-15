# Quickstart

**English** · [Русский](../ru/quickstart.md)

Run the bundled demo store locally and in Docker, then add your own store. The demo is a fictional store with a 40-offer YML feed (`stores/demo/feed.xml`).

Contents: [Requirements](#requirements) · [Run locally](#run-locally) · [Run in Docker](#run-in-docker) · [Add your store](#add-your-store) · [Where the index lives](#where-the-index-lives)

## Requirements

- [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer (for the local run)
- Docker with the Compose plugin (for the container run)

## Run locally

```bash
uv sync
```

Check that the commands exist:

```console
$ uv run shopmcp --help
usage: shopmcp [-h] {serve,index,check,scaffold} ...

MCP server for an online store

positional arguments:
  {serve,index,check,scaffold}
    serve               run the MCP server over HTTP (index in
                        $SHOPMCP_DATA_DIR, default ./data)
    index               build the search index once into $SHOPMCP_DATA_DIR and
                        exit
    check               run the conformance checks (store.toml: served
                        locally; --url: live)
    scaffold            create stores/<store_id>/store.toml; the engine is
                        detected from the site (GET only)

options:
  -h, --help            show this help message and exit
```

Build the index once (optional: `serve` does it on start):

```bash
uv run shopmcp index stores/demo/store.toml
```

```json
{"feed_offers": 40, "skipped": 1, "last_error": null, "warning": null, "products": 39, "categories": 6, "feed_date": "2026-10-13T09:00:00+03:00", "index_age_seconds": 0}
```

The demo feed has 40 offers; one is invalid on purpose and is counted in `skipped`.

Start the server:

```bash
uv run shopmcp serve stores/demo/store.toml
```

The server listens on `0.0.0.0:8000`, the MCP endpoint is `http://localhost:8000/mcp`, the health probe is `http://localhost:8000/healthz`:

```bash
curl -s localhost:8000/healthz
```

```json
{"status":"ok","store":"demo","last_error":null,"failed_refreshes":0,"warning":null,"products":39,"categories":6,"feed_offers":40,"skipped":1,"feed_date":"2026-10-13T09:00:00+03:00","index_age_seconds":3,"stale":false}
```

The server is stateless and answers in JSON; only `POST` reaches the MCP path (a `GET` gets `405`). List the tools:

```bash
curl -s -X POST localhost:8000/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The demo store returns eight tools: `demo_products_search`, `demo_product_details`, `demo_product_by_barcode`, `demo_product_analogs`, `demo_categories`, `demo_promotions`, `demo_cart_link_create`, `demo_store_info`.

Call one:

```bash
curl -s -X POST localhost:8000/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_products_search","arguments":{"q":"молоко","limit":2}}}'
```

The reply starts like this (shortened; the text content is the same compact JSON as `structuredContent`):

```text
{"jsonrpc":"2.0","id":2,"result":{"content":[{"text":"{\"meta\":{\"q\":\"молоко\",\"page\":1,\"limit\":2,\"total\":7,\"pages\":4,\"has_more\":true,\"truncated\":false},\"items\":[{\"id\":\"3\",\"name\":\"Молоко топлёное 4%, 0,5 л\",\"url\":\"https://demo.example/p/3\",\"price\":{\"current\":{\"amount\":95.0,\"currency\":\"RUB\"}},\"available\":true},…
```

In a real MCP client, register `http://localhost:8000/mcp` as a streamable-HTTP server.

Stop the server with `Ctrl+C`.

### Run the conformance checks

```bash
uv run shopmcp check stores/demo/store.toml
```

```text
feed /Users/.../stores/demo/feed.xml: 39 products, 6 categories, 1 skipped
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
C14  ok   rate limit                               429 at request 33 (earlier checks spent part of the bucket)
14 ok, 0 warnings, 0 failed, 1 skipped
```

`check` starts the store on a free local port with a temporary index, so a running `serve` does not interfere. The exit code is `0` if nothing failed and `1` otherwise. What each check verifies: [conformance.md](conformance.md).

## Run in Docker

The repository ships a `Dockerfile` (one image, one store) and `compose.yaml` with a `demo` service.

```bash
docker compose up -d --build
docker compose ps
```

After the health check settles, the status is `Up … (healthy)`, and the port is published on the loopback interface only:

```text
NAME                  IMAGE            COMMAND                  SERVICE   CREATED          STATUS                   PORTS
mcp-for-sale-demo-1   shopmcp:latest   "shopmcp serve /app/…"   demo      10 seconds ago   Up 9 seconds (healthy)   127.0.0.1:8000->8000/tcp
```

```bash
curl -s localhost:8000/healthz
uv run shopmcp check --url http://localhost:8000/mcp stores/demo/store.toml
```

Stop it and drop the index volume:

```bash
docker compose down -v
```

How the container is wired (all of it is in `Dockerfile` and `compose.yaml`):

| Item | Value |
|---|---|
| Base | `python:3.12-slim`, dependencies installed by uv with `--frozen --no-dev` |
| User | `uid 10001`, non-root |
| Store config | `./stores/demo` is mounted read-only at `/app/store`; the command is `shopmcp serve /app/store/store.toml` |
| Index | named volume `demo-data` at `/data`; `SHOPMCP_DATA_DIR=/data` |
| Port | container `8000`, published as `127.0.0.1:8000:8000`; put a reverse proxy in front for the public |
| Health check | requests `/healthz` on the port read from the same `store.toml` |

Notes:

- **Use a named volume for `/data`.** A fresh named volume inherits the image's owner (`10001`). A host bind mount on Linux stays `root:root` and the process cannot write; the `yml` adapter then falls back to a temporary directory and reports it as `warning` in `/healthz`.
- **Behind a reverse proxy** add the public host to `server.allowed_hosts` and set `server.public_url`; see [configuration.md](configuration.md#server).
- **One service per store.** Copy the service block in `compose.yaml` with its own config directory, volume and host port.

## Add your store

1. Put the YML feed next to the future config, in `stores/<store_id>/`. A relative `adapter.feed` is resolved against the directory of `store.toml`, not against the current directory. An `https://` URL works too.
2. Generate the config from the template (run it in the repository root; it writes `stores/<store_id>/store.toml`):

   ```bash
   uv run shopmcp scaffold my-shop --feed feed.xml --site https://my-shop.example --locale en
   ```

   ```text
   created stores/my-shop/store.toml; edit the lines marked EDIT
   ```

   For a store on a known engine give the site and let the scaffold pick the engine and the matching `[adapter]` and `[cart]`; `--feed` is optional then (the file marks the line `EDIT`):

   ```bash
   uv run shopmcp scaffold my-shop --site https://shop.example [--engine shopify] [--feed feed.xml]
   ```

   Without `--engine` the scaffold detects the engine with a few GET requests and prints the signs it found; "not determined" means set `--engine` by hand. Engines, presets and what to fill in: [engines.md](engines.md).

   `--prefix` sets the tool name prefix; by default it is the store id without dashes (`myshop` here). A second run for the same id refuses to overwrite:

   ```text
   error: stores/my-shop/store.toml already exists, not overwritten
   ```

3. Edit the lines marked `EDIT`: `brand`, `instructions`, `server.public_url`, and `conformance.sample_query`. The scaffold writes `CHANGE-ME` as the sample query on purpose, so the check stays red until you replace it with a word that finds at least one product:

   ```text
   C6   fail search and paging                        search('CHANGE-ME') found nothing
   ...
   12 ok, 0 warnings, 1 failed, 1 skipped
   ```

4. Run `uv run shopmcp check stores/my-shop/store.toml`, then `serve`.

If the feed path is wrong, the server does not start (`first index build failed: cannot open feed: …`) and `check` reports `the local server did not start`. Field reference: [configuration.md](configuration.md).

## Where the index lives

The `yml` adapter keeps a SQLite index in `$SHOPMCP_DATA_DIR/<store id>.sqlite` (default: `./data`). `serve` and `index` must use the same `SHOPMCP_DATA_DIR`. The index is a cache: a background task rebuilds it every `adapter.refresh_minutes`; a failed refresh keeps serving the previous index and shows `last_error` in `/healthz`; an index older than `adapter.stale_minutes` turns `/healthz` into `503`.

# Быстрый старт

[English](../en/quickstart.md) · **Русский**

Запускаем демо-магазин из репозитория локально и в Docker, затем добавляем свой магазин. Демо — вымышленный магазин с YML-фидом на 40 предложений (`stores/demo/feed.xml`).

Содержание: [Требования](#требования) · [Локальный запуск](#локальный-запуск) · [Запуск в Docker](#запуск-в-docker) · [Свой магазин](#свой-магазин) · [Где лежит индекс](#где-лежит-индекс)

## Требования

- [uv](https://docs.astral.sh/uv/) и Python 3.12 или новее (для локального запуска)
- Docker с плагином Compose (для запуска в контейнере)

## Локальный запуск

```bash
uv sync
```

Убедитесь, что команды на месте (справка выводится на английском):

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
    scaffold            create stores/<store_id>/store.toml

options:
  -h, --help            show this help message and exit
```

Постройте индекс заранее (необязательно: `serve` сделает это при старте):

```bash
uv run shopmcp index stores/demo/store.toml
```

```json
{"feed_offers": 40, "skipped": 1, "last_error": null, "warning": null, "products": 39, "categories": 6, "feed_date": "2026-10-13T09:00:00+03:00", "index_age_seconds": 0}
```

В демо-фиде 40 предложений; одно намеренно некорректное, оно учтено в `skipped`.

Запустите сервер:

```bash
uv run shopmcp serve stores/demo/store.toml
```

Сервер слушает `0.0.0.0:8000`, MCP-эндпоинт — `http://localhost:8000/mcp`, проверка живости — `http://localhost:8000/healthz`:

```bash
curl -s localhost:8000/healthz
```

```json
{"status":"ok","store":"demo","last_error":null,"failed_refreshes":0,"warning":null,"products":39,"categories":6,"feed_offers":40,"skipped":1,"feed_date":"2026-10-13T09:00:00+03:00","index_age_seconds":3,"stale":false}
```

Сервер без состояния и отвечает JSON-ом; до MCP-пути доходит только `POST` (на `GET` приходит `405`). Список инструментов:

```bash
curl -s -X POST localhost:8000/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Демо-магазин отдаёт восемь инструментов: `demo_products_search`, `demo_product_details`, `demo_product_by_barcode`, `demo_product_analogs`, `demo_categories`, `demo_promotions`, `demo_cart_link_create`, `demo_store_info`.

Вызов одного из них:

```bash
curl -s -X POST localhost:8000/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_products_search","arguments":{"q":"молоко","limit":2}}}'
```

Начало ответа (сокращено; текстовое содержимое — тот же компактный JSON, что и `structuredContent`):

```text
{"jsonrpc":"2.0","id":2,"result":{"content":[{"text":"{\"meta\":{\"q\":\"молоко\",\"page\":1,\"limit\":2,\"total\":7,\"pages\":4,\"has_more\":true,\"truncated\":false},\"items\":[{\"id\":\"3\",\"name\":\"Молоко топлёное 4%, 0,5 л\",\"url\":\"https://demo.example/p/3\",\"price\":{\"current\":{\"amount\":95.0,\"currency\":\"RUB\"}},\"available\":true},…
```

В настоящем MCP-клиенте добавьте `http://localhost:8000/mcp` как сервер streamable HTTP.

Остановка — `Ctrl+C`.

### Проверки соответствия

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
14 ok, 0 failed, 1 skipped
```

`check` сам поднимает магазин на свободном локальном порту с временным индексом, поэтому запущенный `serve` не мешает. Код возврата `0`, если ничего не упало, иначе `1`. Что проверяет каждая проверка: [conformance.md](conformance.md).

## Запуск в Docker

В репозитории есть `Dockerfile` (один образ — один магазин) и `compose.yaml` с сервисом `demo`.

```bash
docker compose up -d --build
docker compose ps
```

Когда health check отработает, статус будет `Up … (healthy)`; порт опубликован только на loopback-интерфейсе:

```text
NAME                  IMAGE            COMMAND                  SERVICE   CREATED          STATUS                   PORTS
mcp-for-sale-demo-1   shopmcp:latest   "shopmcp serve /app/…"   demo      10 seconds ago   Up 9 seconds (healthy)   127.0.0.1:8000->8000/tcp
```

```bash
curl -s localhost:8000/healthz
uv run shopmcp check --url http://localhost:8000/mcp stores/demo/store.toml
```

Остановить и удалить том с индексом:

```bash
docker compose down -v
```

Как устроен контейнер (всё это есть в `Dockerfile` и `compose.yaml`):

| Что | Значение |
|---|---|
| Основа | `python:3.12-slim`, зависимости ставит uv с `--frozen --no-dev` |
| Пользователь | `uid 10001`, не root |
| Конфиг магазина | `./stores/demo` монтируется только для чтения в `/app/store`; команда — `shopmcp serve /app/store/store.toml` |
| Индекс | именованный том `demo-data` в `/data`; `SHOPMCP_DATA_DIR=/data` |
| Порт | в контейнере `8000`, снаружи `127.0.0.1:8000:8000`; для публикации нужен реверс-прокси |
| Health check | запрашивает `/healthz` на порту из того же `store.toml` |

Заметки:

- **Для `/data` берите именованный том.** Свежий именованный том наследует владельца из образа (`10001`). Каталог хоста, смонтированный как bind mount на Linux, остаётся `root:root`, и процесс не может в него писать; адаптер `yml` тогда уходит во временный каталог и сообщает об этом в поле `warning` ответа `/healthz`.
- **За реверс-прокси** добавьте публичный хост в `server.allowed_hosts` и задайте `server.public_url`: [configuration.md](configuration.md#server).
- **Один сервис на магазин.** Скопируйте блок сервиса в `compose.yaml` со своим каталогом конфига, томом и портом хоста.

## Свой магазин

1. Положите YML-фид рядом с будущим конфигом, в `stores/<store_id>/`. Относительный `adapter.feed` отсчитывается от каталога `store.toml`, а не от текущего каталога. Подойдёт и `https://`-ссылка.
2. Создайте конфиг из шаблона (запускайте в корне репозитория; файл появится в `stores/<store_id>/store.toml`):

   ```bash
   uv run shopmcp scaffold my-shop --feed feed.xml --site https://my-shop.example --locale en
   ```

   ```text
   created stores/my-shop/store.toml; edit the lines marked EDIT
   ```

   `--prefix` задаёт префикс имён инструментов; по умолчанию это id магазина без дефисов (здесь `myshop`). Повторный запуск с тем же id файл не перезапишет:

   ```text
   error: stores/my-shop/store.toml already exists, not overwritten
   ```

3. Отредактируйте строки с пометкой `EDIT`: `brand`, `instructions`, `server.public_url` и `conformance.sample_query`. Scaffold намеренно пишет в `sample_query` значение `CHANGE-ME`, поэтому проверка остаётся красной, пока вы не замените его словом, по которому находится хотя бы один товар:

   ```text
   C6   fail search and paging                        search('CHANGE-ME') found nothing
   ...
   12 ok, 1 failed, 1 skipped
   ```

4. Выполните `uv run shopmcp check stores/my-shop/store.toml`, затем `serve`.

Если путь к фиду неверен, сервер не стартует (`first index build failed: cannot open feed: …`), а `check` сообщает `the local server did not start`. Справочник по полям: [configuration.md](configuration.md).

## Где лежит индекс

Адаптер `yml` хранит SQLite-индекс в `$SHOPMCP_DATA_DIR/<id магазина>.sqlite` (по умолчанию `./data`). У `serve` и `index` значение `SHOPMCP_DATA_DIR` должно совпадать. Индекс — это кэш: фоновая задача перестраивает его каждые `adapter.refresh_minutes`; неудачное обновление не мешает работе на прежнем индексе и отражается полем `last_error` в `/healthz`; индекс старше `adapter.stale_minutes` переводит `/healthz` в `503`.

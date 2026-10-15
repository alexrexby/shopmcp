# Проверки соответствия

[English](../en/conformance.md) · **Русский**

`shopmcp check` выполняет 15 проверок (C1-C15) против MCP-эндпоинта и печатает по строке на проверку. Они кодируют дефекты, которые фреймворк обязуется не выпускать, см. [design-notes.ru.md](../design-notes.ru.md). Один и тот же набор работает и по локальной сборке магазина, и по живому URL.

Содержание: [Режимы](#режимы) · [Как читать отчёт](#как-читать-отчёт) · [Проверки](#проверки) · [Запуск по живому URL](#запуск-по-живому-url) · [Типичные падения](#типичные-падения)

## Режимы

| Команда | Что происходит |
|---|---|
| `shopmcp check <store.toml>` | Собирает приложение самого магазина, поднимает его на свободном порту `127.0.0.1` с временным индексом и проверяет. Ожидания (префикс, brand, лимиты, набор инструментов, scope) берутся из конфига |
| `shopmcp check --url <URL>` | Проверяет запущенный эндпоинт без ожиданий: они выводятся из ответов сервера и из канонического набора инструментов фреймворка |
| `shopmcp check --url <URL> <store.toml>` | Проверяет живой эндпоинт и сверяет его с конфигом |

Локальный запуск добавляет `127.0.0.1:*` в `server.allowed_hosts` только для своих запросов, поэтому боевой конфиг, где указан лишь публичный хост, тоже работает. Если какое-то значение в `[adapter]` — это `https://`-URL (например, фид), локальный запуск без `--online` отказывается стартовать:

```text
error: the store reads adapter.feed over the network; rerun with --online
```

`--online` — разрешение читать магазин по сети: локальный запуск забирает https-источники из `[adapter]` (URL фида или `site` адаптера `woocommerce`), то есть читает живой магазин. Без него такой конфиг отклоняется, поэтому проверка не залезет в магазин случайно. С `--online` локальный сервер ждёт первый индекс до **1860 с** (31 минута): API каталога читается вежливо, по странице; без него — до **60 с**. `--start-timeout СЕКУНДЫ` задаёт ожидание явно (число больше 0) в обоих случаях; когда время вышло, запуск останавливается с `RuntimeError`: `the local server did not start in N s (… a big catalog takes longer: --start-timeout)`:

```bash
uv run shopmcp check --online stores/my-shop/store.toml
uv run shopmcp check --online --start-timeout 600 stores/my-shop/store.toml
```

Оба флага действуют только на локальный запуск; с `--url` ничего не стартует.

Коды возврата: `0` — ничего не упало (пропуски не в счёт), `1` — упала хотя бы одна проверка или сломан конфиг, `2` — неверный вызов (нет ни конфига, ни `--url`).

Проверки говорят с эндпоинтом сырым JSON-RPC по HTTP, а не через клиент MCP SDK: SDK проверял бы и скрывал то, что на самом деле уходит по сети.

## Как читать отчёт

```text
C1   ok   initialize, brand, instructions          Демо-маркет, protocol 2025-06-18
...
C6   fail search and paging                        search('CHANGE-ME') found nothing
...
C13  skip auth: 401, RFC 9728                      auth disabled
...
12 ok, 1 failed, 1 skipped
```

Колонки: id, статус (`ok`, `fail`, `skip`), название, подробности. Проверка, которая упала с исключением, считается проваленной, исключение попадает в подробности. Часть проверок выводит не больше пяти проблем.

## Проверки

| Id | Что проверяет |
|---|---|
| C1 | `initialize` работает; `serverInfo.name` равно `brand` (при наличии конфига); `instructions` не пусты; вернулась версия протокола |
| C2 | Каждое имя инструмента соответствует `^<prefix>_[a-z_]+$`, не длиннее 64 символов, имена уникальны. Без конфига префикс должен быть общим у всех инструментов |
| C3 | Набор инструментов равен тому, что следует из capabilities адаптера и `tools.disabled` (без конфига: все инструменты из канонического набора фреймворка); ни один показанный инструмент не отвечает `not_supported` |
| C4 | У каждого инструмента есть описание, `inputSchema` и `outputSchema` типа object и явный булев `annotations.readOnlyHint` |
| C5 | Параметр со значением по умолчанию не обязателен; каждое `maxItems`/`maximum` в схеме встречается числом в описании параметра или инструмента; `maxItems` корзины равен `limits.max_cart_lines` |
| C6 | `sample_query` что-то находит; `total`, `pages`, `has_more` согласованы; `limit` до `max_page_size` соблюдается. Пропуск, если инструмента поиска нет |
| C7 | `product_details` возвращает запрошенный id и перечисляет отсутствующий id в `missing`; все id отсутствуют и неизвестный штрихкод — результаты `isError` с кодом `not_found`; `sample_barcode` находится |
| C8 | Ни одной HTML-сущности или тега ни в одной строке ни одного `structuredContent` |
| C9 | `content[0].text` — компактный JSON, равный `structuredContent`; результаты-ошибки — компактный JSON без `structuredContent` |
| C10 | Каждый ответ укладывается в `max_response_bytes` (самые большие страницы запрашиваются нарочно); поиск возвращает только `meta`, `items`, `facets`, фасетов не больше 10 |
| C11 | Корзина из двух найденных товаров передаётся по настроенной стратегии (`url_template`, деградировавший до `link_list` с пояснением, допускается); URL — `https`, URL `url_template` ведёт на сайт магазина и содержит id; неизвестный id — результат `isError`. Пропуск, если инструмента корзины нет |
| C12 | `GET` по MCP-пути даёт `405`; `subscriptions/listen` не открывает поток; мусорные `POST` не приводят к 5xx |
| C13 | Авторизация. Вызов персонального инструмента без токена — `401` с `resource_metadata` и верным `scope` в `WWW-Authenticate`; `/.well-known/oauth-protected-resource<path>` отвечает `200` и называет публичный URL как `resource`; публичный инструмент токена не требует. Пропуск при `auth.enabled = false` или без персональных инструментов |
| C14 | Лимит запросов: запрос номер `rate_per_minute + 1` получает `429` с корректным `Retry-After`. Только локальный запуск (живой сервер был бы загружен), и нужен конфиг. Выполняется последней, после C15: она опустошает корзину |
| C15 | CORS. Preflight (`OPTIONS`) с чужого origin не получает `Access-Control-Allow-Origin`. Если задан `server.allowed_origins`, preflight с первого origin из списка (для записи с `:*` берётся порт `8443`) отвечает `2xx` ровно с этим origin, `POST` в `Access-Control-Allow-Methods` и `content-type`, `authorization`, `mcp-protocol-version` в `Access-Control-Allow-Headers`, а `POST` с него несёт тот же `Access-Control-Allow-Origin`. Без конфига проверяется только сторона чужого origin |

Границы набора:

- Персональные инструменты вызываются только без токена; авторизованный путь (валидный токен, scope принят) не проверяется. Как проверить его вручную, см. [auth.md](auth.md).
- С адаптером `yml` персональных инструментов нет, поэтому C13 на практике всегда пропускается.
- Проверки вызывают инструменты только для чтения; при `cart.strategy = "share_link"` C11 создаёт настоящую общую корзину в магазине.

## Запуск по живому URL

```bash
uv run shopmcp check --url https://shop.example/mcp stores/shop/store.toml
```

Отчёт для демо-магазина в Docker (`docker compose up -d --build`) с конфигом:

```text
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
C14  skip rate limit                               only against a server the runner started (would throttle a live one)
13 ok, 0 failed, 2 skipped
```

Без конфига тот же сервер даёт `C3 ... (canonical set, no config)`, `C10 ... budget unknown (no config)` `C13 skip ... no personal tools` и `C15 ... allowed_origins unknown`:

```bash
uv run shopmcp check --url http://localhost:8000/mcp
```

Практические замечания:

- **Передавайте конфиг, если он есть.** Без него проверки не могут сверить сервер с его обещаниями (brand, лимиты, набор инструментов, стратегия корзины, scope).
- **Запуск по живому URL расходует лимит запросов сервера.** Прогон делает десятки запросов (на демо-магазине локальный прогон упирается в лимит на 33-м запросе в C14, остальное потратили предыдущие проверки), а в корзине `rate_per_minute` запросов (по умолчанию 60). Если лимит выбрал другой клиент или предыдущий прогон, проверка падает с `429 rate limited; rerun in a minute`.
- **Сервер должен принимать заголовок `Host`.** Для публичного URL этот хост должен быть в `server.allowed_hosts` развёрнутого конфига.
- **Запускайте после каждого деплоя и правки конфига**: это ближайшее подобие приёмочного теста эндпоинта, с которым будет работать клиент.

## Типичные падения

| Отчёт | Причина |
|---|---|
| `C6 fail ... search('CHANGE-ME') found nothing` | В `conformance.sample_query` всё ещё заглушка из scaffold, либо в фиде нет такого товара |
| `C3 fail ... extra [...]` / `missing [...]` | Набор инструментов живого сервера отличается от конфига: выкатана другая версия конфига или изменился `tools.disabled` |
| `C3 fail ... listed but not_supported [...]` | Адаптер объявляет capability, метод которой бросает `NotSupported` |
| `C5 fail ... maximum=N not in its description` | Граница параметра не упомянута в описании. Описания фреймворка всегда её упоминают; проверьте свои переопределения в `[texts]` |
| `C8 fail ... '&nbsp;...'` | Разметка просочилась из источника; пропустите значение через `clean_text()` в адаптере |
| `C10 fail ... N > M bytes` | Ответ больше `limits.max_response_bytes`; посмотрите, что адаптер кладёт в один элемент |
| `C11 fail ... kind 'x' != cart.strategy 'y'` | На живом сервере другая стратегия корзины, чем в конфиге |
| `C13 fail ... HTTP 200, expected 401` | На развёрнутом сервере `auth.enabled = false`, либо у инструмента нет scope |
| `C12 fail GET /mcp: ... expected 405` | Путь не отвечает на `GET` кодом `405`: эндпоинт не этот фреймворк, либо ответ меняет прокси впереди |

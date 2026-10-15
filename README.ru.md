# shopmcp

[English](README.md) · **Русский**

Фреймворк MCP-серверов для интернет-магазинов: под магазин нужен только TOML-конфиг и, если источник данных не YML-фид, адаптер.

## Возможности

- **Всё из конфига.** Один `store.toml` описывает магазин, лимиты, передачу корзины, авторизацию и тексты; заготовку создаёт `shopmcp scaffold`.
- **Набор инструментов задают возможности адаптера.** Инструмент существует, только если адаптер объявил нужную capability (поиск, карточки, штрихкод, аналоги, категории, акции, пункты выдачи, заказы).
- **Передача корзины, а не оформление.** Агент собирает список, сервер возвращает ссылку (или ссылки на карточки), оплачивает покупатель на сайте магазина.
- **Гибридный доступ.** Каталог открыт; персональные инструменты закрыты OAuth 2.1 (сервер выступает resource server) с отдельным scope на инструмент ([docs/ru/auth.md](docs/ru/auth.md)).
- **Тексты для модели на двух языках.** `locale = "ru" | "en"` и переопределения магазина в `[texts]`.
- **Набор проверок соответствия.** `shopmcp check` гоняет проверки C1-C15 по локальной сборке или по живому URL.

## Стек

| Часть | Версия |
|---|---|
| Python | >= 3.12 |
| MCP Python SDK (`mcp`) | 2.3.0 (зафиксирована) |
| PyJWT (`pyjwt[crypto]`) | >= 2.10 |
| snowballstemmer | >= 3 |
| Менеджер пакетов / сборка | [uv](https://docs.astral.sh/uv/), hatchling |
| Для разработки | pytest >= 8, ruff >= 0.6 |

Готовые адаптеры: `yml` (фид YML / Yandex Market Language или Google Merchant, индексируется в SQLite) и `woocommerce` (снимок Store API без ключей). Адаптеры `bitrix`, `insales` и `http_json` пока заглушки. Подробности: [docs/ru/adapters.md](docs/ru/adapters.md).

## Движки магазинов

`shopmcp scaffold <id> --site URL` определяет движок магазина несколькими GET-запросами (или берёт `--engine`) и пишет для него готовые `[adapter]` и `[cart]`. Пресеты есть для десяти движков: Shopify, WooCommerce, Ecwid, 1С-Битрикс, PrestaShop, OpenCart, Magento 2, Tilda, InSales и Shop-Script. У InSales и Shop-Script корзина пока — ссылки на карточки; их корзина `form_post` — БЕТА (не проверена в живых браузерах; страницы-трамплина в коде пока нет) и пишется закомментированной. Таблица, что заполнить вручную и как работает определение: [docs/ru/engines.md](docs/ru/engines.md).

## Быстрый старт

```bash
uv sync
uv run shopmcp check stores/demo/store.toml   # проверка демо-магазина
uv run shopmcp serve stores/demo/store.toml   # MCP-эндпоинт: http://localhost:8000/mcp
```

Docker: `docker compose up -d --build`. Пошагово и с реальным выводом: [docs/ru/quickstart.md](docs/ru/quickstart.md).

## Команды

| Команда | Назначение |
|---|---|
| `shopmcp serve <store.toml>` | Запустить MCP-сервер по HTTP |
| `shopmcp index <store.toml>` | Один раз построить поисковый индекс и выйти; печатает состояние адаптера JSON-ом |
| `shopmcp check [store.toml] [--url URL] [--online] [--start-timeout СЕКУНДЫ]` | Проверки соответствия: локально или по живому эндпоинту |
| `shopmcp scaffold <store_id> --site URL [--engine X] [--feed F] [--prefix …] [--locale ru\|en]` | Создать `stores/<store_id>/store.toml`; движок определяется по сайту, если не задан `--engine` |

## Инструменты

Имена имеют вид `<prefix>_<суффикс>`. Демо-магазин (префикс `demo`) отдаёт 8 из них:

| Суффикс | Нужная capability | Доступ |
|---|---|---|
| `products_search` | `search` | открыт |
| `product_details` | `details` | открыт |
| `product_by_barcode` | `barcode` | открыт |
| `product_analogs` | `analogs` | открыт |
| `categories` | `categories` | открыт |
| `promotions` | `promotions` | открыт |
| `pickup_points` | `pickup_points` | открыт |
| `cart_link_create` | `details` | открыт |
| `orders_history` | `orders` | scope `orders.read` |
| `store_info` | не нужна, есть всегда | открыт |

Адаптер `yml` объявляет `search`, `details`, `barcode`, `analogs`, `categories` и `promotions`; магазин на нём получает `store_info`, шесть соответствующих инструментов и `cart_link_create`.

## Документация

| Документ | Содержание |
|---|---|
| [docs/ru/quickstart.md](docs/ru/quickstart.md) | Демо-магазин локально и в Docker; свой магазин |
| [docs/ru/configuration.md](docs/ru/configuration.md) | `store.toml` по полям, `${ENV}`, `locale`, `[texts]`, `[limits]`, `[auth]` |
| [docs/ru/engines.md](docs/ru/engines.md) | Десять поддерживаемых движков: каталог, корзина, статус; определение; что заполнить |
| [docs/ru/adapters.md](docs/ru/adapters.md) | Контракт адаптера, capabilities, как написать адаптер, адаптер `yml`, фид Google Merchant, адаптер `woocommerce` |
| [docs/ru/conformance.md](docs/ru/conformance.md) | Проверки C1-C15, запуск по живому URL |
| [docs/ru/auth.md](docs/ru/auth.md) | Гибридный доступ, OAuth resource server, scope на инструмент |
| [docs/design-notes.ru.md](docs/design-notes.ru.md) | Почему фреймворк устроен именно так |

## Структура проекта

```text
src/shopmcp/
  cli.py            serve | index | check | scaffold
  config.py         store.toml -> ${ENV} -> модели pydantic
  i18n.py           тексты ru/en и переопределения [texts]
  capabilities.py   перечень capabilities
  adapters/         контракт, реестр, yml (YML + Google Merchant), woocommerce, bitrix / insales / http_json (заглушки)
  tools/            описания инструментов
  cart/             стратегии передачи корзины
  engines.py        пресеты десяти движков для scaffold
  detect.py         определение движка по GET (scaffold)
  server/           ASGI-приложение, авторизация, лимит запросов, instructions
  conformance/      проверки C1-C15 и раннер
  templates/        шаблон store.toml для scaffold
stores/demo/        демо-магазин: store.toml + feed.xml
tests/              тесты pytest
Dockerfile, compose.yaml
```

## Тесты

```bash
uv run pytest
uv run ruff check
```

Тесты с маркером `online` ходят во внешний источник и в CI не запускаются.

## Лицензия

[Apache License 2.0](LICENSE).

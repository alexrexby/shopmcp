# Адаптеры

[English](../en/adapters.md) · **Русский**

Адаптер — единственный код, зависящий от магазина. Он читает источник данных магазина и приводит его к доменной модели фреймворка (`src/shopmcp/domain/models.py`). Ядро, инструменты, передача корзины и сервер сырых данных источника не видят.

Содержание: [Состояние](#состояние) · [Capabilities](#capabilities) · [Контракт](#контракт) · [Как написать адаптер](#как-написать-адаптер) · [Регистрация](#регистрация) · [Адаптер yml](#адаптер-yml)

## Состояние

| `adapter.kind` | Состояние |
|---|---|
| `yml` | Готов. Фид YML (Yandex Market Language) из файла или по `https://` |
| `bitrix` | Заглушка: не объявляет ни одной capability |
| `insales` | Заглушка: не объявляет ни одной capability |
| `http_json` | Заглушка: не объявляет ни одной capability |

Магазин на заглушке стартует, но отдаёт только `<prefix>_store_info`: все остальные инструменты требуют capability (`cart_link_create` требует `details`). В докстрингах модулей-заглушек описаны задуманный источник и ключи `[adapter]`. Roadmap: 10 engines — in progress.

## Capabilities

Адаптер объявляет, что умеет, методом `capabilities()` — это `frozenset` из `shopmcp.capabilities.Capability`. Инструмент регистрируется, только если его capability объявлена и суффикс не входит в `tools.disabled`. Capability — это обещание: если она объявлена, а метод бросает `not_supported`, проверка C3 падает.

| Capability | Метод адаптера | Какой инструмент включает |
|---|---|---|
| `search` | `search(query)` | `products_search` |
| `details` | `get_products(ids)` | `product_details`, `cart_link_create` |
| `barcode` | `by_barcode(barcode)` | `product_by_barcode` |
| `analogs` | `analogs(product_id, limit)` | `product_analogs` |
| `categories` | `categories(parent_id)` | `categories` |
| `promotions` | `promotions(page, limit, sort)` | `promotions` |
| `pickup_points` | `pickup_points(city, page, limit)` | `pickup_points` |
| `share_link` | `create_share_link(lines)` | инструмента нет; разрешает `cart.strategy = "share_link"` |
| `orders` | `orders(token, page, limit)` | `orders_history`, нужен токен со scope `orders.read` ([auth.md](auth.md)) |

`cart_link_create` требует `details`, потому что корзина сверяется с каталогом до построения ссылки. `store_info` не требует ничего: он берётся из `store.toml`.

## Контракт

Интерфейс — `ShopAdapter` в `src/shopmcp/adapters/base.py`. Наследуйтесь от `AdapterBase`: он не объявляет capabilities, и каждый его метод данных бросает `NotSupported`, так что переопределять нужно только объявленное.

| Метод | Контракт |
|---|---|
| `capabilities()` | Объявленный набор |
| `start()` | Загрузить или проиндексировать данные. Вызывается один раз из lifespan сервера; бросьте `Unavailable`, если адаптер совсем не может обслуживать |
| `close()` | Освободить ресурсы |
| `health()` | `dict` для `/healthz` (размер индекса, возраст фида, последняя ошибка) |
| `search(query)` | Возвращает `(products, total, facets)`. В `SearchQuery`: `q`, `category_id`, `price_min`, `price_max`, `in_stock`, `sort` (`relevance`, `price_asc`, `price_desc`, `new`, `discount`), `page`, `limit` |
| `get_products(ids)` | Товары по id; отсутствующие id пропускаются, это не ошибка |
| `by_barcode(barcode)` | Товар или `None` |
| `analogs(product_id, limit)` | Похожие товары |
| `categories(parent_id)` | Категории под родителем; `None` — верхний уровень |
| `promotions(page, limit, sort)` | `(products, total)` |
| `pickup_points(city, page, limit)` | `(points, total)` |
| `create_share_link(lines)` | Ссылка на корзину, созданную на стороне магазина |
| `orders(token, page, limit)` | Заказы пользователя, стоящего за `AccessToken`, `(orders, total)` |

Правила:

- **Бросайте только исключения из `shopmcp.errors`** (`NotFound`, `InvalidInput`, `Unavailable`, `NotSupported`, `Unauthorized`). Всё остальное превращается в `internal_error` без трассировки для модели.
- **Каждую строку из источника пропускайте через `clean_text()`** (`shopmcp.domain.text`): он убирает разметку и HTML-сущности. Проверка C8 падает на любом `&nbsp;`, `<br>` и подобном в ответе.
- **Id — строки.** Цены — `Decimal`; валюта по умолчанию `RUB`.
- **Не режьте страницы сами сверх запроса.** Инструменты считают `pages` и `has_more` по вашему `total`; бюджет размера ответа (`limits.max_response_bytes`) обеспечивает фреймворк.
- **Секреты — только через `${ENV}`** в `[adapter]`; в логи их не пишите (адаптер `yml` скрывает URL фида в логах).
- Адаптер получает `AdapterContext`: `data_dir` (каталог индекса), `http_timeout` (10 с), `store_id`, `config_dir` (каталог `store.toml`; относительные пути отсчитывайте от него).

## Как написать адаптер

Полный минимальный адаптер с двумя capabilities, `search` и `details`. Именно этот код прогнан через набор проверок соответствия (13 ok, 1 skipped: авторизация выключена) при `sample_query = "milk"`.

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

Точка входа модуля — фабрика `create(cfg, ctx)`. `cfg` — это `AdapterConfig`: `cfg.kind` и остальные ключи `[adapter]` в `cfg.model_extra`. Проверяйте их здесь и бросайте `ConfigError` (из `shopmcp.config`) с сообщением, называющим ключ, как это делает адаптер `yml` для неизвестных ключей.

## Регистрация

Соответствие вида и фабрики лежит в `src/shopmcp/adapters/registry.py`.

- **Встроенный вид.** Положите модуль в `src/shopmcp/adapters/` и добавьте строку в `_MODULES`, например `"mini": "shopmcp.adapters.mini"`. Модуль импортируется лениво, поэтому сломанный адаптер роняет только магазины своего вида. После этого `kind = "mini"` работает в `store.toml` и в CLI.
- **Из кода** (тесты, сторонний пакет): `registry.register("mini", factory)` до сборки приложения. Команда `shopmcp` внешние модули сама не подгружает, так что этот путь — для вашей собственной точки входа; раннер проверок так же запускается из Python:

  ```python
  from pathlib import Path
  from shopmcp.adapters import registry
  from shopmcp.conformance import runner

  registry.register("mini", mini_adapter.create)
  print(runner.report(runner.check_config(cfg, Path("."))))   # cfg: StoreConfig
  ```

Неизвестный вид даёт ошибку со списком видов, известных на этот момент.

## Адаптер yml

Источник истины — фид, SQLite-индекс — его кэш.

- `start()` один раз строит индекс. Фоновая задача перестраивает его каждые `adapter.refresh_minutes`. Неудачное обновление оставляет прежний индекс и видно как `last_error` в `/healthz`; фатален только первый старт без пригодного индекса (`upstream_unavailable`).
- Фид читается потоком, поэтому память не растёт даже на фидах в сотни мегабайт. `max_feed_mb` ограничивает размер; DTD с внутренними сущностями отклоняется.
- Некорректное или повторяющееся предложение пропускается и учитывается в `skipped`; фид оно не останавливает.
- Файл индекса — `$SHOPMCP_DATA_DIR/<id магазина>.sqlite`. Если в каталог писать нельзя, индекс уходит во временный каталог, а `/healthz` показывает `warning`.
- Capabilities: `search`, `details`, `barcode`, `analogs`, `categories`, `promotions`. Нет: `pickup_points`, `share_link`, `orders`.
- Поиск нормализует запрос и индекс русским и английским стеммерами snowball, поэтому словоформы совпадают (`молока` находит `молоко`).
- Ключи: `feed`, `refresh_minutes`, `max_feed_mb` ([configuration.md](configuration.md#adapter)).

Формат фида: стандартный `yml_catalog` с `shop/categories` и `shop/offers`; поддерживаемые элементы видны в демо-фиде [`stores/demo/feed.xml`](../../stores/demo/feed.xml) (`url`, `price`, `oldprice`, `currencyId`, `categoryId`, `picture`, `name` либо `typePrefix`+`vendor`+`model`, `vendor`, `barcode`, `description`, `param`, атрибут `available`).

# Адаптеры

[English](../en/adapters.md) · **Русский**

Адаптер — единственный код, зависящий от магазина. Он читает источник данных магазина и приводит его к доменной модели фреймворка (`src/shopmcp/domain/models.py`). Ядро, инструменты, передача корзины и сервер сырых данных источника не видят.

Содержание: [Состояние](#состояние) · [Capabilities](#capabilities) · [Контракт](#контракт) · [Как написать адаптер](#как-написать-адаптер) · [Регистрация](#регистрация) · [Адаптер yml](#адаптер-yml) · [Фид Google Merchant](#фид-google-merchant) · [Адаптер woocommerce](#адаптер-woocommerce)

## Состояние

| `adapter.kind` | Состояние |
|---|---|
| `yml` | Готов. Фид YML (Yandex Market Language) или Google Merchant из файла или по `https://` |
| `feed` | Готов. Тот же адаптер, что `yml`; под этим именем scaffold пишет пресеты движков |
| `woocommerce` | Готов. Снимок Store API WooCommerce без ключей |
| `bitrix` | Заглушка: не объявляет ни одной capability |
| `insales` | Заглушка: не объявляет ни одной capability |
| `http_json` | Заглушка: не объявляет ни одной capability |

Магазин на заглушке стартует, но отдаёт только `<prefix>_store_info`: все остальные инструменты требуют capability (`cart_link_create` требует `details`). В докстрингах модулей-заглушек описаны задуманный источник и ключи `[adapter]`. Пресеты для десяти движков: [engines.md](engines.md).

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

- `start()` один раз строит индекс. Фоновая задача перестраивает его каждые `adapter.refresh_minutes`. Неудачное обновление, что бы оно ни выбросило, оставляет прежний индекс и видно как `last_error` в `/healthz`; следующее обновление идёт по расписанию. Фатален только первый старт без пригодного индекса (`upstream_unavailable`); индекс, оставленный другой версией фреймворка, пригодным не считается.
- `/healthz` отвечает `503` с `"stale": true`, если индекс старше `stale_minutes` (по умолчанию три интервала обновления), три обновления подряд упали или задача обновления остановилась. Одно чтение фида ограничено 30 минутами целиком, поэтому хост, отдающий байты по капле, роняет обновление, а не держит его.
- Фид читается потоком, поэтому память не растёт даже на фидах в сотни мегабайт. `max_feed_mb` ограничивает размер; DTD с внутренними сущностями отклоняется.
- Некорректное или повторяющееся предложение пропускается и учитывается в `skipped`; фид оно не останавливает.
- Файл индекса — `$SHOPMCP_DATA_DIR/<id магазина>.sqlite`. Если в каталог писать нельзя, индекс уходит во временный каталог, а `/healthz` показывает `warning`.
- Capabilities: `search`, `details`, `barcode`, `analogs`, `categories`, `promotions`. Нет: `pickup_points`, `share_link`, `orders`.
- Поиск нормализует запрос и индекс русским и английским стеммерами snowball, поэтому словоформы совпадают (`молока` находит `молоко`).
- Ключи: `feed`, `format`, `refresh_minutes`, `max_feed_mb`, `stale_minutes`, `cart_ref` ([configuration.md](configuration.md#adapter)). Фиды Google Merchant: [ниже](#фид-google-merchant).

Формат фида: стандартный `yml_catalog` с `shop/categories` и `shop/offers`; поддерживаемые элементы видны в демо-фиде [`stores/demo/feed.xml`](../../stores/demo/feed.xml) (`url`, `price`, `oldprice`, `currencyId`, `categoryId`, `picture`, `name` либо `typePrefix`+`vendor`+`model`, `vendor`, `barcode`, `description`, `param`, атрибут `available`).

## Фид Google Merchant

Адаптер `yml` (вид `yml` или `feed`) читает и фид Google Merchant. `adapter.format = "auto"` (по умолчанию) выбирает читателя по корневому элементу: `rss` или Atom `feed` — Merchant, `yml_catalog` — YML. Индекс, поиск, обновление и `/healthz` те же; отличается только читатель.

Что читается (`src/shopmcp/adapters/yml/gmc.py`):

- **Позиции**: RSS `item` или Atom `entry`; пространство имён `g:` (`http://base.google.com/ns/1.0`) должно быть объявлено до первой позиции. Atom без `g:` (фид Shopify с `s:`) — не Merchant и отклоняется целиком.
- **Обязательно в позиции**: `g:id`, ссылка (`g:link` или обычный `link`), название и `g:price`. Позиция без любого из них пропускается и учитывается в `skipped`.
- **Цена** — `1299.00 RUB` (валюта может стоять первой). Читаются группы разрядов и десятичная запятая (`1 299,00`, `1.299,00`, `1,299.00`); одиночная запятая перед ровно тремя цифрами (`1,299`) неоднозначна, позиция пропускается, а не угадывается. `RUR` читается как `RUB`.
- **Акция**: `g:sale_price` ниже `g:price` в той же валюте делает текущей ценой акционную, а `g:price` — старой, с `discount_percent`. Если задан `g:sale_price_effective_date` (`начало/конец`, ISO 8601, любая сторона может быть пустой), акция действует только внутри периода; нечитаемая дата — акции нет. Период проверяется на момент построения индекса, а не запроса: акция начинается и кончается с задержкой до одного обновления.
- **Варианты**: `g:item_group_id` становится группой товара; при `cart_ref` по умолчанию позиция — вариант этой группы ([configuration.md](configuration.md#adaptercart_ref)).
- **Наличие**: `in_stock` — в наличии; `out_of_stock`, `preorder` и `backorder` — нет. Любое другое значение оставляет наличие неизвестным.
- **Категории**: у Merchant нет дерева категорий, поэтому `g:product_type` (`A > B > C`) превращается в категории с синтетическими id, по родителю на каждый префикс пути.
- **Остальные поля**: `g:brand` — производитель, `g:gtin` — штрихкод (8-14 цифр), `g:image_link` и `g:additional_image_link` — картинки, `g:color`, `g:size`, `g:material`, `g:pattern`, `g:gender`, `g:age_group` — параметры.

Пределы: путь категории обрезается на 10 уровнях, `g:product_type` — на 750 символах, позиция больше 10 000 узлов пропускается, фид с внутренним DTD отклоняется, размер ограничен `max_feed_mb`. Фид читается потоком, как YML.

Поддерживаемые элементы видны в демо-фиде [`stores/demo/feed-gmc.xml`](../../stores/demo/feed-gmc.xml).

## Адаптер woocommerce

`kind = "woocommerce"`, `site = "https://shop.example"`. Каталог читается из публичного Store API (`/wp-json/wc/store/v1`): только GET, без ключа и без кук. Capabilities: `search`, `details`, `analogs`, `categories`, `promotions`. Нет: `barcode` (Store API не публикует GTIN), `pickup_points`, `share_link`, `orders`.

- **Снимок, а не живые запросы.** Адаптер постранично читает весь каталог в тот же SQLite-индекс, что и фид, и перестраивает его каждые `adapter.refresh_minutes`, поэтому поиск, фильтры и сортировка ведут себя как на фиде. Первая сборка в `start()`, прежний индекс при неудачном обновлении, `/healthz` `503` при устаревании — как у `yml`.
- **Вежливый обход.** По 100 товаров на страницу, один запрос в секунду, страница ответа ограничена 4 МБ. Запрос, упавший с `429`, `502`, `503` или `504`, повторяется дважды с растущей паузой (`Retry-After` учитывается до 60 с; более долгое ожидание роняет чтение). Пределы одного чтения: `max_pages` на листинг, `max_minutes` на всё чтение и `max_feed_mb` на все байты. Магазин, который не укладывается, отклоняется с названием ключа, который нужно поднять.
- **Цены в минорных единицах**: Store API присылает `"1899"` при `currency_minor_unit: 2`, адаптер читает это как 18.99; `"115"` при `0` — это 115. Обычная цена выше акционной даёт старую цену и скидку.
- **Вариации.** Вариативный товар — не единица покупки, ею являются его вариации. Каждая вариация становится товаром с названием родителя, в категориях родителя, с родителем как группой; используются её собственные ссылка и цена. `{id}` в шаблоне корзины — id вариации, тот, что принимают `?add-to-cart=` и `/checkout-link/`.
- **Пропускаются**: товары `grouped` (набор ссылок), `external` (продаются в другом месте), `variable`-товар без вариаций и вариация, оставляющая атрибут «Любой» (ссылка корзины по одному её id, скорее всего, ничего не добавит; живьём не проверено). Каждый пропуск учитывается в `skipped`.
- **Неполный обход никогда не заменяет индекс.** Если листинг кончился раньше, чем говорят его `X-WP-TotalPages` / `X-WP-Total` (несколько позиций или 1% допускаются на удаления во время чтения), если листинг товаров пуст или упёрся предел, чтение падает: каталог — снимок, и короткий ответ из кэша, файрвола или плагина видимости иначе заменил бы его целиком. Отдаётся прежний индекс, причина видна как `last_error` в `/healthz`. Фатален только первый старт без индекса.
- **Локальная проверка.** Первое чтение большого магазина занимает минуты: `shopmcp check --online --start-timeout …` ([conformance.md](conformance.md#режимы)).

Ключи: `site`, `refresh_minutes`, `stale_minutes`, `max_pages`, `max_minutes`, `max_feed_mb` ([configuration.md](configuration.md#adapter)).

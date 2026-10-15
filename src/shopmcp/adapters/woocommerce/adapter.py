"""WooCommerce adapter: a snapshot of the keyless Store API in the feed index.

The shop's catalog is read page by page (`store_api.read`) into the same SQLite FTS5
index as a YML feed, so search, filters, analogs and promotions behave alike on both.
Everything about the index's life is the YML adapter's: the first build in `start()`,
a rebuild every `refresh_minutes`, the previous index served while the shop is down,
/healthz 503 once it is stale. Only the source differs.

The cart key is the index default: a variation is `variant_id` under its product, and
`{id}` in a cart template is the variation id, the one `?add-to-cart=` and
`/checkout-link/` take.
"""

import logging
import urllib.parse
from pathlib import Path

from shopmcp.adapters.base import AdapterContext
from shopmcp.adapters.woocommerce import store_api
from shopmcp.adapters.yml import index
from shopmcp.adapters.yml.adapter import FEED_SECONDS, YmlAdapter
from shopmcp.adapters.yml.feed import FeedStats
from shopmcp.capabilities import Capability
from shopmcp.config import AdapterConfig, ConfigError

log = logging.getLogger(__name__)

# No barcodes: the Store API does not publish GTINs.
CAPABILITIES = frozenset(
    {
        Capability.SEARCH,
        Capability.DETAILS,
        Capability.ANALOGS,
        Capability.CATEGORIES,
        Capability.PROMOTIONS,
    }
)
_KEYS = {"site", "refresh_minutes", "stale_minutes", "max_pages"}
MAX_PAGES = 200  # per listing: 20 000 products and as many variations


class WooAdapter(YmlAdapter):
    def __init__(
        self,
        site: str,
        ctx: AdapterContext,
        *,
        refresh_minutes: int = 60,
        stale_minutes: int | None = None,
        max_pages: int = MAX_PAGES,
    ) -> None:
        super().__init__(
            site + store_api.API_PATH,  # the label in logs and health
            ctx,
            refresh_minutes=refresh_minutes,
            stale_minutes=stale_minutes,
        )
        self.site = site
        self.max_pages = max_pages

    def capabilities(self) -> frozenset[Capability]:
        return CAPABILITIES

    def _rebuild(self, path: Path) -> None:
        stats = FeedStats()

        def feed_meta() -> dict[str, str]:
            return {"feed_offers": str(stats.offers), "feed_skipped": str(stats.skipped)}

        items = store_api.read(
            self.site,
            stats,
            timeout=self.ctx.http_timeout,
            max_pages=self.max_pages,
            max_seconds=FEED_SECONDS,
        )
        built = index.build(path, items, feed_meta=feed_meta, cart_ref=self.cart_ref)
        self._last_error = None
        self._failures = 0
        log.info(
            "store api %s: %d products, %d categories, %d skipped",
            self._label,
            built.products,
            built.categories,
            stats.skipped + built.duplicates,
        )


def create(cfg: AdapterConfig, ctx: AdapterContext) -> WooAdapter:
    extra = dict(cfg.model_extra or {})
    unknown = sorted(set(extra) - _KEYS)
    if unknown:
        raise ConfigError(f"adapter: unknown keys for kind {cfg.kind!r}: {unknown}")
    site = extra.get("site")
    parts = urllib.parse.urlsplit(site) if isinstance(site, str) else None
    if (
        parts is None
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        raise ConfigError(
            "adapter.site must be the https URL of the WordPress site, without query: "
            "https://shop.example or https://shop.example/shop"
        )
    refresh = extra.get("refresh_minutes", 60)
    stale = extra.get("stale_minutes")
    max_pages = extra.get("max_pages", MAX_PAGES)
    if not isinstance(refresh, int) or isinstance(refresh, bool) or refresh < 0:
        raise ConfigError("adapter.refresh_minutes must be an integer >= 0 (0 = no refresh)")
    if stale is not None and (
        not isinstance(stale, int) or isinstance(stale, bool) or stale <= refresh
    ):
        raise ConfigError("adapter.stale_minutes must be an integer above refresh_minutes")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise ConfigError("adapter.max_pages must be a positive integer")
    return WooAdapter(
        site.rstrip("/"),  # type: ignore[union-attr]
        ctx,
        refresh_minutes=refresh,
        stale_minutes=stale,
        max_pages=max_pages,
    )

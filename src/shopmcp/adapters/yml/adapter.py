"""YML feed adapter: the feed is the source of truth, the SQLite index is its cache.

`start()` builds the index once; a background task rebuilds it every
`refresh_minutes`. A failed refresh keeps serving the previous index and shows
up in `health()`; only a first start with no usable index at all is fatal.
"""

import asyncio
import logging
import os
import sqlite3
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import anyio.to_thread

from shopmcp.adapters.base import AdapterBase, AdapterContext, SearchQuery
from shopmcp.adapters.yml import index
from shopmcp.adapters.yml.feed import FeedError, FeedStats, open_feed, parse, redact
from shopmcp.capabilities import Capability
from shopmcp.config import AdapterConfig, ConfigError
from shopmcp.domain.models import Category, Facets, Product
from shopmcp.errors import Unavailable

log = logging.getLogger(__name__)
T = TypeVar("T")

CAPABILITIES = frozenset(
    {
        Capability.SEARCH,
        Capability.DETAILS,
        Capability.BARCODE,
        Capability.ANALOGS,
        Capability.CATEGORIES,
        Capability.PROMOTIONS,
    }
)
_KEYS = {"feed", "refresh_minutes", "max_feed_mb"}


class YmlAdapter(AdapterBase):
    def __init__(
        self,
        feed: str,
        ctx: AdapterContext,
        *,
        refresh_minutes: int = 60,
        max_feed_mb: int = 200,
    ) -> None:
        self.feed = feed
        self._label = redact(feed)  # for logs: the URL may carry a secret from ${ENV}
        self.ctx = ctx
        self.refresh_minutes = refresh_minutes
        self.max_bytes = max_feed_mb * 1024 * 1024
        self.index: index.Index | None = None
        self._refresher: asyncio.Task[None] | None = None
        self._feed_stats = FeedStats()
        self._last_error: str | None = None
        self._warning: str | None = None

    def capabilities(self) -> frozenset[Capability]:
        return CAPABILITIES

    async def start(self) -> None:
        path = self._index_path()
        try:
            await anyio.to_thread.run_sync(self._rebuild, path)
        except (FeedError, OSError, sqlite3.Error) as e:
            self._last_error = str(e)
            if not path.exists():
                log.error("feed %s: first index build failed: %s", self._label, e)
                raise Unavailable() from e
            log.warning("feed %s: rebuild failed, serving the previous index: %s", self._label, e)
        self.index = index.Index(path)
        if self.refresh_minutes > 0:
            self._refresher = asyncio.create_task(self._refresh_loop(path))

    async def close(self) -> None:
        if self._refresher is not None:
            self._refresher.cancel()
            try:
                await self._refresher
            except asyncio.CancelledError:
                pass
            self._refresher = None

    async def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "feed_offers": self._feed_stats.offers,
            "skipped": self._feed_stats.skipped,
            "last_error": self._last_error,
            "warning": self._warning,
        }
        if self.index is not None:
            stats = await self._call(self.index.stats)
            stats["index_age_seconds"] = round(time.time() - stats.pop("built_at"))
            out.update(stats)
        return out

    async def search(self, q: SearchQuery) -> tuple[list[Product], int, Facets]:
        return await self._call(self._ready().search, q)

    async def get_products(self, ids: Sequence[str]) -> list[Product]:
        return await self._call(self._ready().get_products, ids)

    async def by_barcode(self, barcode: str) -> Product | None:
        return await self._call(self._ready().by_barcode, barcode)

    async def analogs(self, product_id: str, limit: int) -> list[Product]:
        return await self._call(self._ready().analogs, product_id, limit)

    async def categories(self, parent_id: str | None) -> list[Category]:
        return await self._call(self._ready().categories, parent_id)

    async def promotions(self, page: int, limit: int, sort: str) -> tuple[list[Product], int]:
        return await self._call(self._ready().promotions, page, limit, sort)

    # --- internals ------------------------------------------------------------

    def _ready(self) -> index.Index:
        if self.index is None:
            raise Unavailable()
        return self.index

    async def _call(self, fn: Callable[..., T], *args: Any) -> T:
        try:
            return await anyio.to_thread.run_sync(partial(fn, *args))
        except sqlite3.Error as e:
            log.error("index query failed: %s", e)
            raise Unavailable() from e

    def _index_path(self) -> Path:
        """`{data_dir}/{store_id}.sqlite`; an unwritable data_dir falls back to a temp dir."""
        data_dir = self.ctx.data_dir
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            writable = os.access(data_dir, os.W_OK)
        except OSError:
            writable = False
        if not writable:
            # The index is a cache: a root-owned volume must not keep the store down.
            data_dir = Path(tempfile.mkdtemp(prefix=f"shopmcp-{self.ctx.store_id}-"))
            self._warning = f"{self.ctx.data_dir} is not writable, index kept in {data_dir}"
            log.warning(self._warning)
        return data_dir / f"{self.ctx.store_id}.sqlite"

    def _rebuild(self, path: Path) -> None:
        stats = FeedStats()
        with open_feed(self.feed, timeout=self.ctx.http_timeout, max_bytes=self.max_bytes) as f:
            built = index.build(path, parse(f, stats), feed_date=lambda: stats.date)
        # A repeated offer id is skipped like any other invalid offer.
        stats.skipped += built.duplicates
        self._feed_stats = stats
        self._last_error = None
        log.info(
            "feed %s: %d products, %d categories, %d skipped",
            self._label,
            built.products,
            built.categories,
            stats.skipped,
        )

    async def _refresh_loop(self, path: Path) -> None:
        while True:
            await anyio.sleep(self.refresh_minutes * 60)
            try:
                await anyio.to_thread.run_sync(self._rebuild, path)
            except (FeedError, OSError, sqlite3.Error) as e:
                self._last_error = str(e)
                log.warning("feed %s: refresh failed, keeping the old index: %s", self._label, e)


def create(cfg: AdapterConfig, ctx: AdapterContext) -> YmlAdapter:
    extra = dict(cfg.model_extra or {})
    unknown = sorted(set(extra) - _KEYS)
    if unknown:
        raise ConfigError(f"adapter: unknown keys for kind 'yml': {unknown}")
    feed = extra.get("feed")
    if not isinstance(feed, str) or not feed:
        raise ConfigError("adapter.feed is required for kind 'yml'")
    if not urllib.parse.urlsplit(feed).scheme:  # a path: relative to store.toml, not the CWD
        feed = str(ctx.config_dir / feed)
    refresh = extra.get("refresh_minutes", 60)
    max_mb = extra.get("max_feed_mb", 200)
    if not isinstance(refresh, int) or isinstance(refresh, bool) or refresh < 0:
        raise ConfigError("adapter.refresh_minutes must be an integer >= 0 (0 = no refresh)")
    if not isinstance(max_mb, int) or isinstance(max_mb, bool) or max_mb < 1:
        raise ConfigError("adapter.max_feed_mb must be a positive integer")
    return YmlAdapter(feed, ctx, refresh_minutes=refresh, max_feed_mb=max_mb)

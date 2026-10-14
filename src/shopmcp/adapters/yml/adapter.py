"""YML feed adapter: the feed is the source of truth, the SQLite index is its cache.

`start()` builds the index once; a background task rebuilds it every
`refresh_minutes`. A failed refresh keeps serving the previous index and shows
up in `health()`; only a first start with no usable index at all is fatal.
Whatever a rebuild raises, the refresh goes on: a task that dies quietly would leave
the store selling yesterday's prices behind a green health check.
"""

import asyncio
import contextlib
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
_KEYS = {"feed", "refresh_minutes", "max_feed_mb", "stale_minutes"}
FEED_SECONDS = 30 * 60  # one whole feed read; the socket timeout bounds a single operation
STALE_FAILURES = 3  # refreshes failed in a row that make the index stale
# Expected failures carry a curated message; anything else shows only its type in health().
_EXPECTED = (FeedError, OSError, sqlite3.Error)


class YmlAdapter(AdapterBase):
    def __init__(
        self,
        feed: str,
        ctx: AdapterContext,
        *,
        refresh_minutes: int = 60,
        max_feed_mb: int = 200,
        stale_minutes: int | None = None,
    ) -> None:
        self.feed = feed
        self._label = redact(feed)  # for logs: the URL may carry a secret from ${ENV}
        self.ctx = ctx
        self.refresh_minutes = refresh_minutes
        self._interval = refresh_minutes * 60.0  # seconds between refreshes; tests shorten it
        # An index older than this is stale: /healthz answers 503.
        self.stale_seconds = (stale_minutes or 3 * refresh_minutes) * 60
        self.max_bytes = max_feed_mb * 1024 * 1024
        self.index: index.Index | None = None
        self._refresher: asyncio.Task[None] | None = None
        self._failures = 0  # builds failed in a row
        self._last_error: str | None = None
        self._warning: str | None = None

    def capabilities(self) -> frozenset[Capability]:
        return CAPABILITIES

    async def start(self) -> None:
        path = self._index_path()
        index.sweep(path, older_than=2 * FEED_SECONDS)
        try:
            await anyio.to_thread.run_sync(self._rebuild, path)
        except Exception as e:
            usable = index.usable(path)  # False: missing, or left by another schema version
            self._failed(
                e,
                "rebuild failed, serving the previous index"
                if usable
                else "first index build failed",
            )
            if not usable:
                log.error("feed %s: no usable index to serve", self._label)
                raise Unavailable() from e
        self.index = index.Index(path)
        if self.refresh_minutes > 0:
            self._refresher = asyncio.create_task(self._refresh_loop(path))
            self._refresher.add_done_callback(self._refresher_done)

    async def close(self) -> None:
        task, self._refresher = self._refresher, None
        if task is None:
            return
        task.cancel()
        # A task that died has been logged by _refresher_done: it must not fail the stop.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "last_error": self._last_error,
            "failed_refreshes": self._failures,
            "warning": self._warning,
        }
        if self.index is not None:
            stats = await self._call(self.index.stats)
            age = time.time() - stats.pop("built_at")
            stats["index_age_seconds"] = round(age)
            out.update(stats)
            dead = self._refresher is not None and self._refresher.done()
            out["stale"] = self.refresh_minutes > 0 and (
                age > self.stale_seconds or self._failures >= STALE_FAILURES or dead
            )
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

        def feed_meta() -> dict[str, str]:
            return {
                "feed_date": stats.date or "",
                "feed_offers": str(stats.offers),
                "feed_skipped": str(stats.skipped),
            }

        with open_feed(
            self.feed,
            timeout=self.ctx.http_timeout,
            max_bytes=self.max_bytes,
            max_seconds=FEED_SECONDS,
        ) as f:
            built = index.build(path, parse(f, stats), feed_meta=feed_meta)
        self._last_error = None
        self._failures = 0
        log.info(
            "feed %s: %d products, %d categories, %d skipped",
            self._label,
            built.products,
            built.categories,
            stats.skipped + built.duplicates,
        )

    def _failed(self, e: Exception, what: str) -> None:
        """Record a failed build for health(); call inside the `except` block."""
        self._failures += 1
        if isinstance(e, _EXPECTED):
            self._last_error = str(e)
            log.warning("feed %s: %s: %s", self._label, what, e)
        else:
            self._last_error = f"unexpected {type(e).__name__}"
            log.exception("feed %s: %s: unexpected %s", self._label, what, type(e).__name__)

    async def _refresh_loop(self, path: Path) -> None:
        """Ends only by cancellation: every failure is recorded and the next cycle runs."""
        while True:
            await anyio.sleep(self._interval)
            try:
                await anyio.to_thread.run_sync(self._rebuild, path)
            except Exception as e:  # noqa: BLE001 - see the module docstring
                self._failed(e, "refresh failed, keeping the old index")

    def _refresher_done(self, task: asyncio.Task[None]) -> None:
        # The loop catches everything; this is the alarm for whatever still gets through.
        if task.cancelled() or task.exception() is None:
            return
        self._last_error = f"refresh stopped: {type(task.exception()).__name__}"
        log.error(
            "feed %s: refresh task died, the index is no longer refreshed",
            self._label,
            exc_info=task.exception(),
        )


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
    stale = extra.get("stale_minutes")
    if not isinstance(refresh, int) or isinstance(refresh, bool) or refresh < 0:
        raise ConfigError("adapter.refresh_minutes must be an integer >= 0 (0 = no refresh)")
    if not isinstance(max_mb, int) or isinstance(max_mb, bool) or max_mb < 1:
        raise ConfigError("adapter.max_feed_mb must be a positive integer")
    if stale is not None and (
        not isinstance(stale, int) or isinstance(stale, bool) or stale <= refresh
    ):
        # Not above the refresh interval, every index would turn stale between two refreshes.
        raise ConfigError("adapter.stale_minutes must be an integer above refresh_minutes")
    return YmlAdapter(feed, ctx, refresh_minutes=refresh, max_feed_mb=max_mb, stale_minutes=stale)

"""SQLite FTS5 index over a parsed feed: a rebuildable cache, never the source of truth.

A rebuild writes a uniquely named `<file>.<random>.tmp` and swaps it in with
`os.replace`, so two builders on one data dir never touch each other's file. Readers
open a read-only connection per query, so a reader in flight keeps the old file and
the next query sees the new one: no locks between the refresher and the tools.
"""

import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shopmcp.adapters.base import SearchQuery
from shopmcp.adapters.yml.feed import CartRefRule
from shopmcp.adapters.yml.stem import stem_text, stems, variants
from shopmcp.cart.refs import CartRef
from shopmcp.domain.models import Category, Facets, FacetValue, Product
from shopmcp.errors import InvalidInput

# Bumped whenever the tables or the stored Product json change: an index of another
# version is treated as absent, never served as "the previous index".
SCHEMA_VERSION = "3"
_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
-- position = feed order and the FTS rowid
CREATE TABLE products(
    position INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, json TEXT NOT NULL,
    category_id TEXT, price REAL NOT NULL, old_price REAL, available INTEGER,
    group_id TEXT, cart_ref_json TEXT NOT NULL);
CREATE INDEX products_category ON products(category_id);
CREATE TABLE barcodes(barcode TEXT PRIMARY KEY, product_id TEXT NOT NULL);
CREATE TABLE categories(
    id TEXT PRIMARY KEY, parent_id TEXT, name TEXT NOT NULL, path_json TEXT NOT NULL,
    product_count INTEGER);
CREATE INDEX categories_parent ON categories(parent_id);
CREATE VIRTUAL TABLE products_fts USING fts5(
    name, vendor, category_path, params, description,
    content='', tokenize='unicode61 remove_diacritics 2');
"""
DEFAULT_CART_REF = CartRefRule()  # group_id is the product, the feed id its variant
# Column weights for bm25, in the order of the products_fts columns.
_RANK = "bm25(products_fts, 10, 4, 3, 2, 1)"
_FACETS = 10
_DISCOUNT = "COALESCE((p.old_price - p.price) / p.old_price, 0) DESC"
_ORDER = {
    "price_asc": "p.price, p.position",
    "price_desc": "p.price DESC, p.position",
    # YML carries no arrival date; later in the feed is the best proxy for "new".
    "new": "p.position DESC",
    "discount": f"{_DISCOUNT}, p.position",
}
_SUBTREE = (
    "p.category_id IN (WITH RECURSIVE sub(id) AS (SELECT ? UNION "
    "SELECT c.id FROM categories c JOIN sub ON c.parent_id = sub.id) SELECT id FROM sub)"
)


@dataclass(frozen=True)
class BuildStats:
    products: int
    categories: int
    duplicates: int  # offers whose id was already indexed; the first one wins


def build(
    path: Path,
    items: Iterable[Category | Product],
    feed_meta: Callable[[], Mapping[str, str]] = dict,
    cart_ref: Callable[[Product], Mapping[str, Any]] = DEFAULT_CART_REF,
) -> BuildStats:
    """Index `items` into a temp file next to `path`, then atomically replace `path`.

    `feed_meta` (`feed_date`, `feed_offers`, `feed_skipped`) is called after `items` is
    exhausted: a streaming parser learns them on the way. Stored in `meta` with the counts,
    so `stats()` never scans the tables. `cart_ref` gives the raw `CartRef` fields of a
    product (`[adapter.cart_ref]`); they are validated on read, see `Index.cart_refs`.
    """
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    os.close(fd)  # an empty file is an empty database to sqlite
    tmp = Path(name)
    try:
        with closing(sqlite3.connect(tmp)) as db:
            db.executescript(_SCHEMA)
            products = duplicates = 0
            for item in items:
                if isinstance(item, Category):
                    db.execute(
                        "INSERT OR REPLACE INTO categories VALUES (?, ?, ?, ?, NULL)",
                        (
                            item.id,
                            item.parent_id,
                            item.name,
                            json.dumps(item.path, ensure_ascii=False),
                        ),
                    )
                elif _insert_product(db, item, products + 1, cart_ref(item)):
                    products += 1
                else:
                    duplicates += 1
            categories = _count_categories(db)
            meta = {
                **feed_meta(),
                "schema": SCHEMA_VERSION,
                "built_at": str(time.time()),
                "products": str(products),
                "categories": str(categories),
                "duplicates": str(duplicates),
            }
            db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
            db.execute("INSERT INTO products_fts(products_fts) VALUES ('optimize')")
            db.commit()
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return BuildStats(products=products, categories=categories, duplicates=duplicates)


def usable(path: Path) -> bool:
    """An index file of this schema version; anything else counts as no index at all."""
    if not path.exists():
        return False
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == SCHEMA_VERSION


def sweep(path: Path, older_than: float) -> None:
    """Remove temp files of builds killed mid-way (a finished or failed build removes its
    own). A live build rewrites its file all the time, so only stale ones go."""
    cutoff = time.time() - older_than
    for tail in path.parent.glob(path.name + ".*.tmp*"):
        try:
            if tail.stat().st_mtime < cutoff:
                tail.unlink()
        except OSError:
            pass


def _insert_product(
    db: sqlite3.Connection, p: Product, position: int, cart_ref: Mapping[str, Any]
) -> bool:
    old = p.price.old.amount if p.price.old else None
    cur = db.execute(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        (
            position,
            p.id,
            p.model_dump_json(exclude_none=True),
            p.category_id,
            float(p.price.current.amount),
            float(old) if old is not None else None,
            None if p.available is None else int(p.available),
            p.group_id,
            json.dumps(cart_ref, ensure_ascii=False),
        ),
    )
    if not cur.rowcount:
        return False
    db.executemany("INSERT OR IGNORE INTO barcodes VALUES (?, ?)", [(b, p.id) for b in p.barcodes])
    db.execute(
        "INSERT INTO products_fts(rowid, name, vendor, category_path, params, description)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            position,
            stem_text(p.name),
            stem_text(p.vendor),
            stem_text(" ".join(p.category_path)),
            stem_text(" ".join(f"{x.name} {x.value}" for x in p.params)),
            stem_text(p.description),
        ),
    )
    return True


def _count_categories(db: sqlite3.Connection) -> int:
    """product_count of a category includes its subtree, as the search filter does."""
    parents = dict(db.execute("SELECT id, parent_id FROM categories").fetchall())
    counts = dict.fromkeys(parents, 0)
    for cid, n in db.execute("SELECT category_id, COUNT(*) FROM products GROUP BY category_id"):
        seen: set[str] = set()
        while cid in parents and cid not in seen:
            seen.add(cid)
            counts[cid] += n
            cid = parents[cid]
    db.executemany(
        "UPDATE categories SET product_count = ? WHERE id = ?", [(n, c) for c, n in counts.items()]
    )
    return len(parents)


def _match(text: str, op: str, expand: bool = False) -> str:
    """Stems as quoted prefix terms: quoting keeps FTS5 syntax out of user input.

    `expand` lets a word match any of its `variants()` (творожный finds творог)."""
    terms = dict.fromkeys(stems(text))  # unique, order kept
    if not expand:
        return f" {op} ".join(f'"{t}"*' for t in terms)
    groups = (" OR ".join(f'"{v}"*' for v in variants(t)) for t in terms)
    return f" {op} ".join(f"({g})" for g in groups)


class Index:
    """Read side. Every call opens its own read-only connection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)

    def search(self, q: SearchQuery) -> tuple[list[Product], int, Facets]:
        filters, args = [], []
        if q.category_id is not None:
            filters.append(_SUBTREE)
            args.append(q.category_id)
        if q.price_min is not None:
            filters.append("p.price >= ?")
            args.append(float(q.price_min))
        if q.price_max is not None:
            filters.append("p.price <= ?")
            args.append(float(q.price_max))
        if q.in_stock:
            filters.append("p.available = 1")
        with closing(self._connect()) as db:
            if not q.q:
                return self._page(db, None, filters, args, q)
            if not stems(q.q):  # punctuation only: nothing to look for
                return [], 0, Facets()
            found = self._page(db, _match(q.q, "AND", expand=True), filters, args, q)
            if found[1] == 0:  # every word is too strict: retry with any word
                found = self._page(db, _match(q.q, "OR", expand=True), filters, args, q)
            return found

    def _page(
        self,
        db: sqlite3.Connection,
        match: str | None,
        filters: list[str],
        args: list[Any],
        q: SearchQuery,
    ) -> tuple[list[Product], int, Facets]:
        source = "products p"
        where, params = list(filters), list(args)
        if match is not None:
            source += " JOIN products_fts ON products_fts.rowid = p.position"
            where.insert(0, "products_fts MATCH ?")
            params.insert(0, match)
        cond = " AND ".join(where) or "1"
        clause = f"FROM {source} WHERE {cond}"
        relevance = f"{_RANK}, p.position" if match is not None else "p.position"
        order = _ORDER.get(q.sort, relevance)

        (total,) = db.execute(f"SELECT COUNT(*) {clause}", params).fetchone()
        rows = db.execute(
            f"SELECT p.json {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, q.limit, (q.page - 1) * q.limit],
        ).fetchall()
        facets = db.execute(
            "SELECT p.category_id, COALESCE(c.name, p.category_id), COUNT(*) AS n"
            f" FROM {source} LEFT JOIN categories c ON c.id = p.category_id"
            f" WHERE {cond} AND p.category_id IS NOT NULL GROUP BY p.category_id"
            " ORDER BY n DESC, p.category_id LIMIT ?",
            [*params, _FACETS],
        )
        return (
            [Product.model_validate_json(j) for (j,) in rows],
            total,
            Facets(categories=[FacetValue(id=c, name=nm, count=n) for c, nm, n in facets]),
        )

    def get_products(self, ids: Sequence[str]) -> list[Product]:
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return []
        with closing(self._connect()) as db:
            found = dict(
                db.execute(
                    f"SELECT id, json FROM products WHERE id IN ({','.join('?' * len(wanted))})",
                    wanted,
                ).fetchall()
            )
        return [Product.model_validate_json(found[i]) for i in wanted if i in found]

    def cart_refs(self, ids: Sequence[str]) -> dict[str, CartRef]:
        """Validated here, not at build: the `CartRef` id rule applies as it is now. An id
        not in the index gets the adapter default, the feed id itself. A stored ref that
        fails validation is InvalidInput naming the id (an id rule raises its own)."""
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}
        with closing(self._connect()) as db:
            found = dict(
                db.execute(
                    "SELECT id, cart_ref_json FROM products"
                    f" WHERE id IN ({','.join('?' * len(wanted))})",
                    wanted,
                ).fetchall()
            )
        refs = {}
        for i in wanted:
            try:
                refs[i] = (
                    CartRef.model_validate_json(found[i]) if i in found else CartRef(product_id=i)
                )
            except ValidationError as e:
                # An option value over its limit and the like: the model learns which id,
                # instead of the whole cart failing with internal_error.
                raise InvalidInput(details={"invalid_cart_ref": i}) from e
        return refs

    def by_barcode(self, barcode: str) -> Product | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT p.json FROM barcodes b JOIN products p ON p.id = b.product_id"
                " WHERE b.barcode = ?",
                (barcode.strip(),),
            ).fetchone()
        return Product.model_validate_json(row[0]) if row else None

    def analogs(self, product_id: str, limit: int) -> list[Product]:
        """Same category, sharing a word of the name (name column only), closest in price first."""
        with closing(self._connect()) as db:
            base = db.execute(
                "SELECT json, category_id, price FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            if base is None:
                return []
            name = Product.model_validate_json(base[0]).name
            words = " ".join(s for s in stems(name) if len(s) >= 3 and not s.isdigit())
            if not words:
                return []
            rows = db.execute(
                "SELECT p.json FROM products p"
                " JOIN products_fts ON products_fts.rowid = p.position"
                " WHERE products_fts MATCH ? AND p.id != ? AND p.category_id IS ?"
                " ORDER BY ABS(p.price - ?), p.position LIMIT ?",
                (f"name : ({_match(words, 'OR')})", product_id, base[1], base[2], limit),
            ).fetchall()
        return [Product.model_validate_json(j) for (j,) in rows]

    def categories(self, parent_id: str | None) -> list[Category]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id, parent_id, name, path_json, product_count FROM categories"
                " WHERE parent_id IS ? ORDER BY rowid",
                (parent_id,),
            ).fetchall()
        return [
            Category(id=i, name=n, parent_id=pid, path=json.loads(path), product_count=cnt)
            for i, pid, n, path, cnt in rows
        ]

    def promotions(self, page: int, limit: int, sort: str) -> tuple[list[Product], int]:
        """Products whose old price is above the current one; best discount first by default."""
        order = _ORDER.get(sort, _ORDER["discount"])
        clause = "FROM products p WHERE p.old_price > p.price"
        with closing(self._connect()) as db:
            (total,) = db.execute(f"SELECT COUNT(*) {clause}").fetchone()
            rows = db.execute(
                f"SELECT p.json {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                (limit, (page - 1) * limit),
            ).fetchall()
        return [Product.model_validate_json(j) for (j,) in rows], total

    def stats(self) -> dict[str, Any]:
        """From `meta` alone: /healthz is public and must not cost a scan of the catalog.
        Feed counts are None when the index was built without them ("unknown", not zero)."""
        with closing(self._connect()) as db:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        offers, skipped = meta.get("feed_offers"), meta.get("feed_skipped")
        return {
            "products": int(meta["products"]),
            "categories": int(meta["categories"]),
            "feed_offers": int(offers) if offers else None,
            # A repeated offer id is skipped like any other invalid offer.
            "skipped": int(skipped) + int(meta["duplicates"]) if skipped else None,
            "built_at": float(meta["built_at"]),
            "feed_date": meta.get("feed_date") or None,
        }

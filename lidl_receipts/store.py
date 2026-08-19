"""SQLite storage.

The raw API payload is the source of truth: it is stored verbatim on every
receipt row, and the `items` / `item_discounts` tables are a derived cache that
`renormalize()` can rebuild at any time. That way a mistake in the field
mapping costs a re-parse, never a re-download.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from . import products
from .normalize import name_key, normalize_ticket

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id            TEXT PRIMARY KEY,
    date          TEXT,
    currency      TEXT,
    store_code    TEXT,
    store_name    TEXT,
    store_address TEXT,
    store_postal  TEXT,
    store_city    TEXT,
    total_cents   INTEGER,
    fetched_at    TEXT NOT NULL,
    raw_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    receipt_id       TEXT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL,
    name             TEXT,
    -- Lower-cased in Python, not by SQL: see normalize.name_key.
    name_key         TEXT NOT NULL DEFAULT '',
    quantity         REAL,
    unit_price_cents INTEGER,
    amount_cents     INTEGER,
    discount_cents   INTEGER NOT NULL DEFAULT 0,
    net_cents        INTEGER,
    is_weight        INTEGER NOT NULL DEFAULT 0,
    barcode          TEXT,
    tax_group        TEXT,
    deposit_cents    INTEGER,
    PRIMARY KEY (receipt_id, line_no)
);

CREATE TABLE IF NOT EXISTS item_discounts (
    receipt_id   TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    idx          INTEGER NOT NULL,
    description  TEXT,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (receipt_id, line_no, idx),
    FOREIGN KEY (receipt_id, line_no)
        REFERENCES items(receipt_id, line_no) ON DELETE CASCADE
);

-- Hand-maintained article categories. Deliberately a table of its own: the
-- items table is wiped and rebuilt by renormalize, and manual labelling must
-- not be collateral damage. Keyed on the lower-cased name, because the till
-- varies the casing of the same article ("Eiersalade surinaams" vs
-- "eiersalade surinaams").
CREATE TABLE IF NOT EXISTS categories (
    name_key TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

-- Derived grouping of the till's name/barcode variants into one product.
-- Rebuilt from the items, never edited by hand: see products.py.
CREATE TABLE IF NOT EXISTS products (
    name_key     TEXT PRIMARY KEY,
    product_key  TEXT NOT NULL,
    product_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_key ON products(product_key);
CREATE INDEX IF NOT EXISTS idx_receipts_date ON receipts(date);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_items_name_key ON items(name_key);
CREATE INDEX IF NOT EXISTS idx_items_barcode ON items(barcode);

-- Views are dropped and recreated on every open, so a changed definition in
-- this file actually reaches an existing database instead of silently keeping
-- the old one.
DROP VIEW IF EXISTS v_spend;
DROP VIEW IF EXISTS v_items;
DROP VIEW IF EXISTS v_receipts;

-- Convenience views: same data, amounts in euros.
CREATE VIEW v_receipts AS
SELECT id, date, currency, store_name, store_city,
       total_cents / 100.0 AS total
FROM receipts;

-- The analysis view: line items with their category and month attached.
CREATE VIEW v_spend AS
SELECT r.date, substr(r.date, 1, 7) AS month, r.store_name, i.name, i.name_key,
       COALESCE(c.category, 'ongecategoriseerd') AS category,
       COALESCE(p.product_name, i.name) AS product,
       COALESCE(p.product_key, 'name:' || i.name_key) AS product_key,
       i.quantity,
       i.unit_price_cents / 100.0 AS unit_price,
       i.amount_cents / 100.0     AS amount,
       i.discount_cents / 100.0   AS discount,
       i.net_cents / 100.0        AS net,
       i.is_weight, i.barcode, i.tax_group
FROM items i
JOIN receipts r ON r.id = i.receipt_id
LEFT JOIN categories c ON c.name_key = i.name_key
LEFT JOIN products p   ON p.name_key = i.name_key;

CREATE VIEW v_items AS
SELECT i.receipt_id, r.date, r.store_name, i.line_no, i.name, i.quantity,
       i.unit_price_cents / 100.0 AS unit_price,
       i.amount_cents / 100.0     AS amount,
       i.discount_cents / 100.0   AS discount,
       i.net_cents / 100.0        AS net,
       i.is_weight, i.barcode, i.tax_group
FROM items i
JOIN receipts r ON r.id = i.receipt_id;
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self.conn = sqlite3.connect(self.path)
        if not existed:
            # Every receipt you ever had is in here; it should be no more
            # readable than the config holding the token.
            with contextlib.suppress(OSError):
                self.path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        columns = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(items)")
        }
        if columns and "name_key" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN name_key TEXT NOT NULL DEFAULT ''")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def known_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT id FROM receipts")
        return {row["id"] for row in rows}

    def save(self, raw: dict) -> dict:
        """Store a raw ticket payload plus its normalized projection."""
        normalized = normalize_ticket(raw)
        receipt_id = normalized["id"] or str(raw.get("id") or "")
        if not receipt_id:
            raise ValueError("ticket payload has no id")

        store = normalized["store"]
        self.conn.execute(
            """
            INSERT INTO receipts (id, date, currency, store_code, store_name,
                                  store_address, store_postal, store_city,
                                  total_cents, fetched_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                date=excluded.date, currency=excluded.currency,
                store_code=excluded.store_code, store_name=excluded.store_name,
                store_address=excluded.store_address,
                store_postal=excluded.store_postal,
                store_city=excluded.store_city,
                total_cents=excluded.total_cents,
                fetched_at=excluded.fetched_at, raw_json=excluded.raw_json
            """,
            (
                receipt_id,
                normalized["date"],
                normalized["currency"],
                store["code"],
                store["name"],
                store["address"],
                store["postal_code"],
                store["locality"],
                normalized["total_cents"],
                datetime.now(tz=UTC).isoformat(),
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        self._write_items(receipt_id, normalized["items"])
        self.conn.commit()
        return normalized

    def _write_items(self, receipt_id: str, items: list[dict]) -> None:
        self.conn.execute(
            "DELETE FROM items WHERE receipt_id = ?", (receipt_id,)
        )
        self.conn.execute(
            "DELETE FROM item_discounts WHERE receipt_id = ?", (receipt_id,)
        )
        for item in items:
            self.conn.execute(
                """
                INSERT INTO items (receipt_id, line_no, name, name_key,
                                   quantity, unit_price_cents, amount_cents,
                                   discount_cents, net_cents, is_weight,
                                   barcode, tax_group, deposit_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    item["line_no"],
                    item["name"],
                    name_key(item["name"] or ""),
                    item["quantity"],
                    item["unit_price_cents"],
                    item["amount_cents"],
                    item["discount_cents"],
                    item["net_cents"],
                    int(item["is_weight"]),
                    item["barcode"],
                    item["tax_group"],
                    item["deposit_cents"],
                ),
            )
            for idx, discount in enumerate(item["discounts"]):
                self.conn.execute(
                    """
                    INSERT INTO item_discounts (receipt_id, line_no, idx,
                                                description, amount_cents)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        item["line_no"],
                        idx,
                        discount["description"],
                        discount["amount_cents"],
                    ),
                )

    def raw_payloads(self) -> Iterator[tuple[str, dict]]:
        rows = self.conn.execute(
            "SELECT id, raw_json FROM receipts ORDER BY date"
        )
        for row in rows:
            yield row["id"], json.loads(row["raw_json"])

    def renormalize(self) -> tuple[int, list[str]]:
        """Rebuild the derived tables from stored raw payloads."""
        count = 0
        warnings: list[str] = []
        for receipt_id, raw in list(self.raw_payloads()):
            normalized = normalize_ticket(raw)
            store = normalized["store"]
            self.conn.execute(
                """
                UPDATE receipts SET date=?, currency=?, store_code=?,
                    store_name=?, store_address=?, store_postal=?,
                    store_city=?, total_cents=?
                WHERE id=?
                """,
                (
                    normalized["date"],
                    normalized["currency"],
                    store["code"],
                    store["name"],
                    store["address"],
                    store["postal_code"],
                    store["locality"],
                    normalized["total_cents"],
                    receipt_id,
                ),
            )
            self._write_items(receipt_id, normalized["items"])
            for warning in normalized["warnings"]:
                warnings.append(f"{receipt_id}: {warning}")
            count += 1
        products.build(self.conn)
        self.conn.commit()
        return count, warnings

    def stats(self) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS receipts,
                   MIN(date) AS first_date,
                   MAX(date) AS last_date,
                   COALESCE(SUM(total_cents), 0) AS total_cents
            FROM receipts
            """
        ).fetchone()
        items = self.conn.execute(
            "SELECT COUNT(*) AS n FROM items"
        ).fetchone()["n"]
        return {
            "receipts": row["receipts"],
            "items": items,
            "first_date": row["first_date"],
            "last_date": row["last_date"],
            "total_cents": row["total_cents"],
        }

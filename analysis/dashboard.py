#!/usr/bin/env python3
"""Build the local analysis dashboard: one self-contained HTML file.

    python3 analysis/dashboard.py && xdg-open analysis/dashboard.html

Everything ships inside the file: data, charts and filtering. It opens straight
off disk with file://, with no server, build step or dependencies.

The payload carries the raw purchase lines rather than pre-aggregated totals.
It is the finest grain the receipts have, every panel derives from it, and
scanning seventeen thousand rows in the browser costs a millisecond or two.
Shipping rollups instead would mean shipping one per question, and then
answering a new question means regenerating the file.

Lines are arrays, not objects: written as objects the same six key names
repeat seventeen thousand times and the file triples for nothing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else "data/receipts.db")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "analysis/dashboard.html")

DEPOSIT = "statiegeld"


def collect(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row

    categories = [
        r["category"]
        for r in conn.execute(
            "SELECT DISTINCT category FROM v_spend ORDER BY category"
        )
    ]
    cat_index = {c: i for i, c in enumerate(categories)}

    months = [
        r["m"]
        for r in conn.execute(
            "SELECT DISTINCT substr(date,1,7) m FROM receipts ORDER BY m"
        )
    ]
    month_index = {m: i for i, m in enumerate(months)}

    stores = [
        r["store_name"]
        for r in conn.execute(
            "SELECT store_name, SUM(total_cents) t FROM receipts"
            " GROUP BY store_name ORDER BY t DESC"
        )
    ]
    store_index = {s: i for i, s in enumerate(stores)}

    # Products, with the span over which they were bought. The first and last
    # month is what makes "started buying" and "stopped buying" answerable.
    # The dominant category per product in one pass. As a correlated subquery
    # this ran once per product and took a minute and a half on its own.
    dominant: dict[str, str] = {}
    for row in conn.execute(
        """
        SELECT product_key, category, SUM(net) AS spend
        FROM v_spend GROUP BY product_key, category
        ORDER BY product_key, spend ASC
        """
    ):
        dominant[row["product_key"]] = row["category"]

    products: list[list] = []
    product_index: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT product_key, MAX(product) AS name,
               COUNT(DISTINCT name) AS variants,
               COUNT(DISTINCT NULLIF(barcode, '')) AS barcodes,
               MIN(substr(date, 1, 7)) AS first_month,
               MAX(substr(date, 1, 7)) AS last_month,
               MAX(is_weight) AS weighed
        FROM v_spend
        GROUP BY product_key
        ORDER BY SUM(net) DESC
        """
    ):
        product_index[row["product_key"]] = len(products)
        products.append([
            row["name"], cat_index[dominant[row["product_key"]]], row["variants"],
            row["barcodes"], month_index[row["first_month"]],
            month_index[row["last_month"]],
            # Weighed articles carry kilos in the quantity column, so the
            # dashboard must not print them as a count of items.
            1 if row["weighed"] else 0,
        ])

    # Receipts carry a real timestamp, which is what makes the weekday and
    # hour patterns possible at all.
    receipts: list[list] = []
    receipt_index: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT r.id, r.date, r.store_name, r.total_cents,
               (SELECT COUNT(*) FROM items i WHERE i.receipt_id = r.id) AS lines
        FROM receipts r ORDER BY r.date
        """
    ):
        receipt_index[row["id"]] = len(receipts)
        date = row["date"]
        receipts.append([
            date[:10],
            month_index[date[:7]],
            weekday(date[:10]),
            int(date[11:13] or 0),
            store_index[row["store_name"]],
            row["total_cents"],
            row["lines"],
        ])

    # One lookup up front: resolving the product per line with its own query
    # turns this into seventeen thousand round trips.
    to_product = {
        r["name_key"]: r["product_key"]
        for r in conn.execute("SELECT name_key, product_key FROM products")
    }

    # A product groups the till's renames, which is what you want for "how
    # much did I spend on cucumber". It is the wrong unit for "what did
    # cucumber cost": one name covers several pack sizes, and broccoli is sold
    # per piece under one article id and per kilo under another. Comparing
    # across them measures the packaging, not the price. Variants keep those
    # apart, so every price series is drawn per article id.
    variants: list[list] = []
    variant_index: dict[tuple[int, str], int] = {}

    lines = []
    for row in conn.execute(
        """
        SELECT receipt_id, name_key, quantity, net_cents, discount_cents,
               unit_price_cents, tax_group, barcode, is_weight
        FROM items ORDER BY receipt_id, line_no
        """
    ):
        pk = to_product.get(row["name_key"], f"name:{row['name_key']}")
        if pk not in product_index:
            continue
        pi = product_index[pk]
        vkey = (pi, row["barcode"] or "")
        if vkey not in variant_index:
            variant_index[vkey] = len(variants)
            variants.append([row["barcode"] or "", pi,
                             1 if row["is_weight"] else 0])
        lines.append([
            receipt_index[row["receipt_id"]],
            pi,
            round(row["quantity"] or 0, 3),
            row["net_cents"] or 0,
            row["discount_cents"] or 0,
            row["unit_price_cents"] or 0,
            1 if row["tax_group"] == "C" else 0,
            variant_index[vkey],
        ])

    return {
        "categories": categories,
        "months": months,
        "stores": stores,
        "products": products,
        "receipts": receipts,
        "variants": variants,
        "lines": lines,
        "depositCat": cat_index.get(DEPOSIT, -1),
    }


def weekday(iso_date: str) -> int:
    """0 = Monday, matching how a shopping week is read."""
    from datetime import date

    y, m, d = (int(p) for p in iso_date.split("-"))
    return date(y, m, d).weekday()


def main() -> None:
    conn = sqlite3.connect(DB)
    data = collect(conn)
    conn.close()

    template = (Path(__file__).parent / "dashboard.tpl.html").read_text(
        encoding="utf-8"
    )
    html = template.replace(
        "/*__DATA__*/null",
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    )
    OUT.write_text(html, encoding="utf-8")
    print(
        f"{OUT}: {OUT.stat().st_size / 1024:.0f} kB · "
        f"{len(data['lines'])} regels · {len(data['receipts'])} bonnen · "
        f"{len(data['products'])} producten · {len(data['months'])} maanden"
    )


if __name__ == "__main__":
    main()

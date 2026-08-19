#!/usr/bin/env python3
"""Compute the spending analysis from the receipt database.

Writes a JSON blob to stdout with everything the report needs, so the
numbers stay reproducible and the presentation layer holds no logic.

    python3 analysis/spending.py > analysis/spending.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else "data/receipts.db")

# Deposit is money going round in circles, because bottles are paid for and
# brought back. It is excluded from spending totals rather than counted as
# groceries. It is reported separately instead.
DEPOSIT = "statiegeld"


def fetch(conn: sqlite3.Connection, query: str, *params) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(query, params)]


def main() -> None:
    conn = sqlite3.connect(DB)
    out: dict[str, object] = {}

    out["meta"] = fetch(
        conn,
        """
        SELECT COUNT(*) AS receipts,
               MIN(substr(date, 1, 10)) AS first_date,
               MAX(substr(date, 1, 10)) AS last_date,
               ROUND(SUM(total), 2) AS total
        FROM v_receipts
        """,
    )[0]

    out["deposit"] = fetch(
        conn,
        """
        SELECT ROUND(SUM(CASE WHEN net > 0 THEN net ELSE 0 END), 2) AS paid,
               ROUND(SUM(CASE WHEN net < 0 THEN -net ELSE 0 END), 2) AS returned,
               ROUND(SUM(net), 2) AS net
        FROM v_spend WHERE category = ?
        """,
        DEPOSIT,
    )[0]

    out["by_month"] = fetch(
        conn,
        """
        SELECT month,
               COUNT(DISTINCT date) AS trips,
               ROUND(SUM(net), 2)   AS spend
        FROM v_spend WHERE category <> ?
        GROUP BY month ORDER BY month
        """,
        DEPOSIT,
    )

    out["by_category"] = fetch(
        conn,
        """
        SELECT category,
               COUNT(*)           AS lines,
               ROUND(SUM(net), 2) AS spend
        FROM v_spend WHERE category <> ?
        GROUP BY category ORDER BY SUM(net) DESC
        """,
        DEPOSIT,
    )

    out["category_by_month"] = fetch(
        conn,
        """
        SELECT month, category, ROUND(SUM(net), 2) AS spend
        FROM v_spend WHERE category <> ?
        GROUP BY month, category ORDER BY month, SUM(net) DESC
        """,
        DEPOSIT,
    )

    # Grouped per product, not per name: the till spells one article several
    # ways and reissues its barcode, so ranking by name splits a product into
    # pieces that each look smaller than they are. See products.py.
    out["top_articles"] = fetch(
        conn,
        """
        SELECT product AS name, category, COUNT(*) AS times,
               COUNT(DISTINCT name) AS variants,
               ROUND(SUM(net), 2) AS spend
        FROM v_spend WHERE category <> ? AND net > 0
        GROUP BY product_key ORDER BY SUM(net) DESC LIMIT 25
        """,
        DEPOSIT,
    )

    # Discounts: what the Lidl Plus coupons and price cuts actually return.
    out["discounts_by_month"] = fetch(
        conn,
        """
        SELECT month,
               ROUND(SUM(discount), 2) AS discount,
               ROUND(SUM(amount), 2)   AS gross
        FROM v_spend GROUP BY month ORDER BY month
        """,
    )

    out["top_discounted"] = fetch(
        conn,
        """
        SELECT name, COUNT(*) AS times, ROUND(SUM(discount), 2) AS discount
        FROM v_spend WHERE discount > 0
        GROUP BY lower(trim(name)) ORDER BY SUM(discount) DESC LIMIT 15
        """,
    )

    # Non-food: the 21% VAT group, where the outliers tend to hide.
    out["non_food"] = fetch(
        conn,
        """
        SELECT name, substr(date, 1, 10) AS date, ROUND(net, 2) AS net
        FROM v_spend
        WHERE tax_group = 'C' AND net > 0
        ORDER BY net DESC LIMIT 20
        """,
    )

    out["non_food_by_month"] = fetch(
        conn,
        """
        SELECT month,
               ROUND(SUM(CASE WHEN tax_group = 'C' THEN net ELSE 0 END), 2) AS non_food,
               ROUND(SUM(CASE WHEN tax_group <> 'C' THEN net ELSE 0 END), 2) AS food
        FROM v_spend WHERE category <> ?
        GROUP BY month ORDER BY month
        """,
        DEPOSIT,
    )

    # Personal inflation: for articles bought in more than one month, compare
    # the unit price when first seen with the unit price when last seen, and
    # weight each article by what it costs in the basket. Articles bought only
    # once say nothing about price movement and are left out.
    #
    # Grouped by barcode, not by name. The same name covers several pack sizes
    # ("Uien geel" is a loose price per kilo as well as a bagged one), and
    # comparing across them measures the packaging, not the price. Each pack
    # size carries its own article id.
    prices = fetch(
        conn,
        """
        SELECT barcode AS key, MAX(name) AS name, month,
               AVG(unit_price)   AS unit_price,
               SUM(net)          AS spend
        FROM v_spend
        WHERE category <> ? AND unit_price > 0 AND barcode <> ''
        GROUP BY barcode, month ORDER BY barcode, month
        """,
        DEPOSIT,
    )

    per_article: dict[str, list[dict]] = {}
    for row in prices:
        per_article.setdefault(row["key"], []).append(row)

    movers, weighted_sum, weight_total = [], 0.0, 0.0
    for rows in per_article.values():
        if len(rows) < 2:
            continue
        first, last = rows[0], rows[-1]
        if not first["unit_price"]:
            continue
        change = (last["unit_price"] - first["unit_price"]) / first["unit_price"]
        weight = sum(r["spend"] for r in rows)
        weighted_sum += change * weight
        weight_total += weight
        movers.append(
            {
                "name": last["name"],
                "first_month": first["month"],
                "last_month": last["month"],
                "first_price": round(first["unit_price"], 2),
                "last_price": round(last["unit_price"], 2),
                "change_pct": round(change * 100, 1),
                "spend": round(weight, 2),
            }
        )

    movers.sort(key=lambda m: m["spend"], reverse=True)
    out["inflation"] = {
        "articles": len(movers),
        "weighted_change_pct": (
            round(weighted_sum / weight_total * 100, 2) if weight_total else None
        ),
        "risers": sorted(
            [m for m in movers if m["change_pct"] > 0],
            key=lambda m: m["change_pct"], reverse=True,
        )[:12],
        "fallers": sorted(
            [m for m in movers if m["change_pct"] < 0],
            key=lambda m: m["change_pct"],
        )[:12],
        "biggest_basket": movers[:15],
    }

    out["uncategorised"] = fetch(
        conn,
        """
        SELECT ROUND(SUM(net), 2) AS spend, COUNT(*) AS lines
        FROM v_spend WHERE category IN ('overig voeding', 'ongecategoriseerd')
        """,
    )[0]

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    conn.close()


if __name__ == "__main__":
    main()

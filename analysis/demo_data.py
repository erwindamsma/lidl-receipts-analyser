#!/usr/bin/env python3
"""Build a receipts database full of invented purchases.

The dashboard is the point of this project and cannot be shown without
somebody's groceries in it. This writes a plausible history using real Dutch
Lidl article names from the shared category dataset and entirely made-up
purchases. The screenshot in the README, and anyone evaluating the project,
needs no account and exposes nobody's shopping.

    python3 analysis/demo_data.py data/demo.db
    python3 analysis/dashboard.py data/demo.db analysis/demo.html

The seed is fixed, so the same database comes out every run and the screenshot
can be regenerated rather than trusted.

One thing the demo cannot do is `lidl renormalize`: these receipts have no real
API payload to reparse, so rebuilding the derived tables would empty them.
Delete the file and run this again instead.
"""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lidl_receipts import products  # noqa: E402
from lidl_receipts.normalize import name_key  # noqa: E402
from lidl_receipts.store import Store  # noqa: E402

SEED = 20260819
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "data/demo.db")
SEED_CSV = Path(__file__).resolve().parent.parent / "categories.seed.csv"

# Invented stores, so nothing points at a real shop anyone visits.
STORES = [
    ("NL9001", "Lidl Demostad Centrum", "Demostad", 0.55),
    ("NL9002", "Lidl Demostad Noord", "Demostad", 0.30),
    ("NL9003", "Lidl Voorbeelddorp", "Voorbeelddorp", 0.15),
]

# 21% VAT categories: the till marks these C, everything edible B. The
# category cross-check in `lidl categories` keys on exactly this.
HIGH_VAT = {"huishouden", "drogisterij", "non-food", "alcohol"}
WEIGHED = {"groente & fruit"}
DEPOSIT_CATS = {"frisdrank & sap", "alcohol"}
DEPOSIT_NAME = "Statiegeld"

START, END = date(2023, 1, 2), date(2026, 8, 1)
# Roughly a fifth of a household's articles are staples bought over and over;
# without that skew every product looks equally rare and the "top products"
# panels say nothing.
STAPLE_SHARE = 0.2


def load_articles(rng: random.Random) -> list[dict]:
    rows = list(csv.DictReader(SEED_CSV.open(encoding="utf-8")))
    rng.shuffle(rows)
    articles = []
    for i, row in enumerate(rows):
        category = row["category"]
        if category == "statiegeld":
            continue
        base = rng.uniform(2.5, 9.0) if category in {"vlees & vis", "alcohol"} \
            else rng.uniform(0.45, 4.5)
        articles.append({
            "name": row["name"],
            "category": category,
            "barcode": f"{4_000_000 + i * 7:08d}",
            "price": round(base, 2),
            "weight": rng.random() < 0.5 and category in WEIGHED,
            "weight_pick": 1.0 if i < len(rows) * STAPLE_SHARE else 0.15,
            # A renumbering or a rename partway through is what the product
            # grouping exists for, so the demo has to contain some.
            "renumber": rng.random() < 0.12,
            "rename": rng.random() < 0.08,
            "switch": START + timedelta(days=rng.randint(200, 900)),
        })
    return articles


def variant(article: dict, when: date) -> tuple[str, str]:
    """The name and article number the till would print on this date."""
    name, barcode = article["name"], article["barcode"]
    if when >= article["switch"]:
        if article["rename"]:
            name = name.upper()[:20]
        if article["renumber"]:
            barcode = f"9{article['barcode'][1:]}"
    return name, barcode


def main() -> None:
    rng = random.Random(SEED)
    articles = load_articles(rng)
    weights = [a["weight_pick"] for a in articles]

    OUT.unlink(missing_ok=True)
    store = Store(OUT)
    conn = store.conn

    receipts, items = [], []
    day = START
    while day <= END:
        # Shopping clusters around the end of the week, which is what makes
        # the weekday panels show a pattern instead of noise.
        chance = (0.22, 0.20, 0.26, 0.30, 0.42, 0.38, 0.05)[day.weekday()]
        if rng.random() < chance:
            receipts.append(build_receipt(rng, articles, weights, day, items))
        day += timedelta(days=1)

    conn.executemany(
        "INSERT INTO receipts (id, date, currency, store_code, store_name,"
        " store_address, store_postal, store_city, total_cents, fetched_at,"
        " raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        receipts,
    )
    conn.executemany(
        "INSERT INTO items (receipt_id, line_no, name, name_key, quantity,"
        " unit_price_cents, amount_cents, discount_cents, net_cents,"
        " is_weight, barcode, tax_group, deposit_cents)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        items,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO categories (name_key, category) VALUES (?,?)",
        [(name_key(a["name"]), a["category"]) for a in articles]
        + [(name_key(variant(a, END)[0]), a["category"]) for a in articles]
        + [(name_key(DEPOSIT_NAME), "statiegeld")],
    )
    products.build(conn)
    conn.commit()
    store.close()
    print(f"{OUT}: {len(receipts)} bonnen, {len(items)} regels, "
          f"{START} t/m {END}")


def build_receipt(rng, articles, weights, day, items) -> tuple:
    receipt_id = f"demo-{day:%Y%m%d}-{rng.randrange(16**6):06x}"
    hour = rng.choice((9, 10, 11, 12, 14, 15, 16, 17, 17, 18, 18, 19, 20))
    code, name, city, _ = rng.choices(
        STORES, weights=[s[3] for s in STORES]
    )[0]
    # Prices drift upward over the years; without it the price panels are flat.
    inflation = 1 + 0.035 * ((day - START).days / 365.25)

    chosen = rng.choices(articles, weights=weights, k=rng.randint(6, 24))
    total, line_no = 0, 0
    for article in dict.fromkeys(a["name"] for a in chosen):
        article = next(a for a in articles if a["name"] == article)
        art_name, barcode = variant(article, day)
        unit = max(5, round(article["price"] * inflation
                            * rng.uniform(0.94, 1.06) * 100))
        if article["weight"]:
            quantity = round(rng.uniform(0.2, 1.4), 3)
        else:
            quantity = rng.choice((1, 1, 1, 1, 2, 2, 3))
        amount = round(unit * quantity)
        # Lidl Plus discounts land on a minority of lines.
        discount = round(amount * rng.choice((0.2, 0.25, 0.35))) \
            if rng.random() < 0.09 else 0
        line_no += 1
        items.append((
            receipt_id, line_no, art_name, name_key(art_name), quantity,
            unit, amount, discount, amount - discount,
            int(bool(article["weight"])), barcode,
            "C" if article["category"] in HIGH_VAT else "B", None,
        ))
        total += amount - discount

        if article["category"] in DEPOSIT_CATS and rng.random() < 0.5:
            deposit = 25 * rng.choice((1, 4, 6))
            line_no += 1
            items.append((
                receipt_id, line_no, DEPOSIT_NAME, name_key(DEPOSIT_NAME),
                1, deposit, deposit, 0, deposit, 0, "", "A", deposit,
            ))
            total += deposit

    return (
        receipt_id, f"{day:%Y-%m-%d}T{hour:02d}:{rng.randint(0,59):02d}:00",
        "EUR", code, name, "Voorbeeldstraat 1", "1234 AB", city, total,
        f"{day:%Y-%m-%d}T00:00:00+00:00", json.dumps({"demo": True}),
    )


if __name__ == "__main__":
    main()

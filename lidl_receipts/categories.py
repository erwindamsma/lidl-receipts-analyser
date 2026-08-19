"""Article categories: proposals, and the CSV that holds your corrections.

Lidl prints till names only ("Halfvol.weidemelk h."), so spending categories
have to come from somewhere else. The rules below propose one per article;
`categories.csv` is where you overrule them, and that file rather than this
module
is the source of truth. Rerunning the command never overwrites a category that
is already filled in.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from .normalize import name_key

# First match wins, so keep specific patterns above general ones. See
# _INFLECTION below for how a keyword is matched against a name.
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("statiegeld", ("statiegeld", "emballage", "st.geld", "losse fles", "krat")),
    ("kleding", (
        "t-shirt", "shirt", "sweatbroek", "jegging", "sokken", "broek",
        "trui", "jas", "pyjama", "ondergoed", "legging",
    )),
    # Durable goods, before huishouden: the split is consumables (there)
    # against things you buy once and keep (here).
    ("non-food", (
        "stekkerdoos", "lamp", "koolmonoxide", "rookmelder", "ehbo",
        "cactus", "vetplant", "kamerplant", "stickerboek", "speelgoed",
        "broodrooster", "waterkoker", "staafmixer",
    )),
    ("huishouden", (
        "toiletpapier", "keukenpapier", "vaatwas", "wasmiddel", "wasverzachter",
        "afwas", "schoonmaak", "allesreiniger", "vuilniszak", "aluminiumfolie",
        "vershoudfolie", "batterij", "kaars", "servet", "poetspapier",
        "keukenrol", "reinigingsdoek", "schoonmaakdoek",
    )),
    ("drogisterij", (
        "tandpasta", "tandenborstel", "shampoo", "zeep", "douche", "deodorant",
        "paracetamol", "keelpastilles", "pleister", "scheer", "creme",
        "zonnebrand", "luier", "maandverband", "vitamine",
    )),
    # Alcoholvrije varianten vóór de bierregel: "Pils 0,0%" bevat "pils",
    # maar er zit geen alcohol in en telt daarom als frisdrank.
    ("frisdrank & sap", ("0,0%", "0.0%", "alcoholvrij", "alcoholarm")),
    ("alcohol", (
        "wijn", "sauvig", "chardonnay", "merlot", "rioja", "prosecco", "bier",
        "pils", "speciaalbier", " w za", "rose", "cava", "fris en fruitig",
        "liq.", "likeur",
    )),
    ("koffie & thee", ("koffie", "thee", "cappuccino", "espresso")),
    ("frisdrank & sap", (
        "siroop", "water", "sap", "cola", "frisdrank", "limonade", "ijsthee",
        "ice coffee", "energy",
    )),
    # Above dairy on purpose: pindakaas and eiersalade are spreads, and a
    # rule for "kaas" or "eier" would otherwise claim them first.
    ("broodbeleg", (
        "pindakaas", "spread", "speculoospasta", "hagelslag",
        "jam", "honing", "salade", "smeerkaas", "leverworst", "chocopasta",
        "hazelnootpasta", "notenpasta", "eiersalade", "hummus", "pastei",
        "tapenade", "dip",
    )),
    ("zuivel & eieren", (
        "melk", "kaas", "kwark", "yoghurt", "room", "boter", "gouda", "gouds",
        "grana", "mozzarella", "brie", "vla", "eieren", "eier", "scharrelei",
        "mascarpone", "feta", "camembert", "kefir", "pudding", "babybel",
        "kipster",
    )),
    ("vlees & vis", (
        "gehakt", "spek", "kip", "zalm", "ham", "worst", "filet", "sate",
        "saté", "rundvlees", "varkens", "schnitzel", "burger", "tonijn",
        "garnalen", "vis", "bacon", "salami", "rookvlees", "shoarma",
    )),
    ("groente & fruit", (
        "banaan", "bananen", "appel", "peer", "peren", "komkommer", "broccoli",
        "spinazie", "uien", "ui ", "paprika", "tomaat", "tomaten", "druiven",
        "mango", "aardbei", "sla", "sperziebonen", "wortel", "avocado",
        "citroen", "meloen", "sinaasappel", "courgette", "bloemkool",
        "champignon", "prei", "aardappel", "mandarijn", "kiwi", "rauwkost",
        "groente", "fruit", "kool", "boon", "erwt", "asperge", "radijs",
        "waspeen", "peen", "ananas", "bes", "pruim", "perzik", "nectarine",
    )),
    ("brood & bakkerij", (
        "brood", "baguette", "bollen", "bol ", "stokbrood", "croissant",
        "beschuit", "cracker", "knackebrod", "pistolet", "wraps", "tortilla",
        "pannenkoek", "poffertjes",
    )),
    ("maaltijden & gemak", (
        "lasagne", "bami", "nasi", "kroketten", "frikandel", "pizza", "wok",
        "maaltijd", "soep", "quiche", "loempia", "sushi", "curry", "gewokt",
    )),
    ("snoep & snacks", (
        "chocolade", "drop", "pinda", "noten", "cashew", "chips", "koek",
        "ijs", "waterijs", "tiramisu", "snoep", "reep", "wafel", "bonbon",
        "popcorn", "zoutjes", "borrel", "gebak", "taart", "muffin", "choco",
        "donut", "mochi", "ice pop", "snack", "stroopwafel", "speculaas",
    )),
    ("voorraadkast", (
        "bloem", "suiker", "puree", "olie", "azijn", "kruid", "zout", "peper",
        "rijst", "spaghetti", "macaroni", "meel", "gist", "bouillon", "saus",
        "ketchup", "mayonaise", "mosterd", "muesli", "ontbijtgranen", "cornflakes",
        "havermout", "conserven", "blik", "pot ", "margarine", "bakmix",
        "bladerdeeg", "rozijn",
    )),
]

# When no rule matches, the VAT group still says something: 21% is almost
# never groceries at Lidl, and 0% is deposit.
TAX_GROUP_FALLBACK = {
    "A": "statiegeld",
    "C": "non-food",
    "B": "overig voeding",
}

CSV_COLUMNS = ("name", "category", "spend", "times")

# A starter mapping shipped with the project, so a new user does not have to
# categorise two thousand till names from scratch. It holds names and
# categories only (no amounts, no purchase counts) and covers the recurring
# Lidl assortment rather than one-off promotions, which are the entries least
# likely to reappear on someone else's receipts.
SEED_PATH = Path(__file__).resolve().parent.parent / "categories.seed.csv"

# The two buckets an article lands in when nothing classified it. They are
# answers of last resort, so they never override a category that was actually
# decided.
FALLBACK_CATEGORIES = frozenset({"overig voeding", "ongecategoriseerd"})


# A keyword counts when it lines up with a word on at least one side:
#
#   at the start   "koffie" in "koffiebonen"    -- compound modifier
#   at the end     "melk"  in "weidemelk"       -- compound head noun
#                  "worst" in "knakworsten"     -- head noun plus inflection
#
# and nowhere else. Purely mid-word hits are what a substring test gets wrong,
# and they are not rare: "sap" sits inside "lasapparaat", "cola" inside
# "chocolade", "peer" inside "decoupeerzaag". A minimum keyword length does not
# separate the two cases, since "melk" and "cola" are both four letters, so the
# rule is positional rather than about length.
_INFLECTION = r"(?:s|en|je|jes|tje|tjes)?"


@lru_cache(maxsize=1)
def _compiled_rules() -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for category, keywords in RULES:
        alternation = "|".join(
            re.escape(keyword.strip()) for keyword in keywords
        )
        # Each alternative carries its own group: | binds loosest, so an
        # ungrouped lookaround would apply to the first keyword only.
        pattern = (
            rf"(?<![a-z0-9])(?:{alternation})"
            rf"|(?:{alternation}){_INFLECTION}(?![a-z0-9])"
        )
        compiled.append((category, re.compile(pattern, re.IGNORECASE)))
    return compiled


def propose(name: str, tax_group: str = "") -> str:
    """Suggest a category for an article name."""
    for category, pattern in _compiled_rules():
        if pattern.search(name.lower()):
            return category
    return TAX_GROUP_FALLBACK.get(tax_group.upper(), "ongecategoriseerd")


def read_csv(path: Path) -> dict[str, str]:
    """Return {name_key: category} for the rows that have a category."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            name_key(row["name"]): row["category"].strip()
            for row in csv.DictReader(handle)
            if row.get("name") and row.get("category", "").strip()
        }


def read_seed() -> dict[str, str]:
    """The shipped starter mapping, keyed like the user's own file."""
    if not SEED_PATH.exists():
        return {}
    with SEED_PATH.open(newline="", encoding="utf-8") as handle:
        return {
            name_key(row["name"]): row["category"].strip()
            for row in csv.DictReader(handle)
            if row.get("name") and row.get("category", "").strip()
        }


def refresh_csv(conn: sqlite3.Connection, path: Path) -> tuple[int, int, int]:
    """Rewrite the CSV: keep existing categories, seed or propose the rest.

    Rows are ordered by spend so the articles worth labelling carefully sit at
    the top. Returns (total articles, taken from the seed, proposed by rule).
    """
    existing = read_csv(path)
    seed = read_seed()
    rows = conn.execute(
        """
        SELECT name,
               MAX(tax_group)               AS tax_group,
               ROUND(SUM(net_cents) / 100.0, 2) AS spend,
               COUNT(*)                     AS times
        FROM items
        GROUP BY name_key
        ORDER BY SUM(net_cents) DESC
        """
    ).fetchall()

    if not rows:
        # Nothing to write against. Truncating here would replace a hand-made
        # mapping with a header line. A fresh clone, an empty database or a
        # sync that has not run yet must not cost the user their work.
        return len(existing), 0, 0

    seeded = proposed = 0
    out: list[dict[str, object]] = []
    for row in rows:
        key = name_key(row["name"])
        category = existing.get(key)
        if not category:
            # Your own decision first, then the shared mapping, then the rules.
            category = seed.get(key)
            if category:
                seeded += 1
            else:
                category = propose(row["name"], row["tax_group"] or "")
                proposed += 1
        out.append(
            {
                "name": row["name"],
                "category": category,
                "spend": f"{row['spend'] or 0:.2f}",
                "times": row["times"],
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(out)
    return len(out), seeded, proposed


# Categories that a 21%-VAT article can legitimately belong to. Everything
# else at that rate is a miscategorisation: Lidl charges 9% on groceries, so a
# "food" article at 21% is really drink, drugstore or hardware. This is what
# caught 439 euro of wine sitting under soft drinks. The till calls it
# "Fris en fruitig wit Z-A", which reads like lemonade until you notice the
# VAT group.
HIGH_VAT_OK = frozenset(
    {"non-food", "alcohol", "huishouden", "drogisterij", "kleding", "statiegeld"}
)


def high_vat_mismatches(
    conn: sqlite3.Connection, *, min_spend: float = 1.0
) -> list[sqlite3.Row]:
    """Articles taxed at 21% but filed under a food category."""
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in HIGH_VAT_OK)
    return conn.execute(
        f"""
        SELECT name, category, ROUND(SUM(net), 2) AS spend, COUNT(*) AS times
        FROM v_spend
        WHERE tax_group = 'C' AND category NOT IN ({placeholders})
        GROUP BY lower(trim(name))
        HAVING spend >= ?
        ORDER BY spend DESC
        """,
        (*sorted(HIGH_VAT_OK), min_spend),
    ).fetchall()


def category_conflicts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One product filed under more than one category.

    The till renames the same product freely ("Creme Fraiche 30%" one week,
    "Crème fraÎche 30%" the next) and each spelling gets categorised on its
    own. Grouping is per product rather than per barcode, because a product
    also changes article number over the years (see products.py): both kinds
    of drift split one product into rows that then drift apart in category
    too.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT product_key,
               COUNT(DISTINCT category) AS categories,
               ROUND(SUM(net), 2)       AS spend
        FROM v_spend
        GROUP BY product_key
        HAVING categories > 1
        ORDER BY spend DESC
        """
    ).fetchall()


def resolve_category_conflicts(conn: sqlite3.Connection, path: Path) -> list[tuple]:
    """Give every name of a product the category of its biggest earner.

    Returns the (name, from, to) changes, and writes them into the CSV so the
    correction survives the next refresh.
    """
    conn.row_factory = sqlite3.Row
    # Ascending spend, so the last row written per barcode is its biggest
    # earner, so that variant's category is the one the others adopt. A
    # fallback category never wins from a real one, whatever it earns: the
    # variant that got classified is more informative than the one that fell
    # through, and letting spend decide would drag articles back into the
    # bucket the whole review was meant to empty.
    winners: dict[str, str] = {}
    for row in conn.execute(
        """
        SELECT product_key, category, SUM(net) AS spend
        FROM v_spend
        GROUP BY product_key, category
        ORDER BY product_key, spend ASC
        """
    ):
        current = winners.get(row["product_key"])
        if current and current not in FALLBACK_CATEGORIES:
            if row["category"] in FALLBACK_CATEGORIES:
                continue
        winners[row["product_key"]] = row["category"]

    wanted: dict[str, str] = {}
    for row in conn.execute("SELECT DISTINCT name, product_key FROM v_spend"):
        target = winners.get(row["product_key"])
        if target:
            wanted[name_key(row["name"])] = target

    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    changes = []
    for entry in rows:
        target = wanted.get(name_key(entry["name"]))
        if target and entry["category"] != target:
            changes.append((entry["name"], entry["category"], target))
            entry["category"] = target

    if changes:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        # Reload here rather than leaving it to the caller: a resolution that
        # only reaches the file leaves the database disagreeing with it, and
        # the next query still reports the conflict as unresolved.
        apply_csv(conn, path)
    return changes


def apply_csv(conn: sqlite3.Connection, path: Path) -> int:
    """Load the CSV into the categories table, replacing what was there."""
    mapping = read_csv(path)
    conn.execute("DELETE FROM categories")
    conn.executemany(
        "INSERT INTO categories (name_key, category) VALUES (?, ?)",
        sorted(mapping.items()),
    )
    conn.commit()
    return len(mapping)

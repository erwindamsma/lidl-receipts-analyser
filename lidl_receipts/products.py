"""Group the till's many names for one article into a single product.

Ranking spending per article is misleading without this. The till renames
things constantly and reissues article ids, so one product arrives as several
rows and each looks smaller than it is: six spellings of mineral water, and a
tequila beer split across three barcodes.

Neither key works alone. Grouping by name splits a product whose barcode
changed; grouping by barcode splits a product that was renamed, and both were
happening at once. So two rows belong together when they share a **name or a
barcode**, followed transitively. That is a union-find over the (name, barcode)
pairs the receipts actually contain.

Deliberately derived, never hand-maintained: it rebuilds from the items table,
so it costs nothing to redo and there is no second file to keep in sync.
"""

from __future__ import annotations

import sqlite3


class _Union:
    """Union-find with path halving."""

    def __init__(self) -> None:
        self._parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(self, item: tuple[str, str]) -> tuple[str, str]:
        parent = self._parent.setdefault(item, item)
        while parent != item:
            item, parent = parent, self._parent.setdefault(parent, parent)
            self._parent[item] = self._parent.setdefault(parent, parent)
        return item

    def union(self, left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[left_root] = right_root


def build(conn: sqlite3.Connection) -> int:
    """Rebuild the products table from the items. Returns the group count."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT name_key, MAX(name) AS name, barcode,
               SUM(net_cents) AS spend
        FROM items
        WHERE name_key <> ''
        GROUP BY name_key, barcode
        """
    ).fetchall()

    groups = _Union()
    for row in rows:
        node = ("name", row["name_key"])
        groups.find(node)
        if row["barcode"]:
            groups.union(node, ("barcode", row["barcode"]))

    # The name that carries the most spend represents the group: it is the
    # spelling seen most often on the receipts that matter.
    best: dict[tuple[str, str], tuple[int, str]] = {}
    members: dict[str, tuple[str, str]] = {}
    for row in rows:
        root = groups.find(("name", row["name_key"]))
        members[row["name_key"]] = root
        spend = row["spend"] or 0
        if root not in best or spend > best[root][0]:
            best[root] = (spend, row["name"])

    conn.execute("DELETE FROM products")
    conn.executemany(
        "INSERT OR REPLACE INTO products (name_key, product_key, product_name)"
        " VALUES (?, ?, ?)",
        [
            (name_key, f"{root[0]}:{root[1]}", best[root][1])
            for name_key, root in members.items()
        ],
    )
    conn.commit()
    return len(best)

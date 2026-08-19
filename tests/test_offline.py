"""Offline tests for everything that does not need the Lidl API.

Run with: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lidl_receipts import auth, categories, cli, handler  # noqa: E402
from lidl_receipts.config import Config  # noqa: E402
from lidl_receipts.normalize import (  # noqa: E402
    normalize_ticket,
    parse_amount,
    parse_date,
    parse_quantity,
    to_export_dict,
)
from lidl_receipts import products  # noqa: E402
from lidl_receipts.receipt_html import parse_receipt_html  # noqa: E402
from lidl_receipts.store import Store  # noqa: E402

# Shaped after the documented German/Austrian v3 payload, which is the family
# the Dutch response is expected to belong to. Replace with a real NL dump as
# soon as one is available.
SAMPLE_TICKET = {
    "id": "abc-123",
    "date": "2025-08-01T17:42:11+02:00",
    "totalAmount": "54,31",
    "currency": {"code": "EUR"},
    "store": {
        "id": "NL0123",
        "name": "Lidl Utrecht Kanaleneiland",
        "address": "Beneluxlaan 1",
        "postalCode": "3527 HS",
        "locality": "Utrecht",
    },
    "itemsLine": [
        {
            "name": "Vegane Frikadellen",
            "quantity": "1",
            "currentUnitPrice": "2,19",
            "originalAmount": "2,19",
            "isWeight": False,
            "taxGroup": "1",
            "taxGroupName": "A",
            "codeInput": "4023456245134",
            "discounts": [{"description": "5€ Coupon", "amount": "0,21"}],
            "deposit": None,
        },
        {
            "name": "Bananen",
            "quantity": "1,234",
            "currentUnitPrice": "1,29",
            "originalAmount": "1,59",
            "isWeight": True,
            "taxGroup": "1",
            "taxGroupName": "A",
            "codeInput": "20012345",
            "discounts": [],
            "deposit": None,
        },
    ],
}


class ParseAmountTest(unittest.TestCase):
    def test_locale_variants(self):
        self.assertEqual(parse_amount("2,19"), 219)
        self.assertEqual(parse_amount("2.19"), 219)
        self.assertEqual(parse_amount("1.234,56"), 123456)
        self.assertEqual(parse_amount("1,234.56"), 123456)
        self.assertEqual(parse_amount("-0,21"), -21)
        self.assertEqual(parse_amount(2.19), 219)
        self.assertEqual(parse_amount(3), 300)
        self.assertEqual(parse_amount("€ 4,05"), 405)

    def test_thousands_only_dot(self):
        # "1.234" is a thousands separator, not 1 euro 234 cents.
        self.assertEqual(parse_amount("1.234"), 123400)

    def test_empty_and_garbage(self):
        for value in (None, "", "n/a", {}, True):
            self.assertIsNone(parse_amount(value))

    def test_nested_amount_object(self):
        self.assertEqual(parse_amount({"amount": "1,50"}), 150)


class ParseQuantityTest(unittest.TestCase):
    """Quantities are not money: a lone dot is always a decimal point."""

    def test_dot_is_never_a_thousands_separator(self):
        # "0.792" is a weight in kilos, "1.000" is one item. Reading either as
        # grouped digits multiplies any derived amount by a thousand.
        self.assertEqual(parse_quantity("0.792"), 0.792)
        self.assertEqual(parse_quantity("1.000"), 1.0)
        self.assertEqual(parse_quantity("0,792"), 0.792)
        self.assertEqual(parse_quantity("2"), 2.0)

    def test_money_keeps_the_grouping_rule(self):
        self.assertEqual(parse_amount("1.234"), 123400)

    def test_a_dotted_quantity_does_not_inflate_the_line(self):
        receipt = normalize_ticket({
            "id": "q", "date": "2025-01-01", "totalAmount": "2,19",
            "itemsLine": [{"name": "Melk", "quantity": "1.000",
                           "currentUnitPrice": "2,19", "discounts": []}],
        })
        self.assertEqual(receipt["items"][0]["net_cents"], 219)
        self.assertEqual(
            sum(i["net_cents"] for i in receipt["items"]), receipt["total_cents"]
        )


class ParseDateTest(unittest.TestCase):
    def test_variants(self):
        self.assertTrue(parse_date("2025-08-01T17:42:11+02:00"))
        self.assertTrue(parse_date("2025-08-01T17:42:11Z"))
        self.assertTrue(parse_date("2025-08-01"))
        self.assertIsNone(parse_date("gisteren"))


class NormalizeTest(unittest.TestCase):
    def test_receipt_fields(self):
        receipt = normalize_ticket(SAMPLE_TICKET)
        self.assertEqual(receipt["id"], "abc-123")
        self.assertEqual(receipt["total_cents"], 5431)
        self.assertEqual(receipt["currency"], "EUR")
        self.assertEqual(receipt["store"]["locality"], "Utrecht")
        self.assertEqual(receipt["warnings"], [])

    def test_line_items(self):
        items = normalize_ticket(SAMPLE_TICKET)["items"]
        self.assertEqual(len(items), 2)

        first = items[0]
        self.assertEqual(first["name"], "Vegane Frikadellen")
        self.assertEqual(first["amount_cents"], 219)
        self.assertEqual(first["discount_cents"], 21)
        self.assertEqual(first["net_cents"], 198)
        self.assertEqual(first["barcode"], "4023456245134")
        self.assertFalse(first["is_weight"])

        second = items[1]
        self.assertTrue(second["is_weight"])
        # Weights must keep their gram precision, not round to cents.
        self.assertEqual(second["quantity"], 1.234)

    def test_nested_deposit_becomes_its_own_line(self):
        # Older NATIVE receipts hang the deposit inside the article; it counts
        # towards the receipt total but not towards the article's amount.
        receipt = normalize_ticket(
            {
                "id": "native-1",
                "date": "2021-12-19T10:00:00",
                "ticketType": "NATIVE",
                "totalAmount": "12,12",
                "itemsLine": [
                    {
                        "name": "Sinas 0% lemon 0%",
                        "quantity": "12",
                        "currentUnitPrice": "0,76",
                        "originalAmount": "9,12",
                        "codeInput": "20396855",
                        "discounts": [],
                        "deposit": {
                            "quantity": 12,
                            "taxGroupName": "A",
                            "amount": "3,00",
                            "description": "Fris Pet fles leeg",
                            "unitPrice": "0,25",
                        },
                    }
                ],
            }
        )
        self.assertEqual(len(receipt["items"]), 2)
        article, deposit = receipt["items"]
        self.assertEqual(article["net_cents"], 912)
        self.assertEqual(deposit["name"], "Statiegeld Fris Pet fles leeg")
        self.assertEqual(deposit["net_cents"], 300)
        self.assertEqual(deposit["quantity"], 12)
        # Recognisable as deposit, so it lands outside the grocery totals.
        self.assertEqual(categories.propose(deposit["name"]), "statiegeld")
        # And the line sums now reach the receipt total.
        self.assertEqual(
            sum(i["net_cents"] for i in receipt["items"]),
            receipt["total_cents"],
        )

    def test_missing_items_is_flagged_not_fatal(self):
        receipt = normalize_ticket({"id": "x", "date": "2025-01-01"})
        self.assertEqual(receipt["items"], [])
        self.assertTrue(receipt["warnings"])

    def test_uk_html_payload_is_flagged(self):
        receipt = normalize_ticket(
            {"id": "x", "date": "2025-01-01", "htmlPrintedReceipt": "<html>"}
        )
        self.assertTrue(
            any("htmlPrintedReceipt" in w for w in receipt["warnings"])
        )

    def test_export_shape(self):
        exported = to_export_dict(normalize_ticket(SAMPLE_TICKET))
        self.assertEqual(exported["total"], 54.31)
        self.assertEqual(exported["items"][0]["unitPrice"], 2.19)
        self.assertEqual(
            exported["items"][0]["discounts"][0]["amount"], 0.21
        )


def _span(line: int, text: str, cls: str = "", **attrs: str) -> str:
    parts = [f'<span id="purchase_list_line_{line}"']
    if cls:
        parts.append(f' class="{cls}"')
    for key, value in attrs.items():
        parts.append(f' data-{key.replace("_", "-")}="{value}"')
    parts.append(f">{text}</span>\n")
    return "".join(parts)


# Mirrors the real Dutch payload: article, multi-quantity article, a weighed
# article with its continuation line, both discount flavours, and a returned
# deposit line.
SAMPLE_HTML = (
    "<html><body><pre>"
    '<span class="purchase_list">'
    + _span(1, "OMSCHRIJVING     EUR", "currency css_bold", currency="€")
    + _span(
        2, "Bandschuurmachine-0502092     39,99 C", "article css_bold",
        art_id="0502092", unit_price="39,99", tax_type="C",
        art_description="Bandschuurmachine-0502092",
    )
    + _span(
        3, "     Lidl Plus korting       -20,00", "discount css_bold",
        promotion_id="100001000-NL-TEMPLATE-1",
    )
    + _span(4, "In prijs verlaagd             -1,80")
    + _span(
        5, "Frambozen siroop  2 x 1,79     3,58 B", "article css_bold",
        art_id="6607485", art_quantity="2", unit_price="1,79", tax_type="B",
        art_description="Frambozen siroop",
    )
    + _span(
        6, "Bananen                        1,10 B", "article css_bold",
        art_id="0080000", art_quantity="0,792", unit_price="1,39",
        tax_type="B", art_description="Bananen",
    )
    + _span(
        7, "  0,792 kg x 1,39   EUR/kg", "article",
        art_id="0080000", art_quantity="0,792", unit_price="1,39",
        tax_type="B", art_description="Bananen",
    )
    + _span(8, "[X] Emballage                 -3,90", "css_bold")
    + "</span></pre></body></html>"
)


class ReceiptHtmlTest(unittest.TestCase):
    """The Dutch API returns no line-item array, only printed-receipt HTML."""

    def setUp(self):
        self.items = parse_receipt_html(SAMPLE_HTML)

    def test_column_header_is_not_an_item(self):
        self.assertNotIn("OMSCHRIJVING", [i["name"] for i in self.items])

    def test_article_id_suffix_is_stripped_from_the_name(self):
        self.assertEqual(self.items[0]["name"], "Bandschuurmachine")
        self.assertEqual(self.items[0]["codeInput"], "0502092")

    def test_both_discount_flavours_attach_to_the_article(self):
        discounts = self.items[0]["discounts"]
        self.assertEqual(
            [d["description"] for d in discounts],
            ["Lidl Plus korting", "In prijs verlaagd"],
        )
        self.assertEqual([d["amount"] for d in discounts], ["-20,00", "-1,80"])

    def test_quantity_and_unit_price(self):
        siroop = self.items[1]
        self.assertEqual(siroop["quantity"], "2")
        self.assertEqual(siroop["currentUnitPrice"], "1,79")
        self.assertEqual(siroop["originalAmount"], "3,58")
        self.assertEqual(siroop["taxGroupName"], "B")

    def test_weight_continuation_does_not_duplicate_the_article(self):
        bananen = [i for i in self.items if i["name"] == "Bananen"]
        self.assertEqual(len(bananen), 1)
        self.assertTrue(bananen[0]["isWeight"])
        self.assertEqual(bananen[0]["quantity"], "0,792")

    def test_weight_continuation_spelling_variants(self):
        # Older receipts end the continuation line with "EUR", newer ones
        # with "EUR/kg". Recognising it by wording split the article in two.
        for tail in ("EUR", "EUR/kg", "EUR/kg "):
            with self.subTest(tail=tail):
                html = (
                    '<span class="purchase_list">'
                    + _span(
                        1, "Bananen                  1,53 B", "article",
                        art_id="0080000", art_quantity="1,120",
                        unit_price="1,37", tax_type="B",
                        art_description="Bananen",
                    )
                    + _span(
                        2, f"  1,120 kg x 1,37   {tail}", "article",
                        art_id="0080000", art_quantity="1,120",
                        unit_price="1,37", tax_type="B",
                        art_description="Bananen",
                    )
                    + "</span>"
                )
                items = parse_receipt_html(html)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["originalAmount"], "1,53")
                self.assertTrue(items[0]["isWeight"])

    def test_self_scan_correction_receipt_is_itemised(self):
        # The purchase list of such a receipt holds only the lump sum the
        # scanner missed; the real trolley is itemised in the header. Totals
        # reconcile either way, so nothing but this parse recovers the items.
        html = (
            '<span id="header_line_1">Gemiste artikelen           1     9,00</span>\n'
            '<span id="header_line_2">Food                 1 x 9,00     9,00</span>\n'
            '<span id="header_line_3">----------------------------------------</span>\n'
            '<span id="header_line_4">Extra artikelen            3      9,00</span>\n'
            '<span id="header_line_5">Gnocchi              1 x 1,49     1,49</span>\n'
            '<span id="header_line_6">Kipdijfilet gr.verp. 2 x 3,30     6,60</span>\n'
            '<span id="header_line_7">Bananen                          0,91</span>\n'
            '<span id="header_line_8">  0.924 kg x 0,99</span>\n'
            '<span id="header_line_9">----------------------------------------</span>\n'
            '<span class="purchase_list">'
            + _span(1, "Food     9,00 B", "article css_bold",
                    art_id="0002316", unit_price="9,00", tax_type="B",
                    art_description="Food")
            + "</span>"
            '<span class="vat_info"><span id="vat_info_line_1" '
            'data-tax-type="B" data-tax-percentage="9">B 9</span></span>'
        )
        items = parse_receipt_html(html)
        self.assertEqual([i["name"] for i in items],
                         ["Gnocchi", "Kipdijfilet gr.verp.", "Bananen"])

        # A name filling the full 20-character column leaves a single space
        # before the quantity, which a stricter separator missed.
        self.assertEqual(items[1]["quantity"], "2")
        self.assertEqual(items[1]["currentUnitPrice"], "3,30")
        # The tax group comes from the receipt's VAT summary, since the
        # correction block prints none per line.
        self.assertEqual(items[0]["taxGroupName"], "B")

        receipt = normalize_ticket({
            "id": "corr-1", "date": "2023-09-26T11:22:48", "ticketType": "HTML",
            "totalAmount": "9,00", "htmlPrintedReceipt": html,
        })
        self.assertEqual(len(receipt["items"]), 3)
        self.assertEqual(
            sum(i["net_cents"] for i in receipt["items"]), receipt["total_cents"]
        )
        bananen = receipt["items"][2]
        self.assertTrue(bananen["is_weight"])
        self.assertEqual(bananen["quantity"], 0.924)

    def test_dot_decimals_are_accepted(self):
        # Britain and Ireland get the same printed-receipt HTML with dots.
        # Requiring a comma yielded zero articles and a receipt with a total
        # but no lines. Silent, because every line then looked like a
        # continuation of the one above.
        html = (
            '<span class="purchase_list">'
            + _span(2, "Bananas          1.10 B", "article",
                    art_id="0080000", unit_price="1.39", tax_type="B",
                    art_description="Bananas")
            + "</span>"
        )
        items = parse_receipt_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["originalAmount"], "1.10")

    def test_returned_deposit_is_its_own_line_not_a_discount(self):
        emballage = self.items[-1]
        self.assertEqual(emballage["name"], "[X] Emballage")
        self.assertEqual(emballage["originalAmount"], "-3,90")
        self.assertEqual(emballage["discounts"], [])

    def test_receipt_reconciles_to_the_total(self):
        receipt = normalize_ticket(
            {
                "id": "html-1",
                "date": "2026-08-12T14:28:11",
                "ticketType": "HTML",
                # 39,99 - 20,00 - 1,80 + 3,58 + 1,10 - 3,90
                "totalAmount": 18.97,
                "currency": {"code": "EUR"},
                "htmlPrintedReceipt": SAMPLE_HTML,
            }
        )
        self.assertEqual(receipt["warnings"], [])
        self.assertEqual(
            sum(i["net_cents"] for i in receipt["items"]),
            receipt["total_cents"],
        )


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "receipts.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_save_and_dedup(self):
        self.store.save(SAMPLE_TICKET)
        self.store.save(SAMPLE_TICKET)  # idempotent upsert
        stats = self.store.stats()
        self.assertEqual(stats["receipts"], 1)
        self.assertEqual(stats["items"], 2)
        self.assertEqual(self.store.known_ids(), {"abc-123"})

    def test_raw_payload_round_trip(self):
        self.store.save(SAMPLE_TICKET)
        stored = dict(self.store.raw_payloads())["abc-123"]
        self.assertEqual(stored, SAMPLE_TICKET)

    def test_renormalize_rebuilds_derived_tables(self):
        self.store.save(SAMPLE_TICKET)
        self.store.conn.execute("DELETE FROM items")
        self.store.conn.commit()
        count, _ = self.store.renormalize()
        self.assertEqual(count, 1)
        self.assertEqual(self.store.stats()["items"], 2)

    def test_euro_view(self):
        self.store.save(SAMPLE_TICKET)
        row = self.store.conn.execute(
            "SELECT total FROM v_receipts WHERE id = 'abc-123'"
        ).fetchone()
        self.assertAlmostEqual(row["total"], 54.31)


class SyncTest(unittest.TestCase):
    """Exercise the importer against a stub client, no network involved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(
            country="NL",
            language="nl",
            refresh_token="dummy",
            data_dir=self.tmp.name,
        )
        self.fetched: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _stub_client(self, ticket_ids):
        fetched = self.fetched

        class StubClient:
            def __init__(self, config, **kwargs):
                pass

            def list_tickets(self):
                # Newest first, as the real API returns them. Entries are
                # either an id or an (id, date) pair.
                for entry in ticket_ids:
                    if isinstance(entry, tuple):
                        ticket_id, date = entry
                    else:
                        ticket_id, date = entry, "2025-08-01T10:00:00Z"
                    yield {"id": ticket_id, "date": date}

            def ticket(self, ticket_id):
                fetched.append(ticket_id)
                return dict(SAMPLE_TICKET, id=ticket_id)

        return StubClient

    def _run_sync(self, ticket_ids, **flags):
        options = {
            "limit": 0,
            "force": False,
            "fast": False,
            "delay": 0,
            "no_raw_files": False,
            "since": None,
        }
        options.update(flags)
        args = argparse.Namespace(**options)
        with mock.patch.object(
            cli, "LidlClient", self._stub_client(ticket_ids)
        ), mock.patch.object(Config, "load", return_value=self.config):
            return cli.cmd_sync(args)

    def test_fetches_all_then_only_new(self):
        self.assertEqual(self._run_sync(["a", "b"]), 0)
        self.assertEqual(self.fetched, ["a", "b"])

        # Second run: "c" is new, "a"/"b" are already stored.
        self.fetched.clear()
        self._run_sync(["c", "a", "b"])
        self.assertEqual(self.fetched, ["c"])

        with Store(self.config.db_path) as store:
            self.assertEqual(store.known_ids(), {"a", "b", "c"})

    def test_fast_stops_at_first_known(self):
        self._run_sync(["a"])
        self.fetched.clear()
        # "b" is new, then "a" is known: --fast must stop and skip "d".
        self._run_sync(["b", "a", "d"], fast=True)
        self.assertEqual(self.fetched, ["b"])

    def test_force_refetches(self):
        self._run_sync(["a"])
        self.fetched.clear()
        self._run_sync(["a"], force=True)
        self.assertEqual(self.fetched, ["a"])

    def test_raw_files_are_written(self):
        self._run_sync(["a"])
        self.assertTrue((self.config.raw_dir / "a.json").exists())

    def test_limit_caps_downloads(self):
        self._run_sync(["a", "b", "c"], limit=2)
        self.assertEqual(self.fetched, ["a", "b"])

    def test_since_stops_at_the_first_older_receipt(self):
        self._run_sync(
            [
                ("new", "2026-03-01T10:00:00"),
                ("edge", "2026-01-01T09:00:00"),
                ("old", "2025-12-31T23:59:00"),
                ("older", "2020-10-05T21:43:17"),
            ],
            since="2026-01-01",
        )
        # The boundary date itself is included; everything before it stops
        # the scan, including receipts further down the list.
        self.assertEqual(self.fetched, ["new", "edge"])


class CategoriesTest(unittest.TestCase):
    """Keyword matching against Dutch till names."""

    def test_compound_head_noun_is_matched(self):
        # Dutch compounds carry the head noun at the end.
        for name, expected in [
            ("Mineraalwater bruis.", "frisdrank & sap"),
            ("Halfvol.weidemelk h.", "zuivel & eieren"),
            ("Knakworsten 10 stuks", "vlees & vis"),
            ("Schouderham rond BL", "vlees & vis"),
            ("Kookroom", "zuivel & eieren"),
            ("Cherrytomaten", "groente & fruit"),
            ("Turksbrood", "brood & bakkerij"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(categories.propose(name, "B"), expected)

    def test_short_keyword_does_not_match_mid_word(self):
        # "sap" inside "lasapparaat" must not make it a soft drink.
        self.assertEqual(
            categories.propose("Inverter lasapparaat", "C"), "non-food"
        )

    def test_rule_order_puts_spreads_before_dairy(self):
        self.assertEqual(
            categories.propose("Pindakaas 100% XXL", "B"), "broodbeleg"
        )
        self.assertEqual(
            categories.propose("eiersalade surinaams", "B"), "broodbeleg"
        )

    def test_tax_group_fallback(self):
        self.assertEqual(categories.propose("Zomaar iets", "C"), "non-food")
        self.assertEqual(
            categories.propose("Zomaar iets", "B"), "overig voeding"
        )
        self.assertEqual(categories.propose("Zomaar iets", ""), "ongecategoriseerd")

    def test_high_vat_article_in_a_food_category_is_flagged(self):
        # Lidl charges 9% on groceries, so a "food" article at 21% is really
        # drink, drugstore or hardware. This is what caught wine filed under
        # soft drinks because the till calls it "Fris en fruitig".
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "categories.csv"
            store = Store(Path(tmp) / "receipts.db")
            store.save(
                {
                    "id": "vat-1",
                    "date": "2024-05-05T10:00:00",
                    "totalAmount": "10,00",
                    "itemsLine": [
                        {
                            "name": "Fris en fruitig wit",
                            "originalAmount": "8,00",
                            "quantity": "1",
                            "taxGroupName": "C",
                            "discounts": [],
                        },
                        {
                            "name": "Bananen",
                            "originalAmount": "2,00",
                            "quantity": "1",
                            "taxGroupName": "B",
                            "discounts": [],
                        },
                    ],
                }
            )
            path.write_text(
                "name,category,spend,times\n"
                "Fris en fruitig wit,frisdrank & sap,8.00,1\n"
                "Bananen,groente & fruit,2.00,1\n",
                encoding="utf-8",
            )
            categories.apply_csv(store.conn, path)

            flagged = categories.high_vat_mismatches(store.conn, min_spend=1.0)
            self.assertEqual([r["name"] for r in flagged], ["Fris en fruitig wit"])

            # Moving it to a category that may carry 21% clears the flag.
            path.write_text(
                "name,category,spend,times\n"
                "Fris en fruitig wit,alcohol,8.00,1\n"
                "Bananen,groente & fruit,2.00,1\n",
                encoding="utf-8",
            )
            categories.apply_csv(store.conn, path)
            self.assertEqual(
                categories.high_vat_mismatches(store.conn, min_spend=1.0), []
            )
            store.close()

    def test_accented_name_still_finds_its_category(self):
        # SQLite's lower() folds ASCII only, so joining on lower(name) left
        # every accented article unmatched and looking uncategorised. The key
        # is computed in Python and stored, which is what this guards.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "categories.csv"
            store = Store(Path(tmp) / "receipts.db")
            store.save(
                {
                    "id": "acc-1",
                    "date": "2023-01-01T10:00:00",
                    "totalAmount": "5,00",
                    "itemsLine": [
                        {
                            "name": "Âlde Fryske",
                            "originalAmount": "5,00",
                            "quantity": "1",
                            "taxGroupName": "B",
                            "discounts": [],
                        }
                    ],
                }
            )
            path.write_text(
                "name,category,spend,times\nÂlde Fryske,zuivel & eieren,5.00,1\n",
                encoding="utf-8",
            )
            categories.apply_csv(store.conn, path)

            row = store.conn.execute(
                "SELECT category FROM v_spend WHERE name = 'Âlde Fryske'"
            ).fetchone()
            self.assertEqual(row["category"], "zuivel & eieren")
            store.close()

    def test_category_conflict_prefers_the_decided_category(self):
        # Two names behind one barcode are one product. The variant that got
        # classified wins, even when the one in the fallback bucket earned
        # more, otherwise resolving conflicts undoes the categorisation.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "categories.csv"
            store = Store(Path(tmp) / "receipts.db")
            store.save(
                {
                    "id": "bc-1",
                    "date": "2023-01-01T10:00:00",
                    "totalAmount": "13,00",
                    "itemsLine": [
                        {
                            "name": "Haverbiscuits bosvr.",
                            "codeInput": "6611072",
                            "originalAmount": "10,00",
                            "quantity": "1",
                            "taxGroupName": "B",
                            "discounts": [],
                        },
                        {
                            "name": "Haverbiscuits bosvrucht",
                            "codeInput": "6611072",
                            "originalAmount": "3,00",
                            "quantity": "1",
                            "taxGroupName": "B",
                            "discounts": [],
                        },
                    ],
                }
            )
            path.write_text(
                "name,category,spend,times\n"
                "Haverbiscuits bosvr.,overig voeding,10.00,1\n"
                "Haverbiscuits bosvrucht,snoep & snacks,3.00,1\n",
                encoding="utf-8",
            )
            categories.apply_csv(store.conn, path)
            products.build(store.conn)

            changes = categories.resolve_category_conflicts(store.conn, path)
            self.assertEqual(
                changes, [("Haverbiscuits bosvr.", "overig voeding", "snoep & snacks")]
            )
            self.assertEqual(categories.category_conflicts(store.conn), [])
            store.close()

    def test_empty_database_does_not_wipe_the_mapping(self):
        # refresh_csv used to write a header-only file when there was nothing
        # to write against, replacing hand-made categories with nothing.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "categories.csv"
            path.write_text(
                "name,category,spend,times\nBananen,groente & fruit,1.00,1\n",
                encoding="utf-8",
            )
            store = Store(Path(tmp) / "empty.db")
            total, seeded, proposed = categories.refresh_csv(store.conn, path)
            self.assertEqual((total, seeded, proposed), (1, 0, 0))
            self.assertEqual(
                categories.read_csv(path), {"bananen": "groente & fruit"}
            )
            store.close()

    def test_csv_round_trip_preserves_corrections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "categories.csv"
            store = Store(Path(tmp) / "receipts.db")
            store.save(SAMPLE_TICKET)

            categories.refresh_csv(store.conn, path)
            # Overrule one proposal by hand, as the user would.
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace("Bananen,groente & fruit", "Bananen,mijn-eigen-label"),
                encoding="utf-8",
            )

            total, seeded, proposed = categories.refresh_csv(store.conn, path)
            self.assertEqual((seeded, proposed), (0, 0))  # nothing re-derived
            self.assertEqual(
                categories.read_csv(path)["bananen"], "mijn-eigen-label"
            )

            categories.apply_csv(store.conn, path)
            row = store.conn.execute(
                "SELECT category FROM v_spend WHERE name = 'Bananen'"
            ).fetchone()
            self.assertEqual(row["category"], "mijn-eigen-label")
            store.close()


class VerifyCommandTest(unittest.TestCase):
    """`verify` had no test at all, which is how it shipped a NameError."""

    def _run(self, tmp, receipt):
        store = Store(Path(tmp) / "receipts.db")
        if receipt:
            store.save(receipt)
        store.close()
        config = Config(data_dir=tmp)
        with mock.patch.object(Config, "load", return_value=config):
            return cli.cmd_verify(argparse.Namespace())

    # SAMPLE_TICKET deliberately does not reconcile, since it exists to
    # exercise field mapping, so this needs a receipt whose lines add up.
    SOUND = {
        "id": "sound-1", "date": "2025-03-03T12:00:00", "totalAmount": "3,57",
        "itemsLine": [
            {"name": "Vegane Frikadellen", "quantity": "1",
             "originalAmount": "2,19",
             "discounts": [{"description": "Coupon", "amount": "0,21"}]},
            {"name": "Bananen", "quantity": "1", "originalAmount": "1,59",
             "discounts": []},
        ],
    }

    def test_reconciling_receipt_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(tmp, self.SOUND), 0)

    def test_empty_database_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(tmp, None), 0)

    def test_mismatch_returns_one(self):
        broken = dict(self.SOUND, id="broken", totalAmount="99,99")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(tmp, broken), 1)


class AuthTest(unittest.TestCase):
    def test_pkce_challenge_is_url_safe(self):
        verifier, challenge = auth.generate_pkce()
        self.assertNotIn("=", challenge)
        self.assertNotIn("+", challenge)
        self.assertNotEqual(verifier, challenge)

    def test_extract_code_from_callback_url(self):
        url = "com.lidlplus.app://callback?code=ABC123&scope=openid"
        self.assertEqual(auth.extract_code(url), "ABC123")

    def test_extract_bare_code(self):
        self.assertEqual(auth.extract_code("  ABC-123_x  "), "ABC-123_x")

    def test_extract_code_rejects_junk(self):
        with self.assertRaises(ValueError):
            auth.extract_code("no code here!")

    def test_authorize_url_carries_country(self):
        url = auth.authorize_url("chal", "NL", "nl")
        self.assertIn("Country=NL", url)
        self.assertIn("language=nl-NL", url)
        self.assertIn("code_challenge_method=S256", url)


def fake_winreg():
    """Enough of the Windows registry to exercise the native backend.

    The Windows path cannot be run from Linux, and it is the one backend with
    no manual fallback worth having, so it gets checked against a stand-in
    rather than not at all.
    """
    store: dict[str, dict] = {}

    class Key:
        def __init__(self, path):
            self.path = path
            store.setdefault(path, {})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_key(root, path):
        if path not in store:
            raise FileNotFoundError(path)
        return Key(path)

    def delete_key(root, path):
        if path not in store:
            raise FileNotFoundError(path)
        del store[path]

    return types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        REG_SZ=1,
        CreateKey=lambda root, path: Key(path),
        OpenKey=open_key,
        DeleteKey=delete_key,
        SetValueEx=lambda key, name, _reserved, _type, value: (
            store[key.path].__setitem__(name, value)
        ),
        QueryValueEx=lambda key, name: (store[key.path][name], 1),
        store=store,
    )


def fake_xdg_run(argv, **kwargs):
    """Stand in for xdg-mime, which is absent on a headless machine."""
    stdout = ""
    if argv[:3] == ["xdg-mime", "query", "default"]:
        stdout = handler.DESKTOP_ID + "\n"
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class HandlerTest(unittest.TestCase):
    """The scheme handler, minus the desktop it registers with."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "callback.json"
        patch = mock.patch.multiple(
            handler, CONFIG_DIR=Path(self.tmp.name), CALLBACK_PATH=self.path
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_deposit_extracts_the_code(self):
        handler.deposit("com.lidlplus.app://callback?code=abc123&state=xyz")
        self.assertEqual(handler.wait_for_code(timeout=1), "abc123")

    def test_deposit_is_private(self):
        handler.deposit("com.lidlplus.app://callback?code=abc123")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_code_is_consumed_once(self):
        handler.deposit("com.lidlplus.app://callback?code=abc123")
        handler.wait_for_code(timeout=1)
        self.assertFalse(self.path.exists())

    def test_junk_is_reported_not_raised(self):
        # The handler runs detached, so an exception there would vanish.
        handler.deposit("not a callback at all")
        with self.assertRaises(ValueError):
            handler.wait_for_code(timeout=1)

    def test_timeout_falls_back_to_pasting(self):
        self.assertIsNone(handler.wait_for_code(timeout=0.3, interval=0.05))

    def test_clear_drops_a_stale_code(self):
        handler.deposit("com.lidlplus.app://callback?code=stale")
        handler.clear()
        self.assertIsNone(handler.wait_for_code(timeout=0.3, interval=0.05))

    def test_every_backend_spells_out_the_drop_path(self):
        """Explorer starts the handler without the user's shell, so the
        directory to write in cannot come from the environment."""
        for command in (
            handler._windows_command(),
            handler._wsl_command(),
            handler._xdg_exec(),
        ):
            with self.subTest(command=command):
                self.assertIn(f"--callback {self.path}", command)

    def test_windows_style_commands_quote_the_url(self):
        for command in (handler._windows_command(), handler._wsl_command()):
            with self.subTest(command=command):
                self.assertTrue(command.endswith(' "%1"'))

    def test_native_windows_does_not_detour_through_wsl(self):
        self.assertNotIn("wsl.exe", handler._windows_command())
        self.assertIn("wsl.exe", handler._wsl_command())

    def test_desktop_entry_uses_the_freedesktop_placeholder(self):
        exec_line = handler._xdg_exec()
        self.assertTrue(exec_line.endswith(" %u"))
        self.assertNotIn("%1", exec_line)

    def test_every_backend_is_wired_up(self):
        self.assertEqual(
            set(handler._BACKENDS), {"windows", "wsl", "xdg"}
        )
        for kind, functions in handler._BACKENDS.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(functions), 3)
                self.assertTrue(all(callable(f) for f in functions))

    def test_a_handler_writing_elsewhere_counts_as_absent(self):
        # Right script, wrong drop file: this login would wait forever.
        stale = handler._wsl_command().replace(
            str(self.path), "/somewhere/else/callback.json"
        )
        with mock.patch.object(
            handler, "registered_command", return_value=stale
        ):
            self.assertFalse(handler.installed())

    def test_ensure_is_quiet_where_it_cannot_register(self):
        # A login must never fail because the desktop is unsupported.
        with mock.patch.object(handler, "backend", return_value=None):
            self.assertIsNone(handler.ensure())

    def test_ensure_swallows_a_failing_desktop(self):
        with mock.patch.object(handler, "backend", return_value="xdg"), \
                mock.patch.object(
                    handler, "_xdg_registered", return_value=None
                ), \
                mock.patch.object(
                    handler, "_xdg_install", side_effect=OSError("no desktop")
                ):
            self.assertIsNone(handler.ensure())

    def test_windows_registration_round_trips(self):
        winreg = fake_winreg()
        with mock.patch.dict(sys.modules, {"winreg": winreg}):
            command = handler._windows_install()
            self.assertEqual(handler._windows_registered(), command)
            # The empty "URL Protocol" value is what marks a key as a scheme;
            # without it Explorer ignores the registration entirely.
            self.assertIn("URL Protocol", winreg.store[handler.REG_PATH])
            self.assertEqual(
                winreg.store[handler.REG_PATH][None], handler.REG_DESCRIPTION
            )
            handler._windows_uninstall()
            self.assertIsNone(handler._windows_registered())

    def test_xdg_registration_round_trips(self):
        desktop = Path(self.tmp.name) / handler.DESKTOP_ID
        with mock.patch.object(handler, "DESKTOP_PATH", desktop), \
                mock.patch.object(
                    handler.subprocess, "run", side_effect=fake_xdg_run
                ):
            exec_line = handler._xdg_install()
            body = desktop.read_text()
            self.assertIn(
                f"MimeType=x-scheme-handler/{handler.SCHEME};", body
            )
            self.assertIn(f"Exec={exec_line}", body)
            self.assertEqual(handler._xdg_registered(), exec_line)
            handler._xdg_uninstall()
            self.assertFalse(desktop.exists())

    def test_ensure_leaves_an_existing_registration_alone(self):
        with mock.patch.object(handler, "installed", return_value=True), \
                mock.patch.object(handler, "install") as install:
            self.assertIsNone(handler.ensure())
        install.assert_not_called()

    def test_a_handler_from_elsewhere_counts_as_absent(self):
        # It would deposit the code in another config directory, and this
        # login would wait for a file that never arrives.
        with mock.patch.object(
            handler, "registered_command", return_value="wsl.exe /elsewhere/lidl.py"
        ):
            self.assertFalse(handler.installed())

    def test_own_registration_is_recognised(self):
        with mock.patch.object(
            handler, "registered_command", return_value=handler._wsl_command()
        ):
            self.assertTrue(handler.installed())


class LoginCallbackTest(unittest.TestCase):
    def test_receive_only_deposits(self):
        """--receive must not touch the config: no login is in progress."""
        with mock.patch.object(handler, "deposit") as deposit, \
                mock.patch.object(auth, "exchange_code") as exchange:
            args = argparse.Namespace(
                receive="com.lidlplus.app://callback?code=abc",
                callback="/tmp/drop.json",
                code=None, country=None, language=None, no_handler=False,
            )
            self.assertEqual(cli.cmd_login(args), 0)
        deposit.assert_called_once_with(
            "com.lidlplus.app://callback?code=abc", "/tmp/drop.json"
        )
        exchange.assert_not_called()

    def test_a_callback_without_a_code_falls_back_to_pasting(self):
        """An error redirect is the user's browser talking, not a bug here."""
        with mock.patch.object(
            handler, "installed", return_value=True
        ), mock.patch.object(handler, "ensure", return_value=None), \
                mock.patch.object(handler, "clear"), \
                mock.patch.object(
                    handler, "wait_for_code",
                    side_effect=ValueError("no code in callback"),
                ), \
                mock.patch.object(auth, "generate_pkce",
                                  return_value=("v", "c")), \
                mock.patch.object(auth, "save_verifier"), \
                mock.patch("builtins.input",
                           return_value="com.lidlplus.app://cb?code=pasted"), \
                mock.patch.object(
                    auth, "exchange_code",
                    return_value={"refresh_token": "r"},
                ) as exchange, \
                mock.patch.object(Config, "save"), \
                mock.patch.object(auth, "clear_verifier"):
            args = argparse.Namespace(
                receive=None, callback=None, code=None,
                country=None, language=None, no_handler=False,
            )
            self.assertEqual(cli.cmd_login(args), 0)
        # It recovered via the prompt rather than dying on the ValueError.
        exchange.assert_called_once_with("pasted", "v")

    def test_deposit_honours_the_registered_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "elsewhere.json"
            handler.deposit("com.lidlplus.app://callback?code=abc", target)
            self.assertEqual(json.loads(target.read_text()), {"code": "abc"})


if __name__ == "__main__":
    unittest.main()

"""Turn raw Lidl ticket JSON into a stable, analysis-friendly shape.

The raw payload is kept verbatim in the database, so this module is a pure
derived view: when a field turns out to be named differently than expected,
fix it here and run `lidl renormalize`, with no refetching needed.

Money is carried as integer cents throughout. Receipt amounts arrive as
locale-formatted strings ("2,19", "1.234,56"), and rounding those into floats
before aggregating is how cent-level drift creeps into a spend analysis.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .receipt_html import parse_receipt_html

# Candidate key names, in priority order. The Dutch payload has not been
# confirmed field-by-field yet, so each lookup accepts the variants seen
# across the German/Austrian and UK responses.
_ITEM_LIST_KEYS = ("itemsLine", "itemLines", "items", "lines")
_TOTAL_KEYS = ("totalAmount", "total", "amountTotal", "totalPrice")
_DATE_KEYS = ("date", "ticketDate", "purchaseDate", "issueDate")


def name_key(name: str) -> str:
    """The key an article name is matched on, everywhere.

    Computed in Python on purpose. SQLite's `lower()` only folds ASCII, so
    joining on `lower(name)` in SQL silently drops every article with an
    accent. "Âlde Fryske" stays capital-A-circumflex there while Python
    lowercases it, the join misses, and the article lands in the
    uncategorised bucket looking like an unknown product.
    """
    return name.strip().lower()


def _to_decimal(value: Any, *, grouped: bool = True) -> Decimal | None:
    """Parse a locale-formatted number: ``2,19``, ``1.234,56``, ``-0,21``.

    `grouped` says whether a lone dot may be a thousands separator. True for
    money, where "1.234" means one thousand. False for quantities, which are
    quoted with three decimals, so "0.792" is a weight in kilos, not 792 of
    something, and "1.000" is one item rather than a thousand.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, dict):
        for key in ("amount", "value", "total"):
            if key in value:
                return _to_decimal(value[key])
        return None

    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("-") or text.endswith("-")
    text = re.sub(r"[^\d.,]", "", text)
    if not text:
        return None

    if "." in text and "," in text:
        # Whichever separator comes last is the decimal one.
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif "." in text:
        # A lone dot is a decimal point only when 1-2 digits follow it;
        # otherwise it is a thousands separator ("1.234").
        tail = text.rsplit(".", 1)[1]
        if grouped and (len(tail) > 2 or text.count(".") > 1):
            text = text.replace(".", "")

    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -number if negative else number


def parse_amount(value: Any) -> int | None:
    """Parse a money value into integer cents."""
    number = _to_decimal(value)
    if number is None:
        return None
    return int((number * 100).to_integral_value(rounding=ROUND_HALF_UP))


def parse_quantity(value: Any) -> float | None:
    """Parse a quantity, keeping sub-cent precision.

    Weighed articles are quoted in kilograms ("0,792"), so this must not go
    through parse_amount: rounding to cents would turn 0.792 kg into 0.79.
    Nor may a lone dot be read as a thousands separator here. That turned
    0.792 kg of bananas into 792 and multiplied any derived amount by a
    thousand.
    """
    number = _to_decimal(value, grouped=False)
    return None if number is None else float(number)


def parse_date(value: Any) -> str | None:
    """Return an ISO-8601 string, or None when the value is unparseable."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt).isoformat()
        except ValueError:
            continue
    return None


def _first(data: dict, *keys: str) -> Any:
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, ""):
            return data[key]
    return None


def _normalize_store(raw: dict) -> dict:
    store = raw.get("store")
    if not isinstance(store, dict):
        store = {}
    return {
        "code": str(
            _first(store, "id", "code", "storeCode")
            or _first(raw, "storeCode", "storeId")
            or ""
        ),
        "name": str(_first(store, "name", "storeName") or ""),
        "address": str(_first(store, "address", "street") or ""),
        "postal_code": str(_first(store, "postalCode", "zipCode") or ""),
        "locality": str(_first(store, "locality", "city", "town") or ""),
    }


def _normalize_discounts(item: dict) -> list[dict]:
    raw_discounts = item.get("discounts")
    if not isinstance(raw_discounts, list):
        return []
    result = []
    for entry in raw_discounts:
        if not isinstance(entry, dict):
            continue
        amount = parse_amount(
            _first(entry, "amount", "discountAmount", "value")
        )
        result.append(
            {
                "description": str(
                    _first(entry, "description", "name", "title") or ""
                ),
                # Discounts are quoted as positive reductions; keep them that
                # way so `net = amount - discount` reads naturally.
                "amount_cents": abs(amount) if amount is not None else 0,
            }
        )
    return result


def _normalize_item(item: dict, line_no: int) -> dict:
    discounts = _normalize_discounts(item)
    discount_cents = sum(d["amount_cents"] for d in discounts)
    amount_cents = parse_amount(
        _first(item, "originalAmount", "totalAmount", "amount", "price")
    )
    unit_price_cents = parse_amount(
        _first(item, "currentUnitPrice", "unitPrice", "originalUnitPrice")
    )
    quantity = parse_quantity(_first(item, "quantity", "units", "amountUnits"))

    # Some lines quote only a unit price; derive the line amount so totals add
    # up regardless of which of the two the payload happens to carry.
    if amount_cents is None and unit_price_cents is not None and quantity:
        amount_cents = int(round(unit_price_cents * quantity))
    if unit_price_cents is None and amount_cents is not None and quantity:
        unit_price_cents = int(round(amount_cents / quantity))

    deposit_cents = parse_amount(item.get("deposit"))

    return {
        "line_no": line_no,
        "name": str(_first(item, "name", "description", "articleName") or ""),
        "quantity": quantity,
        "unit_price_cents": unit_price_cents,
        "amount_cents": amount_cents,
        "discount_cents": discount_cents,
        "net_cents": (
            None if amount_cents is None else amount_cents - discount_cents
        ),
        "is_weight": bool(item.get("isWeight", False)),
        "barcode": str(_first(item, "codeInput", "barcode", "ean") or ""),
        "tax_group": str(_first(item, "taxGroupName", "taxGroup") or ""),
        "deposit_cents": deposit_cents,
        "discounts": discounts,
    }


def _deposit_line(item: dict, line_no: int) -> dict | None:
    """Turn a nested deposit charge into a line of its own.

    On older (`ticketType: NATIVE`) receipts the deposit hangs inside the
    article that carries it (twelve bottles at 25 cents) and is counted in
    the receipt total but not in the article's own amount. Printed receipts
    put the same charge on a separate line. Emitting one here keeps both
    vintages the same shape, makes the line sums add up, and lets the deposit
    fall into its own spending category instead of inflating the groceries.
    """
    deposit = item.get("deposit")
    if not isinstance(deposit, dict):
        return None
    amount = parse_amount(deposit.get("amount"))
    if not amount:
        return None

    description = str(deposit.get("description") or "").strip()
    return {
        "line_no": line_no,
        # Named so the category rules recognise it as deposit, the way the
        # printed receipts' own "statiegeld" lines are recognised.
        "name": f"Statiegeld {description}".strip(),
        "quantity": parse_quantity(deposit.get("quantity")),
        "unit_price_cents": parse_amount(deposit.get("unitPrice")),
        "amount_cents": amount,
        "discount_cents": 0,
        "net_cents": amount,
        "is_weight": False,
        "barcode": "",
        "tax_group": str(deposit.get("taxGroupName") or ""),
        "deposit_cents": amount,
        "discounts": [],
    }


def normalize_ticket(raw: dict) -> dict:
    """Map a raw v3 ticket payload onto the canonical receipt shape."""
    warnings: list[str] = []

    items_raw = None
    for key in _ITEM_LIST_KEYS:
        candidate = raw.get(key)
        if isinstance(candidate, list) and candidate:
            items_raw = candidate
            break

    if items_raw is None:
        html = raw.get("htmlPrintedReceipt")
        if html:
            # NL (and UK) ship the printed receipt instead of a line-item
            # array; the parser returns the same shape, so the code below
            # does not care which country the receipt came from.
            items_raw = parse_receipt_html(html)
            if not items_raw:
                warnings.append(
                    "htmlPrintedReceipt present but no article lines parsed"
                )
        else:
            items_raw = []
            warnings.append(
                "no line-item array found under any of: "
                + ", ".join(_ITEM_LIST_KEYS)
            )

    items: list[dict] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        items.append(_normalize_item(entry, len(items)))
        deposit = _deposit_line(entry, len(items))
        if deposit is not None:
            items.append(deposit)

    total_cents = parse_amount(_first(raw, *_TOTAL_KEYS))
    if total_cents is None and items:
        total_cents = sum(i["net_cents"] or 0 for i in items)
        warnings.append("total missing from payload; summed from line items")

    date = parse_date(_first(raw, *_DATE_KEYS))
    if date is None:
        warnings.append("could not parse a receipt date")

    currency = raw.get("currency")
    if isinstance(currency, dict):
        currency_code = str(currency.get("code") or "")
    else:
        currency_code = str(currency or "")

    return {
        "id": str(raw.get("id") or ""),
        "date": date,
        "currency": currency_code or "EUR",
        "store": _normalize_store(raw),
        "total_cents": total_cents,
        "items": items,
        "warnings": warnings,
    }


def to_export_dict(receipt: dict) -> dict:
    """Render a normalized receipt with euro amounts, for JSON export."""

    def euros(cents: int | None) -> float | None:
        return None if cents is None else round(cents / 100, 2)

    return {
        "id": receipt["id"],
        "date": receipt["date"],
        "currency": receipt["currency"],
        "store": receipt["store"]["name"] or receipt["store"]["code"],
        "storeDetails": receipt["store"],
        "total": euros(receipt["total_cents"]),
        "items": [
            {
                "name": item["name"],
                "quantity": item["quantity"],
                "unitPrice": euros(item["unit_price_cents"]),
                "amount": euros(item["amount_cents"]),
                "net": euros(item["net_cents"]),
                "barcode": item["barcode"],
                "isWeight": item["is_weight"],
                "taxGroup": item["tax_group"],
                "deposit": euros(item["deposit_cents"]),
                "discounts": [
                    {
                        "description": d["description"],
                        "amount": euros(d["amount_cents"]),
                    }
                    for d in item["discounts"]
                ],
            }
            for item in receipt["items"]
        ],
    }

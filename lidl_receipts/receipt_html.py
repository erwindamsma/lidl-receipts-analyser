"""Parse the printed-receipt HTML that the API returns for some countries.

The Dutch API sets `ticketType: "HTML"` and carries no line-item array at all:
everything sits in `htmlPrintedReceipt`, a monospace rendering of the paper
receipt. It is mechanical enough to parse reliably, because every span carries
`data-*` attributes with the underlying values:

    <span id="purchase_list_line_2" class="article" data-art-id="0080000"
          data-art-quantity="0,792" data-unit-price="1,39" data-tax-type="B"
          data-art-description="Bananen">Bananen        1,10 B</span>

One receipt line is split across several spans that share an id, so spans are
grouped by that id first. The output is shaped like the `itemsLine` array that
other countries return, which lets the rest of the pipeline stay identical.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

_LINE_ID = re.compile(r"^purchase_list_line_(\d+)$")

# A trailing money amount, optionally followed by the tax-group letter:
#   "Watermeloen            4,99 B" -> ("4,99", "B")
#   "  Lidl Plus korting   -2,00"   -> ("-2,00", None)
# Either separator: the same printed-receipt HTML reaches Britain and Ireland
# with dots, and requiring a comma silently yields zero articles there.
_TRAILING_AMOUNT = re.compile(r"(-?\d[\d.,]*[.,]\d{2})\s*([A-Z])?\s*$")

# Weighed articles print a second line repeating the same data attributes:
#   "  0,792 kg x 1,39   EUR/kg"   (also seen as "EUR" and "EUR/kg" spacing
#                                   variants across receipt vintages)
# Such a line is recognised by having no amount of its own rather than by its
# wording, which is what keeps the spelling variants from splitting an article
# into two. This only marks it as sold by weight.
_WEIGHT_HINT = re.compile(r"\bkg\b")


class _Line:
    __slots__ = ("text", "classes", "attrs")

    def __init__(self) -> None:
        self.text = ""
        self.classes: set[str] = set()
        self.attrs: dict[str, str] = {}


class _PurchaseListParser(HTMLParser):
    """Collect the spans of the purchase list, grouped per receipt line."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: dict[int, _Line] = {}
        self._open: list[int | None] = []

    def handle_starttag(self, tag, attrs):
        if tag != "span":
            return
        attr_map = {k: v or "" for k, v in attrs}
        match = _LINE_ID.match(attr_map.get("id", ""))
        if not match:
            self._open.append(None)
            return

        number = int(match.group(1))
        line = self.lines.setdefault(number, _Line())
        line.classes.update(attr_map.get("class", "").split())
        for key, value in attr_map.items():
            if key.startswith("data-"):
                line.attrs[key] = value
        self._open.append(number)

    def handle_endtag(self, tag):
        if tag == "span" and self._open:
            self._open.pop()

    def handle_data(self, data):
        if self._open and self._open[-1] is not None:
            self.lines[self._open[-1]].text += data


def _split_amount(text: str) -> tuple[str | None, str | None, str]:
    """Split a line into (amount, tax group, remaining description)."""
    match = _TRAILING_AMOUNT.search(text)
    if not match:
        return None, None, text.strip()
    return match.group(1), match.group(2), text[: match.start()].strip()


def _clean_name(description: str, art_id: str) -> str:
    """Non-food articles print as "Bandschuurmachine-0502092"; drop the id."""
    suffix = f"-{art_id}"
    if art_id and description.endswith(suffix):
        return description[: -len(suffix)].strip()
    return description.strip()


# A self-scan correction receipt lists what the scanner missed as one lump sum
# ("Gemiste artikelen ... Food 107,48") and then itemises what was actually in
# the trolley under "Extra artikelen". Only the lump sum reaches the purchase
# list, so without this the whole shop collapses into one meaningless article
# and the totals still reconcile, which is why `verify` cannot catch it.
_CORRECTION_HEADING = re.compile(r"Extra artikelen", re.IGNORECASE)
_HEADER_LINE = re.compile(r'<span id="header_line_(\d+)"[^>]*>(.*?)</span>')
_CORR_ARTICLE = re.compile(r"^(.+?)\s+(\d+)\s*x\s*([\d.,]+)\s+(-?[\d.,]+)$")
_CORR_WEIGHT = re.compile(r"^\s*([\d.,]+)\s*kg\s*x\s*([\d.,]+)\s*$")
_CORR_PLAIN = re.compile(r"^(.+?)\s{2,}(-?[\d.,]+)$")


def _header_lines(html: str) -> list[str]:
    """Reassemble the receipt header, one string per printed line."""
    joined: dict[int, str] = {}
    for match in _HEADER_LINE.finditer(html):
        number = int(match.group(1))
        joined[number] = joined.get(number, "") + unescape(match.group(2))
    return [joined[n].rstrip() for n in sorted(joined)]


def _correction_items(html: str) -> list[dict]:
    """Parse the itemised block of a self-scan correction receipt."""
    lines = _header_lines(html)
    start = next(
        (i for i, text in enumerate(lines) if _CORRECTION_HEADING.search(text)),
        None,
    )
    if start is None:
        return []

    block: list[str] = []
    for text in lines[start + 1:]:
        stripped = text.strip()
        if stripped and set(stripped) == {"-"}:
            break
        if stripped:
            block.append(text)

    # The block carries no per-line tax group. When the receipt's VAT summary
    # names exactly one, every line must belong to it.
    groups = set(re.findall(r'data-tax-type="([A-Z])"', html))
    tax_group = groups.pop() if len(groups) == 1 else ""

    items: list[dict] = []
    index = 0
    while index < len(block):
        text = block[index]

        article = _CORR_ARTICLE.match(text)
        if article:
            name, quantity, unit_price = (
                article.group(1).strip(),
                article.group(2),
                article.group(3),
            )
            items.append(
                _correction_item(name, quantity, unit_price, None, tax_group, False)
            )
            index += 1
            continue

        # A weighed article prints its amount first and the kilo breakdown on
        # the next line.
        plain = _CORR_PLAIN.match(text)
        weight = _CORR_WEIGHT.match(block[index + 1]) if index + 1 < len(block) else None
        if plain and weight:
            items.append(
                _correction_item(
                    plain.group(1).strip(),
                    weight.group(1),
                    weight.group(2),
                    plain.group(2),
                    tax_group,
                    True,
                )
            )
            index += 2
            continue

        index += 1

    return items


def _correction_item(
    name: str,
    quantity: str,
    unit_price: str,
    amount: str | None,
    tax_group: str,
    is_weight: bool,
) -> dict:
    return {
        "name": name,
        "quantity": quantity,
        "currentUnitPrice": unit_price,
        # Leave the amount to be derived from quantity x unit price where the
        # receipt does not print one: the printed column is narrow enough to
        # clip a leading digit ("107,48" prints as "07,48").
        "originalAmount": amount,
        "codeInput": "",
        "taxGroupName": tax_group,
        "isWeight": is_weight,
        "discounts": [],
    }


def parse_receipt_html(html: str) -> list[dict]:
    """Return items shaped like the `itemsLine` array of other countries."""
    corrected = _correction_items(html)
    if corrected:
        return corrected

    parser = _PurchaseListParser()
    parser.feed(html)
    parser.close()

    items: list[dict] = []
    current: dict | None = None

    for number in sorted(parser.lines):
        line = parser.lines[number]
        text = line.text.rstrip()
        if not text.strip():
            continue

        # Column header ("OMSCHRIJVING ... EUR").
        if "currency" in line.classes:
            continue

        art_id = line.attrs.get("data-art-id", "")
        if art_id:
            amount, tax_group, _ = _split_amount(text)
            if amount is None:
                # Continuation of the article above rather than a new one:
                # it repeats data-art-id but carries no amount of its own.
                if current is not None and _WEIGHT_HINT.search(text):
                    current["isWeight"] = True
                continue

            current = {
                "name": _clean_name(
                    line.attrs.get("data-art-description", ""), art_id
                ),
                "quantity": line.attrs.get("data-art-quantity") or "1",
                "currentUnitPrice": line.attrs.get("data-unit-price"),
                "originalAmount": amount,
                "codeInput": art_id,
                "taxGroupName": tax_group or line.attrs.get("data-tax-type", ""),
                "isWeight": False,
                "discounts": [],
            }
            items.append(current)
            continue

        amount, tax_group, description = _split_amount(text)
        if amount is None:
            continue

        is_discount = (
            "discount" in line.classes
            or "data-promotion-id" in line.attrs
            # Price reductions ("In prijs verlaagd") carry no class at all.
            or not line.classes
        )
        if is_discount and current is not None:
            current["discounts"].append(
                {"description": description, "amount": amount}
            )
            continue

        # A standalone line rather than a modifier on the article above:
        # returned deposit ("[X] Emballage") is the case seen in practice.
        # Attaching it to the previous article would misattribute the money.
        items.append(
            {
                "name": description,
                "quantity": "1",
                "currentUnitPrice": amount,
                "originalAmount": amount,
                "codeInput": "",
                "taxGroupName": tax_group or "",
                "isWeight": False,
                "discounts": [],
            }
        )
        current = None

    return items

#!/usr/bin/env python3
"""Render the spending analysis as a self-contained HTML report.

    python3 analysis/spending.py > analysis/spending.json
    python3 analysis/report.py   > analysis/report.html

Kept separate from spending.py so the numbers and their presentation stay
apart: rerun both after correcting categories.csv and the report follows.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

DATA = json.loads(
    Path(sys.argv[1] if len(sys.argv) > 1 else "analysis/spending.json").read_text()
)

MONTHS_NL = {
    "01": "jan", "02": "feb", "03": "mrt", "04": "apr",
    "05": "mei", "06": "jun", "07": "jul", "08": "aug",
    "09": "sep", "10": "okt", "11": "nov", "12": "dec",
}


def esc(text: object) -> str:
    return html.escape(str(text))


def eur(value: float, decimals: int = 2) -> str:
    """Format as Dutch currency: thousands dot, decimal comma."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def month_label(month: str) -> str:
    return MONTHS_NL.get(month[5:7], month)


def month_full(month: str) -> str:
    """"mrt 2021", unambiguous where a six-year chart repeats month names."""
    return f"{month_label(month)} {month[:4]}"


# --------------------------------------------------------------------------
# Charts. Hand-authored SVG: no chart library survives the artifact CSP, and
# these shapes are simple enough that the spec is easier to hold to directly.
# --------------------------------------------------------------------------

BAR_MAX = 24  # marks stay thin; the band's leftover is air
GAP = 2       # surface-coloured gap that separates touching marks


def stacked_columns(rows: list[dict]) -> str:
    """Monthly spend split into food and non-food."""
    width, height = 720, 280
    pad_l, pad_r, pad_t, pad_b = 40, 8, 34, 34
    plot_h = height - pad_t - pad_b
    plot_w = width - pad_l - pad_r

    top = max(r["food"] + r["non_food"] for r in rows)
    scale = max(1, round(top / 200 + 0.5) * 200)
    band = plot_w / len(rows)
    bar_w = min(BAR_MAX, band * 0.5)

    # Six years is seventy columns: a value on every cap and a month under
    # every column turns into a grey smear. Past a dozen columns, label the
    # peak and the year boundaries and let the tooltip carry the rest.
    dense = len(rows) > 12
    peak = max(range(len(rows)), key=lambda i: rows[i]["food"] + rows[i]["non_food"])

    parts = []
    for index in range(5):
        value = scale * index / 4
        y = pad_t + plot_h - plot_h * (value / scale)
        parts.append(
            f'<line class="grid" x1="{pad_l - 4}" y1="{y:.1f}" '
            f'x2="{width - pad_r}" y2="{y:.1f}"/>'
            f'<text class="tick" x="{pad_l - 8}" y="{y + 3.5:.1f}">'
            f"{eur(value, 0)}</text>"
        )

    for index, row in enumerate(rows):
        cx = pad_l + band * (index + 0.5)
        x = cx - bar_w / 2
        total = row["food"] + row["non_food"]
        h_food = plot_h * (row["food"] / scale)
        h_non = plot_h * (row["non_food"] / scale)
        y_food = pad_t + plot_h - h_food
        y_non = y_food - h_non - GAP

        parts.append(
            f'<g class="col" tabindex="0" '
            f'data-tip="{esc(month_full(row["month"]))} · voeding '
            f'€ {eur(row["food"])} · non-food € {eur(row["non_food"])}">'
            f'<rect class="hit" x="{cx - band / 2:.1f}" y="{pad_t}" '
            f'width="{band:.1f}" height="{plot_h}"/>'
            f'<rect class="s1" x="{x:.1f}" y="{y_food:.1f}" '
            f'width="{bar_w:.1f}" height="{max(0, h_food):.1f}" rx="0"/>'
        )
        if h_non > 0.5:
            parts.append(
                f'<rect class="s2" x="{x:.1f}" y="{y_non:.1f}" '
                f'width="{bar_w:.1f}" height="{h_non:.1f}" rx="3"/>'
            )
        if not dense or index == peak:
            parts.append(
                f'<text class="cap" x="{cx:.1f}" y="{min(y_food, y_non) - 8:.1f}">'
                f"€ {eur(total, 0)}</text>"
            )
        if dense:
            label = row["month"][:4] if row["month"][5:7] == "01" else ""
        else:
            label = month_label(row["month"])
        if label:
            parts.append(
                f'<text class="xlab" x="{cx:.1f}" y="{height - 12}">'
                f"{esc(label)}</text>"
            )
        parts.append("</g>")

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Uitgaven per maand, gesplitst in voeding en non-food">'
        + "".join(parts)
        + "</svg>"
    )


def horizontal_bars(rows: list[dict], value_key: str, label_key: str) -> str:
    """One hue for every bar: these categories have no natural order."""
    row_h, label_w = 30, 150
    width = 720
    height = row_h * len(rows) + 6
    top = max(r[value_key] for r in rows)

    # Reserve the value column from the widest label actually rendered, not a
    # guess: a four-digit amount ran past the viewBox and lost its last digit.
    longest = max(len(f"€ {eur(r[value_key])}") for r in rows)
    value_w = longest * 7 + 14
    track = width - label_w - value_w

    parts = []
    for index, row in enumerate(rows):
        y = index * row_h + 3
        bar_h = min(BAR_MAX - 6, row_h - 12)
        bar_y = y + (row_h - bar_h) / 2 - 3
        bar_w = track * (row[value_key] / top)
        parts.append(
            f'<g class="row" tabindex="0" '
            f'data-tip="{esc(row[label_key])} · € {eur(row[value_key])}">'
            f'<text class="rlab" x="{label_w - 10}" y="{y + row_h / 2 - 1:.1f}">'
            f"{esc(row[label_key])}</text>"
            f'<rect class="s1" x="{label_w}" y="{bar_y:.1f}" '
            f'width="{max(2, bar_w):.1f}" height="{bar_h}" rx="3"/>'
            f'<text class="rval" x="{label_w + bar_w + 10:.1f}" '
            f'y="{y + row_h / 2 - 1:.1f}">€ {eur(row[value_key])}</text></g>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Uitgaven per categorie">' + "".join(parts) + "</svg>"
    )


def diverging_bars(rows: list[dict]) -> str:
    """Price change per article, as a deviation from zero.

    A dumbbell of the two absolute prices was the first attempt, but it puts
    a 45-cent article and a 7-euro one on one axis and squashes most rows
    against the left. The question is how much the price moved, so the axis
    is the change itself, centred on no-change. Absolute prices ride along as
    the row's note.
    """
    row_h, label_w, note_w, right = 30, 156, 108, 56
    width = 720
    height = row_h * len(rows) + 26
    zero = label_w + note_w
    arm = (width - zero - right) / 2
    reach = max(abs(r["change_pct"]) for r in rows)

    parts = [
        f'<line class="zero" x1="{zero + arm:.1f}" y1="16" '
        f'x2="{zero + arm:.1f}" y2="{height - 8}"/>',
        f'<text class="axhead" x="{zero + arm - 8:.1f}" y="10">goedkoper</text>',
        f'<text class="axhead axhead-r" x="{zero + arm + 8:.1f}" y="10">duurder</text>',
    ]
    for index, row in enumerate(rows):
        y = index * row_h + 26
        bar_h = min(BAR_MAX - 8, row_h - 14)
        bar_y = y - bar_h / 2
        length = arm * (abs(row["change_pct"]) / reach)
        up = row["change_pct"] > 0
        x = zero + arm if up else zero + arm - length
        tone = "up" if up else "down"
        label_x = x + length + 8 if up else x - 8
        parts.append(
            f'<g class="row" tabindex="0" '
            f'data-tip="{esc(row["name"])} · {esc(row["first_month"])} '
            f'€ {eur(row["first_price"])} → {esc(row["last_month"])} '
            f'€ {eur(row["last_price"])}">'
            f'<text class="rlab" x="{label_w - 10}" y="{y + 4:.1f}">'
            f"{esc(row['name'])}</text>"
            f'<text class="rnote" x="{label_w + 6}" y="{y + 4:.1f}">'
            f"€ {eur(row['first_price'])} → € {eur(row['last_price'])}</text>"
            f'<rect class="{tone}-f" x="{x:.1f}" y="{bar_y:.1f}" '
            f'width="{max(2, length):.1f}" height="{bar_h}" rx="3"/>'
            f'<text class="rval {tone}-t" x="{label_x:.1f}" y="{y + 4:.1f}">'
            f"{'+' if up else ''}{row['change_pct']:.0f}%</text></g>"
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Prijsverandering per artikel ten opzichte van de eerste '
        f'aankoop">' + "".join(parts) + "</svg>"
    )


def leader_rows(rows: list[tuple[str, str, str]]) -> str:
    """Label / dotted leader / amount, the till roll's own rhythm."""
    return "".join(
        f'<li><span class="lead-label">{esc(a)}</span>'
        f'<span class="lead-dots" aria-hidden="true"></span>'
        f'<span class="lead-note">{esc(b)}</span>'
        f'<span class="lead-value">{esc(c)}</span></li>'
        for a, b, c in rows
    )


def table(headers: list[str], rows: list[list[str]], caption: str) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="tablewrap"><table><caption>{esc(caption)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def build() -> str:
    meta = DATA["meta"]
    deposit = DATA["deposit"]
    inflation = DATA["inflation"]
    by_month = DATA["by_month"]
    non_food_by_month = DATA["non_food_by_month"]
    categories = [r for r in DATA["by_category"] if r["spend"] > 0]

    spend_total = sum(r["spend"] for r in by_month)
    discount_total = sum(r["discount"] for r in DATA["discounts_by_month"])
    gross_total = sum(r["gross"] for r in DATA["discounts_by_month"])
    non_food_total = sum(r["non_food"] for r in non_food_by_month)
    trips = sum(r["trips"] for r in by_month)
    per_trip = spend_total / trips if trips else 0
    uncategorised = DATA["uncategorised"]

    change = inflation["weighted_change_pct"] or 0.0
    change_word = "vrijwel vlak" if abs(change) < 0.5 else (
        "duurder" if change > 0 else "goedkoper"
    )

    kpis = [
        ("Uitgegeven", f"€ {eur(spend_total)}",
         f"{meta['receipts']} bonnen, statiegeld eruit"),
        ("Per bon", f"€ {eur(per_trip)}", f"over {trips} winkelbezoeken"),
        ("Korting ontvangen", f"€ {eur(discount_total)}",
         f"{discount_total / gross_total * 100:.1f}% van het brutobedrag"),
        ("Prijsverandering", f"{change:+.2f}%",
         f"{change_word}, gewogen over {inflation['articles']} artikelen"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><p class="kpi-label">{esc(label)}</p>'
        f'<p class="kpi-value">{esc(value)}</p>'
        f'<p class="kpi-note">{esc(note)}</p></div>'
        for label, value, note in kpis
    )

    month_rows = [
        {
            "month": row["month"],
            "food": next(
                m["food"] for m in non_food_by_month if m["month"] == row["month"]
            ),
            "non_food": next(
                m["non_food"] for m in non_food_by_month if m["month"] == row["month"]
            ),
        }
        for row in by_month
    ]

    top_discounted = leader_rows(
        [
            (r["name"], f"{r['times']}x", f"€ {eur(r['discount'])}")
            for r in DATA["top_discounted"][:10]
        ]
    )
    top_articles = leader_rows(
        [
            (
                r["name"],
                r["category"]
                + (
                    f" · {r['variants']} schrijfwijzen"
                    if r.get("variants", 1) > 1
                    else ""
                ),
                f"€ {eur(r['spend'])}",
            )
            for r in DATA["top_articles"][:12]
        ]
    )

    non_food_table = table(
        ["Artikel", "Datum", "Bedrag"],
        [[r["name"], r["date"], f"€ {eur(r['net'])}"] for r in DATA["non_food"][:12]],
        "Grootste non-food aankopen (21% btw)",
    )

    month_table = table(
        ["Maand", "Voeding", "Non-food", "Totaal"],
        [
            [
                month_full(r["month"]),
                f"€ {eur(r['food'])}",
                f"€ {eur(r['non_food'])}",
                f"€ {eur(r['food'] + r['non_food'])}",
            ]
            for r in month_rows
        ],
        "Uitgaven per maand",
    )

    movers = inflation["risers"][:8] + list(reversed(inflation["fallers"][:8]))

    # Prose that states a fact has to read that fact from the data, or it goes
    # stale the moment the dataset grows, which is exactly what happened when
    # this report went from eight months to six years.
    per_year: dict[str, dict[str, float]] = {}
    for row in non_food_by_month:
        year = per_year.setdefault(row["month"][:4], {"food": 0.0, "non": 0.0, "n": 0})
        year["food"] += row["food"]
        year["non"] += row["non_food"]
        year["n"] += 1
    full = {y: v for y, v in per_year.items() if v["n"] >= 12} or per_year
    years = sorted(full)
    first_year, last_year = years[0], years[-1]
    shares = {
        y: v["non"] / (v["food"] + v["non"]) * 100
        for y, v in full.items()
        if v["food"] + v["non"] > 0
    }
    low_year = min(shares, key=shares.get)
    high_year = max(shares, key=shares.get)

    ranked = [r["category"] for r in categories]
    non_food_rank = ranked.index("non-food") + 1 if "non-food" in ranked else 0

    # One bar per month is unreadable across six years, and a single bar for a
    # single year is not a chart at all, so the grain follows the span.
    discounts = DATA["discounts_by_month"]
    if len(discounts) > 18:
        per_year: dict[str, float] = {}
        for row in discounts:
            per_year[row["month"][:4]] = per_year.get(row["month"][:4], 0) + row["discount"]
        discount_rows = [{"label": y, "discount": round(v, 2)} for y, v in sorted(per_year.items())]
        discount_grain = "per jaar"
    else:
        discount_rows = [
            {"label": month_label(r["month"]), "discount": r["discount"]} for r in discounts
        ]
        discount_grain = "per maand"

    return TEMPLATE.format(
        period=f"{meta['first_date']} t/m {meta['last_date']}",
        kpis=kpi_html,
        chart_months=stacked_columns(month_rows),
        month_table=month_table,
        chart_categories=horizontal_bars(categories, "spend", "category"),
        chart_movers=diverging_bars(movers),
        chart_discounts=horizontal_bars(discount_rows, "discount", "label"),
        top_discounted=top_discounted,
        top_articles=top_articles,
        non_food_table=non_food_table,
        non_food_total=eur(non_food_total),
        non_food_pct=f"{non_food_total / spend_total * 100:.0f}",
        deposit_paid=eur(deposit["paid"]),
        deposit_returned=eur(deposit["returned"]),
        deposit_net=eur(deposit["net"]),
        uncat_spend=eur(uncategorised["spend"]),
        uncat_pct=f"{uncategorised['spend'] / spend_total * 100:.0f}",
        receipts=meta["receipts"],
        first_year=first_year,
        last_year=last_year,
        food_first=eur(full[first_year]["food"], 0),
        food_last=eur(full[last_year]["food"], 0),
        low_year=low_year,
        high_year=high_year,
        share_low=f"{shares[low_year]:.0f}",
        share_high=f"{shares[high_year]:.0f}",
        top_category=categories[0]["category"][:1].upper() + categories[0]["category"][1:],
        top_spend=eur(categories[0]["spend"]),
        non_food_rank=non_food_rank,
        discount_grain=discount_grain,
        rise_count=len(inflation["risers"]),
        fall_count=len(inflation["fallers"]),
    )


TEMPLATE = """<title>Waar het geld heen gaat</title>
<style>
:root {{
  color-scheme: light;
  --paper:   #f4f4f1;
  --surface: #fcfcfb;
  --ink:     #16160f;
  --ink-2:   #52514e;
  --ink-3:   #86847c;
  --rule:    #e1e0d9;
  --axis:    #c3c2b7;
  --s1:      #2a78d6;
  --s2:      #eb6834;
  --up:      #e34948;
  --down:    #2a78d6;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --paper:   #0d0d0d;
    --surface: #1a1a19;
    --ink:     #f7f7f2;
    --ink-2:   #c3c2b7;
    --ink-3:   #8f8d85;
    --rule:    #2c2c2a;
    --axis:    #383835;
    --s1:      #3987e5;
    --s2:      #d95926;
    --up:      #e66767;
    --down:    #3987e5;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --paper:   #0d0d0d;
  --surface: #1a1a19;
  --ink:     #f7f7f2;
  --ink-2:   #c3c2b7;
  --ink-3:   #8f8d85;
  --rule:    #2c2c2a;
  --axis:    #383835;
  --s1:      #3987e5;
  --s2:      #d95926;
  --up:      #e66767;
  --down:    #3987e5;
}}

* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}}
.wrap {{
  max-width: 860px;
  margin: 0 auto;
  padding: 56px 24px 96px;
  display: flex;
  flex-direction: column;
  gap: 56px;
}}

.eyebrow {{
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 0;
}}
h1 {{
  font-size: clamp(30px, 5vw, 42px);
  line-height: 1.12;
  letter-spacing: -0.02em;
  margin: 10px 0 0;
  text-wrap: balance;
}}
h2 {{
  font-size: 21px;
  letter-spacing: -0.01em;
  margin: 0;
  text-wrap: balance;
}}
p {{ margin: 0; max-width: 66ch; }}
.lede {{ color: var(--ink-2); margin-top: 14px; }}
.sub  {{ color: var(--ink-2); font-size: 15px; }}

header {{ border-bottom: 1px solid var(--rule); padding-bottom: 28px; }}

.kpis {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(178px, 1fr));
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule);
}}
.kpi {{ background: var(--surface); padding: 18px 18px 16px; }}
.kpi-label {{
  font-family: var(--mono); font-size: 10.5px; letter-spacing: 0.13em;
  text-transform: uppercase; color: var(--ink-3); margin: 0 0 8px;
}}
.kpi-value {{ font-size: 27px; font-weight: 600; letter-spacing: -0.02em; margin: 0; }}
.kpi-note {{ font-size: 12.5px; color: var(--ink-2); margin: 6px 0 0; line-height: 1.45; }}

section {{ display: flex; flex-direction: column; gap: 18px; }}
.card {{
  background: var(--surface);
  border: 1px solid var(--rule);
  padding: 22px 20px 14px;
  overflow-x: auto;
}}
svg {{ display: block; width: 100%; height: auto; min-width: 520px; }}

.grid  {{ stroke: var(--rule); stroke-width: 1; }}
.tick  {{ fill: var(--ink-3); font-family: var(--mono); font-size: 10px; text-anchor: end; }}
.xlab  {{ fill: var(--ink-2); font-family: var(--mono); font-size: 11px; text-anchor: middle; }}
.cap   {{ fill: var(--ink); font-family: var(--mono); font-size: 11px; font-weight: 600; text-anchor: middle; }}
.rlab  {{ fill: var(--ink-2); font-size: 12.5px; text-anchor: end; }}
.rval  {{ fill: var(--ink); font-family: var(--mono); font-size: 11.5px; }}
svg .s1 {{ fill: var(--s1); }}
svg .s2 {{ fill: var(--s2); }}
.hit   {{ fill: transparent; }}
.zero  {{ stroke: var(--axis); stroke-width: 1; }}
.axhead {{ fill: var(--ink-3); font-family: var(--mono); font-size: 10px;
          letter-spacing: 0.1em; text-transform: uppercase; text-anchor: end; }}
.axhead-r {{ text-anchor: start; }}
.rnote {{ fill: var(--ink-3); font-family: var(--mono); font-size: 10.5px; }}
svg .up-f   {{ fill: var(--up); }}
svg .down-f {{ fill: var(--down); }}
.up-t   {{ fill: var(--ink); text-anchor: start; }}
.down-t {{ fill: var(--ink); text-anchor: end; }}
.row:hover .rlab, .col:hover .xlab,
.row:focus-visible .rlab, .col:focus-visible .xlab {{ fill: var(--ink); }}
.row, .col {{ cursor: default; outline: none; }}
.row:focus-visible .s1, .col:focus-visible .s1 {{ stroke: var(--ink); stroke-width: 2; }}

.legend {{ display: flex; gap: 18px; flex-wrap: wrap; padding: 0 0 4px; margin: 0; list-style: none; }}
.legend li {{ display: flex; align-items: center; gap: 7px; font-size: 13px; color: var(--ink-2); }}
.swatch {{ width: 11px; height: 11px; border-radius: 2px; flex: none; }}

.leaders {{ list-style: none; margin: 0; padding: 0; }}
.leaders li {{
  display: flex; align-items: baseline; gap: 8px;
  padding: 7px 0; border-bottom: 1px solid var(--rule);
}}
.leaders li:last-child {{ border-bottom: 0; }}
.lead-label {{ flex: none; max-width: 58%; }}
.lead-dots {{
  flex: 1 1 auto; min-width: 16px; align-self: center;
  border-bottom: 1px dotted var(--axis);
}}
.lead-note {{ flex: none; font-size: 12px; color: var(--ink-3); font-family: var(--mono); }}
.lead-value {{
  flex: none; font-family: var(--mono); font-variant-numeric: tabular-nums;
  min-width: 78px; text-align: right;
}}

.tablewrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
caption {{
  text-align: left; font-family: var(--mono); font-size: 10.5px;
  letter-spacing: 0.13em; text-transform: uppercase; color: var(--ink-3);
  padding-bottom: 10px;
}}
th, td {{ text-align: left; padding: 8px 12px 8px 0; border-bottom: 1px solid var(--rule); }}
th {{ font-size: 11.5px; font-weight: 600; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.06em; }}
td:not(:first-child), th:not(:first-child) {{
  text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums;
}}
details {{ border-top: 1px solid var(--rule); padding-top: 12px; }}
summary {{
  cursor: pointer; font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3);
}}
summary:focus-visible {{ outline: 2px solid var(--s1); outline-offset: 3px; }}
details[open] summary {{ padding-bottom: 14px; }}

.note {{
  border-left: 2px solid var(--s2);
  padding: 2px 0 2px 16px;
  color: var(--ink-2);
  font-size: 15px;
}}
footer {{ border-top: 1px solid var(--rule); padding-top: 24px; color: var(--ink-3); font-size: 13.5px; }}
footer p {{ max-width: 68ch; }}
footer p + p {{ margin-top: 10px; }}
code {{ font-family: var(--mono); font-size: 0.92em; color: var(--ink-2); }}

#tip {{
  position: fixed; z-index: 10; pointer-events: none; opacity: 0;
  transition: opacity .12s; background: var(--ink); color: var(--surface);
  font-family: var(--mono); font-size: 11.5px; padding: 6px 9px;
  border-radius: 3px; max-width: 280px;
}}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}

/* Op papier: altijd het lichte palet, en niets mag over een paginarand vallen.
   De kaarten scrollen op het scherm horizontaal; in print bestaat scrollen niet,
   dus daar moet de SVG juist meekrimpen in plaats van afgekapt te worden. */
@media print {{
  :root {{
    --paper: #ffffff; --surface: #ffffff; --ink: #111111;
    --ink-2: #444444; --ink-3: #6b6b6b; --rule: #d8d7d0; --axis: #b4b3aa;
  }}
  @page {{ size: A4; margin: 14mm 12mm 16mm; }}
  body {{ background: #fff; font-size: 10.5pt; }}
  .wrap {{ max-width: none; padding: 0; gap: 22px; }}
  h1 {{ font-size: 26pt; }}
  h2 {{ font-size: 14pt; break-after: avoid; }}
  .card {{ overflow: visible; padding: 14px 12px 8px; }}
  svg {{ min-width: 0; }}
  section, .card, .kpis, table, .leaders li {{ break-inside: avoid; }}
  thead {{ display: table-header-group; }}
  details, #tip {{ display: none; }}
  a {{ text-decoration: none; color: inherit; }}
}}
</style>

<div class="wrap">
  <header>
    <p class="eyebrow">Lidl Plus · {period}</p>
    <h1>Waar het geld heen gaat</h1>
    <p class="lede">Uit {receipts} kassabonnen, regel voor regel. Statiegeld telt niet
    als uitgave: dat is rondpompen. Categorieën komen uit de gedeelde dataset,
    de regels in de code en je eigen correcties in <code>categories.csv</code>.</p>
  </header>

  <div class="kpis">{kpis}</div>

  <section>
    <h2>Per maand, voeding tegenover non-food</h2>
    <p class="sub">Wat je aan eten uitgeeft groeit gestaag: van
    € {food_first} in {first_year} naar € {food_last} in {last_year}. Het
    grillige deel is non-food, van {share_low}% van je uitgaven in {low_year}
    tot {share_high}% in {high_year}. Dat zijn losse aankopen, geen
    boodschappen.</p>
    <div class="card">
      <ul class="legend">
        <li><span class="swatch" style="background:var(--s1)"></span>Voeding (9% btw)</li>
        <li><span class="swatch" style="background:var(--s2)"></span>Non-food (21% btw)</li>
      </ul>
      {chart_months}
    </div>
    <details><summary>Cijfers</summary>{month_table}</details>
  </section>

  <section>
    <h2>Per categorie</h2>
    <p class="sub">{top_category} is je grootste post met € {top_spend}.
    Non-food staat op plek {non_food_rank}: over één jaar gemeten leek dat je
    grootste uitgave, maar over zes jaar valt één naaimachine weg tegen een
    gestage stroom boodschappen.</p>
    <div class="card">{chart_categories}</div>
    <p class="note">€ {uncat_spend} ({uncat_pct}% van het totaal) staat onder
    “overig voeding”: artikelen waar geen categorieregel op paste. De namen uit
    de recentste jaargang zijn met de hand nagelopen, de oudere niet. Daar valt
    dus nog het meeste te winnen.</p>
  </section>

  <section>
    <h2>Wat er duurder en goedkoper werd</h2>
    <p class="sub">Per artikel de prijs bij de eerste aankoop tegenover die bij
    de laatste. Gewogen over je hele mandje is het verschil vrijwel nul: er zijn
    net zo goed dalers als stijgers. Vergeleken op artikelnummer, niet op naam,
    want dezelfde naam dekt meerdere verpakkingsmaten.</p>
    <div class="card">
      <ul class="legend">
        <li><span class="swatch" style="background:var(--down)"></span>Goedkoper geworden</li>
        <li><span class="swatch" style="background:var(--up)"></span>Duurder geworden</li>
      </ul>
      {chart_movers}
    </div>
  </section>

  <section>
    <h2>Kortingen</h2>
    <p class="sub">Wat Lidl Plus-kortingen en prijsverlagingen {discount_grain}
    opleverden.</p>
    <div class="card">{chart_discounts}</div>
    <ul class="leaders">{top_discounted}</ul>
  </section>

  <section>
    <h2>Non-food: de uitschieters</h2>
    <p class="sub">€ {non_food_total}, oftewel {non_food_pct}% van al je
    uitgaven, zit in het 21%-tarief. Bijna alles daarin is een eenmalige
    aankoop.</p>
    {non_food_table}
  </section>

  <section>
    <h2>Waar het meeste geld naartoe ging</h2>
    <p class="sub">Per product, niet per kassanaam. De kassa schrijft hetzelfde
    artikel op meerdere manieren en geeft het onderweg nieuwe artikelnummers.
    Mineraalwater staat er onder zes namen en vier nummers in. Wie op naam
    telt, ziet elk product kleiner dan het is.</p>
    <ul class="leaders">{top_articles}</ul>
  </section>

  <footer>
    <p><strong>Statiegeld.</strong> € {deposit_paid} betaald aan flessen en
    blikken, € {deposit_returned} teruggebracht, netto € {deposit_net}. Het staat
    wel op de bon maar is geen uitgave, dus het is overal uit de totalen gelaten.</p>
    <p><strong>Prijsvergelijking.</strong> Per artikel de prijs bij de eerste
    aankoop tegenover die bij de laatste, gewogen naar wat het artikel in je
    mandje kost. Let op wat dat wel en niet meet: het interval verschilt per
    artikel. Iets dat je alleen in 2021 en 2022 kocht wordt over twee jaar
    gemeten, iets dat je nog steeds koopt over zes. Het is dus geen
    inflatiecijfer over een vaste periode, en een aanbiedingsprijs als eerste
    of laatste waarneming trekt de uitslag scheef. Artikelen die je maar in één
    maand kocht doen niet mee.</p>
    <p><strong>Herkomst.</strong> Gegenereerd uit <code>data/receipts.db</code>
    met <code>analysis/spending.py</code> en <code>analysis/report.py</code>.
    Elke bon is gecontroleerd met <code>lidl verify</code>: de artikelregels
    tellen tot op de cent op tot het totaal dat Lidl zelf teruggeeft.</p>
  </footer>
</div>

<div id="tip" role="status"></div>
<script>
const tip = document.getElementById('tip');
function show(event) {{
  const host = event.target.closest('[data-tip]');
  if (!host) return;
  tip.textContent = host.dataset.tip;
  tip.style.opacity = '1';
  const box = host.getBoundingClientRect();
  const x = Math.min(box.left + box.width / 2, window.innerWidth - 150);
  tip.style.left = Math.max(12, x - tip.offsetWidth / 2) + 'px';
  tip.style.top = Math.max(8, box.top - tip.offsetHeight - 8) + 'px';
}}
function hide() {{ tip.style.opacity = '0'; }}
for (const node of document.querySelectorAll('[data-tip]')) {{
  node.addEventListener('mouseenter', show);
  node.addEventListener('mouseleave', hide);
  node.addEventListener('focus', show);
  node.addEventListener('blur', hide);
}}
</script>
"""


if __name__ == "__main__":
    sys.stdout.write(build())

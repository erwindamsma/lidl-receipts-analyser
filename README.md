# lidl-receipts-analyser

Fetches your Lidl Plus receipts with every line item, stores them in SQLite and
analyses them offline. No dependencies, just the Python standard library (3.11+).

*[Nederlandse versie](README.nl.md)*

![The dashboard, filled with invented demo data](docs/dashboard.png)

## Quick start

```bash
git clone https://github.com/erwindamsma/lidl-receipts-analyser
cd lidl-receipts-analyser

./lidl.py config --country NL --language nl   # one-time setup
./lidl.py login                               # one-time login
./lidl.py sync                                # every receipt into SQLite
./lidl.py categories                          # sort articles into categories
python3 analysis/dashboard.py                 # -> analysis/dashboard.html
xdg-open analysis/dashboard.html              # or: explorer.exe analysis\dashboard.html
```

- Data lands in `./data/`: `receipts.db` plus `raw/<id>.json`.
- Refresh token in `~/.config/lidl-receipts/config.json`, mode 0600.
- `categories` is optional. Skip it and every line falls under "uncategorised".
- To look around without the dashboard: `./lidl.py info`, `verify`,
  `dump -o receipt.json`.

## Which countries this works for

The client, the login and the storage are country-agnostic. The receipt parser
is written for Dutch receipts.

Other countries return the same JSON envelope, so `sync` and `dump` should work
anywhere. The printed-receipt HTML inside it differs per country, so
[receipt_html.py](lidl_receipts/receipt_html.py) will need work elsewhere.
`lidl verify` tells you whether it does: it checks that the line items of every
receipt add up to the total the API itself reports.

The dashboard and the report are in Dutch.

## Why a separate client

`Andre0512/lidl-plus` and `RoryDotGG/iLidl` already do this, and the endpoints,
headers and OAuth parameters here come from them. They target other countries,
and a Dutch receipt differs in the places that reach the parser:

- Commas as the decimal separator (`39,99`).
- No `TOTAL` line.
- Discounts are called `Lidl Plus korting`, `Lidl Plus kassabon` and
  `In prijs verlaagd`.
- One article can appear on several separate lines of a single receipt. That
  happened on 6 of the 12 receipts I looked at, up to 3 lines each.
  Deduplicating on article number drops those.

Both clients drive a browser (selenium-wire and Playwright). Lidl guards the
login form with reCAPTCHA Enterprise, so that approach means keeping pace with a
captcha built to stay ahead. Logging in through your own browser steps out of
that race. The auth flow is only about 50 lines and the receipts API is two
endpoints, so a separate client came out smaller than a wrapper around either.

One thing is worth knowing if you build something like this yourself: **Lidl
rotates refresh tokens.** Every renewal returns a new one and invalidates the
old. Store only the first and everything works for months before it stops
([client.py:53-60](lidl_receipts/client.py#L53-L60)).

## Logging in

`./lidl.py login` starts a PKCE flow in your own browser and waits for the
callback.

That callback is `com.lidlplus.app://callback?code=...`, an app scheme rather
than a web address. `login` claims that scheme with your desktop, so the browser
hands the callback to a handler, which drops the code in
`~/.config/lidl-receipts/callback.json`. There is nothing to set up. The first
time, the browser asks permission to open it. Allow that, and tick *always
allow*.

| Platform | How the scheme is claimed |
| --- | --- |
| Windows | `HKCU\Software\Classes` via `winreg`, launched with `pythonw.exe` so no console window appears |
| WSL | the same registry key written through PowerShell, pointing back into the distro via `wsl.exe` |
| Linux | a `.desktop` file plus `xdg-mime` |
| macOS | unsupported, because Launch Services wants a real app bundle |

`./lidl.py handler status` shows the registration, `handler uninstall` removes
it, `login --no-handler` skips it.

A loopback redirect is not an option. Lidl's IdentityServer only accepts the
app's own redirect URI and sends `http://localhost:.../callback` to `/error`
before the login form appears. That is a server-side answer, so there is no port
to listen on on any platform.

### By hand, without the handler

If the registration fails, read the URL instead of following it. Before logging
in, open F12 and the Network tab with **Preserve log** ticked. The callback is
the last request. Chrome and Edge show it only there; Firefox usually leaves it
in the address bar as well. Paste the URL, or just the `code=` value.

Codes expire within minutes, but the verifier is kept:

```bash
./lidl.py login --code "com.lidlplus.app://callback?code=..."
```

## Commands

| Command | What it does |
| --- | --- |
| `login` | One-time browser login, stores the refresh token |
| `handler` | `status` / `install` / `uninstall` for the callback registration |
| `config` | Shows or changes country, language, app version, data directory |
| `list` | Receipt list straight from the API (v2), without fetching details |
| `dump` | Raw JSON of one receipt (v3). `--id X`, newest by default |
| `sync` | Downloads receipts that are not in the database yet |
| `renormalize` | Reparses stored raw JSON, no network |
| `verify` | Checks that line items add up to the receipt total |
| `export` | Exports to JSON or CSV |
| `info` | Database statistics |

```bash
./lidl.py dump --normalized          # raw JSON plus the normalized projection
./lidl.py sync --fast                # stop at the first receipt already known
./lidl.py sync --since 2026-01-01    # only receipts from a date onward
./lidl.py sync --limit 5             # try something small first
./lidl.py export --format csv -o items.csv
```

## Data model

The **raw API JSON is the source of truth**. It sits verbatim in
`receipts.raw_json` and as a separate file in `data/raw/`. The `items` and
`item_discounts` tables are a derived cache. Change the parser and run
`./lidl.py renormalize`, with no network and no re-downloading.

Amounts are **whole cents** (`INTEGER`) everywhere. They arrive as locale
strings (`"2,19"`, `"1.234,56"`), and rounding those through floats is how cent
drift gets into a spending analysis. The views `v_receipts`, `v_items` and
`v_spend` hand back euros:

```sql
SELECT name, COUNT(*) AS times, ROUND(SUM(net), 2) AS spent
FROM v_items GROUP BY name ORDER BY spent DESC LIMIT 20;
```

`export --format json` gives an `items` array per receipt with name, quantity,
unit price, barcode and discounts.

## The Dutch receipt

Two formats, depending on the age of the receipt:

| | `ticketType` | Line items | Deposit |
| --- | --- | --- | --- |
| until ~2022 | `NATIVE` | `itemsLine`, as in DE and AT | nested object inside the article line |
| after that | `HTML` | no JSON, only `htmlPrintedReceipt` | its own line |

On a NATIVE receipt the deposit counts towards the receipt total but not towards
the line amount. Normalization turns it into a separate line in both cases.

The HTML variant is a monospace rendering of the paper receipt. Each span
carries the underlying values as `data-*` attributes.

```html
<span id="purchase_list_line_2" class="article" data-art-id="0080000"
      data-art-quantity="0,792" data-unit-price="1,39" data-tax-type="B"
      data-art-description="Bananen">Bananen        1,10 B</span>
```

One receipt line is spread over several spans sharing an `id`.
[receipt_html.py](lidl_receipts/receipt_html.py) groups on that and returns the
same shape as `itemsLine`. Three edge cases break the arithmetic:

| Line | Trap |
| --- | --- |
| `1,120 kg x 1,37 EUR` | Continuation of a weighed article, carrying the same `data-art-id`. Counts twice if you turn it into an article |
| `In prijs verlaagd  -1,80` | Discount on the line above, but without `class="discount"` and without a promotion id |
| `[X] Emballage  -3,90` | Deposit refund: a **separate** line, not a discount on the article above |

For the first one the tail differs per receipt vintage: `EUR`, `EUR/kg` and
`EUR/kg ` all occur. The parser looks at structure instead. An article line
without a closing amount of its own is a continuation.

## Dashboard

`python3 analysis/dashboard.py` writes a single self-contained HTML file for
`file://`, half a megabyte to a megabyte. Everything is computed in the browser,
with no server, no build step and no dependencies.

| Panel | What you see |
| --- | --- |
| Overzicht | Spending per month, per category, largest products |
| Tijd | Weekday x hour, spending per weekday, basket per hour, days between visits |
| Categorieën | Stacked per year, total per category |
| Seizoen | Seasonal index per category-month, summer and winter products |
| Producten | All products, new to your basket, no longer bought |
| Prijzen | Your own basket index, steepest risers and fallers |
| Kortingen | Per month, per category in euros and in percent |
| Mandje | Receipt size distribution, time since previous visit, small versus large |
| Bonnen | Every receipt, with a drill-down to the line items |

The payload carries the **individual purchase lines** rather than
pre-aggregated totals. Every panel derives from those, and scanning seventeen
thousand rows costs the browser a few milliseconds. Filtering works on year,
category, store and article name, and those four scope everything: the figures,
the charts and the tables. The filters live in the URL hash, so a view can be
bookmarked:

```
dashboard.html#tab=seizoen
dashboard.html#jaar=2026&cat=alcohol
dashboard.html#q=mineraalwater&p=Mineraalwater%201.5l
```

You can look at it without an account, using invented data:

```bash
python3 analysis/demo_data.py data/demo.db
python3 analysis/dashboard.py data/demo.db analysis/demo.html
```

That is three years of invented shopping built from the shared category dataset,
with a fixed seed. The screenshot at the top comes from it.

## Report and PDF

```bash
python3 analysis/spending.py > analysis/spending.json
python3 analysis/report.py   > analysis/report.html
./analysis/topdf.sh          # -> analysis/report.pdf
```

`spending.py` computes, `report.py` draws. The prose in the report reads its
claims out of the data, so it does not go stale as your dataset grows.
`topdf.sh` uses Chrome on the Windows side. With `chromium` or `wkhtmltopdf`
installed, that script is unnecessary.

## Categories

Lidl prints till names only. `./lidl.py categories` builds `categories.csv` from
three sources, in this order:

1. **your own corrections** in `categories.csv`, which always win
2. **[categories.seed.csv](categories.seed.csv)**, some eight hundred recurring
   Dutch Lidl articles with their category
3. **the keyword rules** in [categories.py](lidl_receipts/categories.py)

Your own `categories.csv` is in `.gitignore`, because it holds your amounts and
purchase counts. The shared dataset holds names and categories only, and covers
the recurring assortment. Articles whose till name gives away too little stay in
`overig voeding`, deliberately unsorted rather than guessed.

The command also cross-checks the VAT group. Lidl charges 9% on groceries, so
21% on something filed as food is almost certainly wrong:

```bash
./lidl.py categories
# 21% btw maar ingedeeld als voeding:
#   €   439.14  Fris en fruitig wit Z-A          frisdrank & sap
```

That is a wine line with a name that sounds like a soft drink. Keywords never
catch all of those. The till does not lie about VAT.

## One product, many names

The till writes the same article in several ways and gives it new article
numbers along the way. Mineral water appears under six names and four numbers.
Counting by name makes every product look smaller than it is.

Neither key works on its own. Grouping by name splits a product whose barcode
changed, grouping by barcode splits a product that was renamed, and both happen.
So two lines belong together when they share a **name or a barcode**, applied
transitively. That is a union-find over the combinations that actually occur on
your receipts ([products.py](lidl_receipts/products.py)). The grouping is
derived rather than maintained by hand. Do check it against your own data: if a
generic till code ties unrelated products together, you see it as a group with
an unusual number of names.

### Group for totals, never for prices

Grouping is right for "how much did I spend on cucumber" and wrong for "what did
cucumber cost". One till name often covers several pack sizes, and sometimes a
different unit:

| barcode | | |
| --- | --- | --- |
| `0082895` | € 2.31 **per kg** | 2025-2026 |
| `20242091` | € 0.91 **each** | 2021-2022 |

Both are "Broccoli". On one line that suggests +150%. Per article number it is
-10% for the kilo price and +9% for the unit price. Almost a quarter of products
have more than one article number.

Every price series therefore keys on the article number. Only amounts are summed
per product.

## Verification

The sum of the line items (amount minus discounts) should exactly equal the
`totalAmount` from the API. A missed, doubled or misread line breaks that sum:

```bash
./lidl.py verify
# All 718 receipts reconcile exactly.
```

This is the sharpest test there is on a receipt parser. It caught the `EUR`
spelling above, the nested deposit on the NATIVE receipts, and a sample that
happened to come from a single vintage. Run it after every sync.

Receipts that contradict themselves, where the `taxes` block names a different
amount than the line items do, are left standing as discrepancies.

## API reference

```
POST https://accounts.lidl.com/connect/authorize   (PKCE S256, Country=NL, language=nl-NL)
POST https://accounts.lidl.com/connect/token       (Basic LidlPlusNativeClient:secret)
GET  https://tickets.lidlplus.com/api/v2/NL/tickets?pageNumber=1&onlyFavorite=false
GET  https://tickets.lidlplus.com/api/v3/NL/tickets/{id}
```

Required headers on the tickets API: `Authorization: Bearer`, `App-Version`,
`Operating-System: iOs`, `App: com.lidl.eci.lidl.plus`, `Accept-Language`.

The API rejects implausible app versions. If calls start failing with a 4xx that
mentions the app version, bump it: `./lidl.py config --app-version 17.1.0`.

## Tests

```bash
python3 -m unittest discover -s tests
```

73 tests, all offline: amount and date parsing, the HTML receipt parser with its
edge cases, normalization, the SQLite store, the sync importer against a stubbed
client, and the callback handler on all three desktops.

## Licence

MIT. See [LICENSE](LICENSE).

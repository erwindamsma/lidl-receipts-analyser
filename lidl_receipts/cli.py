"""Command-line interface."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from . import auth, categories, handler, products
from .client import AuthExpired, LidlClient
from .config import Config
from .http import HttpError
from .normalize import normalize_ticket, to_export_dict
from .store import Store

LOGIN_INSTRUCTIONS = """
1. Open the URL below in your normal browser (not a private window).
2. Before logging in, open the developer tools: press F12 -> Network tab ->
   tick "Preserve log". You need the request that comes at the very end.
3. Log in with your Lidl Plus phone number and password, and complete the
   SMS verification if asked.
4. Login ends with a redirect to com.lidlplus.app://callback?code=...
   That is an app scheme, not a web address: clicking it does nothing and
   the browser shows an error or an "open app?" dialog. Expected: read it
   out of the Network tab rather than trying to follow it.

   Chrome/Edge: find the com.lidlplus.app://callback entry in the Network
   tab and copy its full URL.
   Firefox: the failed URL usually stays visible in the address bar.

5. Paste that full URL (or just the code=... value) below.

This is the fallback. Normally `lidl login` claims the callback scheme with
your desktop and the code arrives on its own. `lidl handler status` says why
it could not here.
"""

HANDLED_LOGIN_INSTRUCTIONS = """
1. Open the URL below in your normal browser (not a private window).
2. Log in with your Lidl Plus phone number and password, and complete the
   SMS verification if asked.
3. The browser asks permission to open the callback handler, so allow it. The
   code then arrives here on its own.
"""


def _out(message: str = "") -> None:
    print(message, file=sys.stderr)


def _write_json(data, path: str | None, *, compact: bool = False) -> None:
    text = json.dumps(
        data,
        ensure_ascii=False,
        indent=None if compact else 2,
        sort_keys=False,
    )
    if path in (None, "-"):
        print(text)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text + "\n", encoding="utf-8")
        _out(f"Wrote {path}")


def cmd_login(args: argparse.Namespace) -> int:
    if args.receive:
        # Not the user typing: this is the registered scheme handler, started
        # by the browser. Park the code for the waiting login and get out.
        handler.deposit(args.receive, args.callback)
        return 0

    config = Config.load()
    if args.country:
        config.country = args.country
    if args.language:
        config.language = args.language

    if args.code:
        verifier = auth.load_verifier()
        code = auth.extract_code(args.code)
    else:
        verifier, challenge = auth.generate_pkce()
        auth.save_verifier(verifier)
        url = auth.authorize_url(challenge, config.country, config.language)
        registered = None if args.no_handler else handler.ensure()
        automatic = not args.no_handler and handler.installed()
        # A code from an abandoned run is expired and single-use; never let
        # one satisfy this login.
        handler.clear()
        if registered:
            _out(
                f"Registered {handler.SCHEME}:// with your desktop "
                f"({registered}), so the callback arrives here by itself.\n"
                "Undo any time with: lidl handler uninstall\n"
            )
        _out(HANDLED_LOGIN_INSTRUCTIONS if automatic else LOGIN_INSTRUCTIONS)

        _out("Authorization URL:\n")
        print(url)
        _out("")

        code = ""
        if automatic:
            _out("Waiting for the browser... (Ctrl-C to paste it by hand)")
            try:
                code = handler.wait_for_code() or ""
            except ValueError as exc:
                # The browser handed over something without a code in it --
                # an error redirect, say, when consent is refused. That is
                # not a bug here, and no reason to abandon a login that can
                # still be finished by pasting.
                _out(f"The callback carried no code: {exc}")
        if not code:
            try:
                pasted = input("Paste callback URL or code: ")
            except (EOFError, KeyboardInterrupt):
                _out("\nAborted. Finish later with: lidl login --code <code>")
                return 1
            code = auth.extract_code(pasted)

    try:
        tokens = auth.exchange_code(code, verifier)
    except HttpError as exc:
        _out(f"Token exchange failed: {exc}")
        if "invalid_grant" in exc.body:
            _out(
                "\nAuthorization codes expire within minutes and are "
                "single-use. Run `lidl login` again and paste more quickly."
            )
        return 1

    config.refresh_token = tokens["refresh_token"]
    config.save()
    auth.clear_verifier()
    _out(f"Logged in. Refresh token stored for country={config.country}.")
    return 0


def cmd_handler(args: argparse.Namespace) -> int:
    if args.action == "install":
        command = handler.install()
        _out(f"Registered {handler.SCHEME}:// -> {command}")
        _out(
            "\nThe first callback makes the browser ask whether to open it. "
            'Allow it, and tick "always" to be asked only once.'
        )
        return 0

    if args.action == "uninstall":
        handler.uninstall()
        _out(f"Removed the {handler.SCHEME}:// registration.")
        return 0

    kind = handler.backend()
    if kind is None:
        _out(handler.UNSUPPORTED)
        _out("\n`lidl login` still works; it will ask you to paste the URL.")
        return 1
    _out(f"Desktop: {kind}")
    current = handler.registered_command()
    if not current:
        _out(f"{handler.SCHEME}:// is not registered yet.")
        _out("`lidl login` registers it on its own, or do it now: "
             "lidl handler install")
        return 1
    _out(f"Command: {current}")
    if not handler.installed():
        _out(
            "\nThat points at another copy of this project, so its callbacks "
            "would land in a directory this one is not watching. Expected:\n"
            f"  {handler.expected_command()}\n"
            "Run `lidl handler install` to claim the scheme for this copy."
        )
        return 1
    _out("Ready: `lidl login` picks the code up by itself.")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = Config.load()
    changed = False
    for field in ("country", "language", "app_version", "data_dir"):
        value = getattr(args, field, None)
        if value:
            setattr(config, field, value)
            changed = True
    if changed:
        config.save()
        _out("Config updated.")

    shown = {
        "country": config.country,
        "language": config.language,
        "app_version": config.app_version,
        "data_dir": config.data_dir,
        "refresh_token": (
            f"<set, {len(config.refresh_token)} chars>"
            if config.refresh_token
            else "<not set>"
        ),
    }
    print(json.dumps(shown, indent=2))
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    """Fetch one receipt and print the raw API payload, unmodified."""
    config = Config.load()
    client = LidlClient(config)

    if args.id:
        raw = client.ticket(args.id)
    else:
        raw = client.latest_ticket()

    _write_json(raw, args.output, compact=args.compact)

    if args.normalized:
        normalized = normalize_ticket(raw)
        _out("\n--- normalized ---")
        print(json.dumps(to_export_dict(normalized), ensure_ascii=False, indent=2))
        for warning in normalized["warnings"]:
            _out(f"WARNING: {warning}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List receipt summaries straight from the API (no detail fetches)."""
    config = Config.load()
    client = LidlClient(config)
    summaries = []
    for summary in client.list_tickets():
        summaries.append(summary)
        if args.limit and len(summaries) >= args.limit:
            break

    if args.json:
        _write_json(summaries, args.output)
        return 0

    for summary in summaries:
        print(
            f"{summary.get('id', '?'):40} "
            f"{str(summary.get('date', ''))[:19]:20} "
            f"{summary.get('totalAmount', ''):>10} "
            f"{summary.get('storeCode', '')}"
        )
    _out(f"\n{len(summaries)} receipts.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Download every receipt not yet in the database."""
    config = Config.load()
    client = LidlClient(config)

    with Store(config.db_path) as store:
        known = store.known_ids()
        _out(f"{len(known)} receipts already stored in {config.db_path}")

        to_fetch: list[str] = []
        for summary in client.list_tickets():
            ticket_id = str(summary.get("id") or "")
            if not ticket_id:
                continue
            # The API lists newest first, so the first receipt older than
            # --since means every remaining one is older too.
            if args.since:
                date = str(summary.get("date") or "")[:10]
                if date and date < args.since:
                    _out(f"Reached {date}, older than {args.since}; stopping.")
                    break
            if ticket_id in known and not args.force:
                if args.fast:
                    _out("Reached a known receipt; stopping (--fast).")
                    break
                continue
            to_fetch.append(ticket_id)
            if args.limit and len(to_fetch) >= args.limit:
                break

        if not to_fetch:
            _out("Nothing new.")
            return 0

        _out(f"Fetching {len(to_fetch)} receipt(s)...")
        warnings: list[str] = []
        for index, ticket_id in enumerate(to_fetch, start=1):
            raw = client.ticket(ticket_id)
            normalized = store.save(raw)
            if not args.no_raw_files:
                config.raw_dir.mkdir(parents=True, exist_ok=True)
                (config.raw_dir / f"{ticket_id}.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            total = normalized["total_cents"]
            _out(
                f"  [{index}/{len(to_fetch)}] {ticket_id} "
                f"{str(normalized['date'])[:10]} "
                f"{'' if total is None else f'{total / 100:.2f}'} "
                f"({len(normalized['items'])} items)"
            )
            warnings.extend(
                f"{ticket_id}: {w}" for w in normalized["warnings"]
            )
            if index < len(to_fetch):
                time.sleep(args.delay)

        if warnings:
            _out("\nNormalization warnings (raw data is stored regardless):")
            for warning in warnings[:20]:
                _out(f"  {warning}")
            if len(warnings) > 20:
                _out(f"  ... and {len(warnings) - 20} more")

        products.build(store.conn)
        stats = store.stats()
        _out(
            f"\nStored: {stats['receipts']} receipts, {stats['items']} items, "
            f"{stats['total_cents'] / 100:.2f} total."
        )
    return 0


def cmd_renormalize(args: argparse.Namespace) -> int:
    config = Config.load()
    with Store(config.db_path) as store:
        count, warnings = store.renormalize()
        _out(f"Reparsed {count} stored receipts.")
        for warning in warnings[:20]:
            _out(f"  {warning}")
        if len(warnings) > 20:
            _out(f"  ... and {len(warnings) - 20} more")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = Config.load()
    with Store(config.db_path) as store:
        receipts = [
            to_export_dict(normalize_ticket(raw))
            for _, raw in store.raw_payloads()
        ]

    if args.format == "json":
        _write_json(receipts, args.output)
        return 0

    rows = []
    for receipt in receipts:
        for item in receipt["items"]:
            rows.append(
                {
                    "receipt_id": receipt["id"],
                    "date": receipt["date"],
                    "store": receipt["store"],
                    "name": item["name"],
                    "quantity": item["quantity"],
                    "unit_price": item["unitPrice"],
                    "amount": item["amount"],
                    "discount": round(
                        sum(d["amount"] or 0 for d in item["discounts"]), 2
                    ),
                    "net": item["net"],
                    "barcode": item["barcode"],
                    "tax_group": item["taxGroup"],
                    "is_weight": item["isWeight"],
                }
            )

    handle = (
        open(args.output, "w", newline="", encoding="utf-8")
        if args.output not in (None, "-")
        else sys.stdout
    )
    try:
        writer = csv.DictWriter(
            handle, fieldnames=list(rows[0]) if rows else ["receipt_id"]
        )
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if handle is not sys.stdout:
            handle.close()
            _out(f"Wrote {len(rows)} item rows to {args.output}")
    return 0


def cmd_categories(args: argparse.Namespace) -> int:
    """Refresh the category CSV and load it into the database.

    Safe to rerun: categories already filled in are kept, only articles that
    are new to the file get a proposed one.
    """
    config = Config.load()
    path = Path(args.file)

    with Store(config.db_path) as store:
        total, seeded, proposed = categories.refresh_csv(store.conn, path)
        loaded = categories.apply_csv(store.conn, path)
        _out(
            f"{path}: {total} articles, {seeded} from the shared mapping, "
            f"{proposed} proposed by rule, {loaded} loaded."
        )

        rows = store.conn.execute(
            """
            SELECT category, COUNT(*) AS lines,
                   ROUND(SUM(net), 2) AS spend
            FROM v_spend GROUP BY category ORDER BY SUM(net) DESC
            """
        ).fetchall()
        conflicts = categories.resolve_category_conflicts(store.conn, path)
        mismatches = categories.high_vat_mismatches(store.conn, min_spend=5.0)

    width = max((len(r["category"]) for r in rows), default=10)
    for row in rows:
        print(
            f"  {row['category']:<{width}}  {row['lines']:>5} regels  "
            f"€ {row['spend']:>8.2f}"
        )
    if proposed:
        _out(
            f"\nLoop {path} na: de bovenste regels zijn waar het geld zit. "
            "Corrigeer de kolom 'category' en draai dit commando opnieuw."
        )

    if conflicts:
        _out(
            "\nEén product stond in meerdere categorieën. De kassa hernoemt "
            "en hernummert, dus die zijn gelijkgetrokken:"
        )
        for name, was, now in conflicts:
            _out(f"  {name[:32]:<32} {was} -> {now}")

    if mismatches:
        _out(
            "\n21% btw maar ingedeeld als voeding. Lidl rekent 9% over "
            "boodschappen, dus dit is vermoedelijk drank, drogisterij of "
            "non-food:"
        )
        for row in mismatches:
            _out(
                f"  € {row['spend']:>8.2f}  {row['name'][:32]:<32} "
                f"{row['category']}"
            )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check parsed line items against the total the API reports.

    This is the sharpest test available on the receipt parser: if a line is
    dropped, duplicated or misread, the sum stops matching. Receipt formats
    vary by vintage, so it is worth rerunning after every sync.
    """
    config = Config.load()
    mismatches = 0
    checked = 0

    with Store(config.db_path) as store:
        for receipt_id, raw in store.raw_payloads():
            receipt = normalize_ticket(raw)
            checked += 1
            items_sum = sum(i["net_cents"] or 0 for i in receipt["items"])
            total = receipt["total_cents"]
            if total is None or items_sum != total or receipt["warnings"]:
                mismatches += 1
                diff = "?" if total is None else f"{(items_sum - total) / 100:+.2f}"
                _out(
                    f"MISMATCH {str(receipt['date'])[:10]} {receipt_id}: "
                    f"lines {items_sum / 100:.2f} vs total "
                    f"{'?' if total is None else f'{total / 100:.2f}'} "
                    f"(diff {diff})"
                )
                for warning in receipt["warnings"]:
                    _out(f"         {warning}")

    if mismatches:
        _out(f"\n{checked - mismatches}/{checked} receipts reconcile.")
        return 1
    _out(f"All {checked} receipts reconcile exactly.")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    config = Config.load()
    with Store(config.db_path) as store:
        stats = store.stats()
    print(
        json.dumps(
            {
                "database": str(config.db_path),
                "receipts": stats["receipts"],
                "items": stats["items"],
                "first_date": stats["first_date"],
                "last_date": stats["last_date"],
                "total_spend": round(stats["total_cents"] / 100, 2),
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lidl", description="Fetch and analyse Lidl Plus receipts."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="one-time browser login")
    p_login.add_argument("--country", help="e.g. NL")
    p_login.add_argument("--language", help="e.g. nl")
    p_login.add_argument(
        "--code", help="finish a pending login with a callback URL or code"
    )
    p_login.add_argument(
        "--receive",
        help="internal: hand a callback URL over from the scheme handler",
    )
    p_login.add_argument(
        "--callback",
        help="internal: where the scheme handler drops the code",
    )
    p_login.add_argument(
        "--no-handler",
        action="store_true",
        help="do not register the callback scheme; paste the URL instead",
    )
    p_login.set_defaults(func=cmd_login)

    p_handler = sub.add_parser(
        "handler", help=f"register the {handler.SCHEME}:// callback scheme"
    )
    p_handler.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["install", "uninstall", "status"],
    )
    p_handler.set_defaults(func=cmd_handler)

    p_config = sub.add_parser("config", help="show or change settings")
    p_config.add_argument("--country")
    p_config.add_argument("--language")
    p_config.add_argument("--app-version", dest="app_version")
    p_config.add_argument("--data-dir", dest="data_dir")
    p_config.set_defaults(func=cmd_config)

    p_dump = sub.add_parser(
        "dump", help="print one receipt's raw API payload"
    )
    p_dump.add_argument("--id", help="receipt id (default: most recent)")
    p_dump.add_argument("-o", "--output", help="write to file instead of stdout")
    p_dump.add_argument("--compact", action="store_true")
    p_dump.add_argument(
        "--normalized",
        action="store_true",
        help="also print the normalized projection",
    )
    p_dump.set_defaults(func=cmd_dump)

    p_list = sub.add_parser("list", help="list receipts from the API")
    p_list.add_argument("--limit", type=int, default=0)
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("-o", "--output")
    p_list.set_defaults(func=cmd_list)

    p_sync = sub.add_parser("sync", help="download new receipts into SQLite")
    p_sync.add_argument("--limit", type=int, default=0)
    p_sync.add_argument(
        "--force", action="store_true", help="refetch receipts already stored"
    )
    p_sync.add_argument(
        "--fast",
        action="store_true",
        help="stop at the first already-known receipt",
    )
    p_sync.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="skip receipts older than this date",
    )
    p_sync.add_argument("--delay", type=float, default=0.3)
    p_sync.add_argument("--no-raw-files", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_renorm = sub.add_parser(
        "renormalize", help="reparse stored raw payloads (no network)"
    )
    p_renorm.set_defaults(func=cmd_renormalize)

    p_export = sub.add_parser("export", help="export stored receipts")
    p_export.add_argument(
        "--format", choices=("json", "csv"), default="json"
    )
    p_export.add_argument("-o", "--output")
    p_export.set_defaults(func=cmd_export)

    p_categories = sub.add_parser(
        "categories", help="refresh and load the article category mapping"
    )
    p_categories.add_argument(
        "--file",
        default="categories.csv",
        help="CSV holding the mapping (default: categories.csv)",
    )
    p_categories.set_defaults(func=cmd_categories)

    p_verify = sub.add_parser(
        "verify", help="check that line items add up to the receipt total"
    )
    p_verify.set_defaults(func=cmd_verify)

    p_info = sub.add_parser("info", help="database statistics")
    p_info.set_defaults(func=cmd_info)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AuthExpired as exc:
        _out(f"Authentication problem: {exc}")
        return 2
    except HttpError as exc:
        _out(f"API error: {exc}")
        return 3
    except KeyboardInterrupt:
        _out("Interrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001: a CLI should not spill tracebacks
        _out(f"{type(exc).__name__}: {exc}")
        _out(
            "\nThat is a bug. Rerun with LIDL_DEBUG=1 for the traceback, and "
            "please report it with the command you ran."
        )
        if os.environ.get("LIDL_DEBUG"):
            raise
        return 1

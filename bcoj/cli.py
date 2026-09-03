"""Command line entry point. Standard library only.

    python3 -m bcoj.cli reconcile export.csv
    python3 -m bcoj.cli panel      export.csv --symbol TICKER
    python3 -m bcoj.cli shares     export.csv --estimates
    python3 -m bcoj.cli flags      export.csv
    python3 -m bcoj.cli chains     export.csv --symbol TICKER --top 5
    python3 -m bcoj.cli serve      --db journal.db
"""

import argparse
import sys
from decimal import Decimal

from .domain.enums import MatchingRule
from .domain.money import fmt
from .engine.chains import ChainIndex
from .engine.pnl import realized_pl
from .engine.risk import break_even, credit_to_recover
from .engine.targets import target
from .importer import legacy_csv, legacy_formulas, shares as share_import, triage
from .importer.reconcile import reconcile, render


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bcoj", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("reconcile", "recompute every figure and diff against the sheet"),
        ("import", "reconcile, then persist to a SQLite journal"),
        ("panel", "reproduce the sheet's summary panel"),
        ("shares", "share lots, disposals and P/L by ticker"),
        ("flags", "data-quality concerns needing a human"),
        ("chains", "roll chains, longest first"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("csv", help="path to the legacy export")
        if name == "import":
            p.add_argument("--db", default="journal.db", help="SQLite file to write")
            p.add_argument(
                "--estimates",
                action="store_true",
                help="seed reconstructed share lots for tickers in deficit",
            )
            p.add_argument(
                "--force",
                action="store_true",
                help="import even if rows disagree with the sheet",
            )
        if name in ("panel", "shares", "chains"):
            p.add_argument("--symbol", default=None, help="filter to one ticker")
        if name in ("shares",):
            p.add_argument(
                "--rule",
                default="FIFO",
                choices=[r.value for r in MatchingRule if r.value != "SPECIFIC"],
            )
            p.add_argument(
                "--estimates",
                action="store_true",
                help="seed reconstructed lots for tickers in deficit",
            )
        if name in ("chains",):
            p.add_argument("--top", type=int, default=10)
        if name in ("reconcile", "flags"):
            p.add_argument("--limit", type=int, default=40)

    serve_parser = sub.add_parser("serve", help="run the web UI locally")
    serve_parser.add_argument("--db", default="journal.db")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "serve":
        from .web import serve

        serve(args.db, args.host, args.port)
        return 0

    try:
        report = legacy_csv.parse_file(args.csv)
    except FileNotFoundError:
        print(f"error: no such file: {args.csv}", file=sys.stderr)
        print(
            "\nThe legacy export is not in the repo. Save it as, for example,\n"
            "  data/export.csv\n"
            "and pass that path.",
            file=sys.stderr,
        )
        return 2

    if not report.rows:
        print("error: no data rows found", file=sys.stderr)
        return 2

    if not report.header_matched:
        print("warning: header differs from the expected layout", file=sys.stderr)
        print(f"  found: {','.join(report.header)}", file=sys.stderr)

    handler = {
        "reconcile": _cmd_reconcile,
        "import": _cmd_import,
        "panel": _cmd_panel,
        "shares": _cmd_shares,
        "flags": _cmd_flags,
        "chains": _cmd_chains,
    }[args.command]
    return handler(args, report)


def _cmd_import(args, report) -> int:
    """Reconcile first, then write. A disagreeing import needs --force."""
    from .db import store

    result = reconcile(report.rows)
    summary = render(result, limit=10)
    print(summary)
    print()

    if not result.clean and not args.force:
        print(
            "refusing to import: figures disagree with the sheet.\n"
            "Resolve them, or pass --force to import anyway (flags are kept).",
            file=sys.stderr,
        )
        return 1

    conn = store.open_db(args.db)
    try:
        batch = store.record_batch(
            conn, args.csv, row_count=len(report.rows), report=summary
        )
    except store.AlreadyImportedError as exc:
        print(f"nothing to do: {exc}")
        return 0

    store.save_positions(conn, report.positions, batch_id=batch)

    derived = share_import.derive(report.rows, include_estimates=args.estimates)
    store.save_shares(conn, derived.lots, derived.disposals, batch_id=batch)

    flags = triage.triage(report.rows)
    store.save_flags(conn, flags)

    errors = store.rebuild_allocations(conn)

    print(f"imported batch {batch} into {args.db}")
    print(f"  positions      {len(report.positions)}")
    print(f"  share lots     {len(derived.lots)}")
    print(f"  disposals      {len(derived.disposals)}")
    print(f"  flags raised   {len(flags)}")
    if errors:
        print("  share allocation blocked for:")
        for underlying, message in errors.items():
            print(f"    {underlying}: {message}")
        print("  (re-run with --estimates, or add the missing lot in the app)")
    return 0


def _cmd_reconcile(args, report) -> int:
    result = reconcile(report.rows)
    print(render(result, limit=args.limit))
    return 0 if result.clean else 1


def _cmd_panel(args, report) -> int:
    rows = report.rows
    p = legacy_formulas.panel(rows, args.symbol)
    label = p.symbol or "PORTFOLIO"

    print(f"{label} - as the sheet computes it")
    print("=" * 56)
    for field_label, value in (
        ("Realized P/L", p.realized_pl),
        ("Assignment", p.assignment),
        ("Adjusted P/L", p.adjusted_pl),
        ("Unrealised P/L", p.unrealised_pl),
        ("Current Premium", p.current_premium),
        ("Current Cost Basis", p.current_cost_basis),
        ("Current Put Risk", p.current_put_risk),
        ("Expected P/L", p.expected_pl),
        ("Expected Closing", p.expected_closing),
    ):
        print(f"  {field_label:<22}{fmt(value):>18}")
    print(f"  {'Open Calls':<22}{p.open_calls:>18}")
    print(f"  {'Open Puts':<22}{p.open_puts:>18}")
    print(f"  {'Shares':<22}{p.shares:>18}")
    print(f"  {'Adjusted Unit Price':<22}{fmt(p.adjusted_unit_price):>18}")
    print(f"  {'Adjusted Cover Call':<22}{fmt(p.adjusted_cover_call):>18}")

    if p.cover_call_double_count != 0:
        print()
        print("  Adjusted Cover Call double-counts rows that are both a call and")
        print("  assigned, because they satisfy both SUMIFs in the formula:")
        print(f"    double-counted P/L    {fmt(p.cover_call_double_count):>18}")
        print(f"    corrected figure      {fmt(p.adjusted_cover_call_corrected):>18}")
    if p.shares < 0:
        print()
        print(f"  WARNING: share count is {p.shares}, which is impossible.")
        print("  Every per-share figure above is derived from it and is unusable.")
    return 0


def _cmd_shares(args, report) -> int:
    rule = MatchingRule(args.rule)
    derived = share_import.derive(report.rows, include_estimates=args.estimates)
    if args.symbol:
        derived = derived.for_underlying(args.symbol)

    summaries = share_import.summarize(derived, rule)
    print(f"Share P/L by ticker ({rule.value})")
    print("=" * 78)
    print(
        f"  {'ticker':<8}{'acq':>7}{'disp':>7}{'net':>7}"
        f"{'realized':>14}{'open qty':>10}{'open cost':>14}"
    )
    total = Decimal("0")
    for s in summaries:
        if s.error:
            print(f"  {s.underlying:<8}{s.acquired:>7}{s.disposed:>7}{s.net:>7}"
                  f"{'BLOCKED':>14}{s.open_quantity:>10}{'-':>14}")
            continue
        total += s.realized
        print(
            f"  {s.underlying:<8}{s.acquired:>7}{s.disposed:>7}{s.net:>7}"
            f"{fmt(s.realized):>14}{s.open_quantity:>10}{fmt(s.open_cost):>14}"
        )
    print("-" * 78)
    print(f"  {'total':<8}{'':>21}{fmt(total):>14}")

    blocked = [s for s in summaries if s.error]
    if blocked:
        print()
        print("BLOCKED - an acquisition is missing:")
        for s in blocked:
            print(f"  {s.underlying}: {s.error}")
        print("  Re-run with --estimates to seed the reconstructed lot.")
    return 0


def _cmd_flags(args, report) -> int:
    flags = triage.triage(report.rows)
    tally = triage.summarize(flags)

    print(f"Data-quality flags: {len(flags)}")
    print("=" * 72)
    for kind, count in tally.items():
        print(f"  {count:>4}  {kind}")
    print()

    by_kind: dict[str, list] = {}
    for flag in flags:
        by_kind.setdefault(flag.kind, []).append(flag)

    for kind in tally:
        print(f"{kind}")
        for flag in by_kind[kind][: args.limit]:
            print(f"  {flag.entity_id:<40} {flag.detail}")
        extra = len(by_kind[kind]) - args.limit
        if extra > 0:
            print(f"  ... and {extra} more")
        print()
    return 0


def _cmd_chains(args, report) -> int:
    positions = report.positions
    if args.symbol:
        wanted = args.symbol.upper()
        positions = [p for p in positions if p.underlying == wanted]

    index = ChainIndex(positions)
    chains = sorted(index.chains(), key=lambda c: -c.leg_count)

    print(f"Roll chains: {len(chains)}")
    print("=" * 72)
    for chain in chains[: args.top]:
        head = chain.head
        state = "OPEN" if chain.is_open else head.status.value
        print(
            f"\n{chain.underlying}  {chain.leg_count} legs  {state}"
            f"  {chain.opened_on} -> {chain.closed_on or 'now'}"
        )
        print(f"  realized        {fmt(chain.realized):>16}")
        print(f"  carry into head {fmt(chain.carry):>16}")
        print(f"  net credit      {fmt(chain.net_credit):>16}")

        be = break_even(chain)
        if be is not None:
            print(
                f"  break-even      {fmt(be.price):>16}"
                f"   (strike {fmt(be.strike)}, {fmt(be.per_share)}/share)"
            )
        recover = credit_to_recover(chain)
        if recover > 0:
            print(f"  credit to recover{fmt(recover):>15}")
        if chain.is_open:
            t = target(head, chain.carry)
            print(
                f"  50% target      {fmt(t.price):>16}"
                f"   -> P/L {fmt(t.expected_pl)}"
            )
        for leg in chain.legs:
            leg_pl = None if leg.is_open else realized_pl(leg)
            print(
                f"    {leg.opened_on}  {leg.right.value[0]}"
                f"  {fmt(leg.strike):>9}  x{leg.quantity:<4}"
                f"  {leg.status.value:<9}{fmt(leg_pl):>14}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

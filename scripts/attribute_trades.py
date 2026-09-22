"""Run P&L attribution over an account's real Delta trade history.

    python scripts/attribute_trades.py --user mathi
    python scripts/attribute_trades.py --user mathi --days 90 --json out.json

Read-only: it reads the authenticated fill ledger and public BTCUSD candles,
and places no orders. It must run somewhere the account's API key is
configured *and* the host IP is whitelisted by Delta -- in practice the EC2
box, not a workstation.

Credentials are never passed on the command line or printed; the script
reuses the dashboard's own account lookup so the keys stay where they already
live (``users/<account>/account.json``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trade_attribution as ta  # noqa: E402

_CANDLE_RESOLUTION = "1m"
# /v2/history/candles caps a response at 4,000 rows, so a minute series has
# to be requested in windows rather than one sweep.
_CANDLE_ROWS_PER_REQUEST = 3_000
_CANDLE_WINDOW = timedelta(minutes=_CANDLE_ROWS_PER_REQUEST)


def _money(value: float | None) -> str:
    if value is None:
        return "        --"
    return f"{value:>10,.2f}"


def fetch_candles(dashboard, start: datetime, end: datetime) -> list[dict]:
    """Public 1-minute BTCUSD candles spanning [start, end], paged."""
    rows: list[dict] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + _CANDLE_WINDOW, end)
        response = dashboard.req.get(
            f"{dashboard.API_BASE}/v2/history/candles",
            params={"resolution": _CANDLE_RESOLUTION, "symbol": "BTCUSD",
                    "start": int(cursor.timestamp()),
                    "end": int(window_end.timestamp())},
            timeout=20,
        ).json()
        page = response.get("result") or []
        if not isinstance(page, list):
            raise RuntimeError("candle history returned an invalid page")
        rows.extend(page)
        cursor = window_end
    return rows


def build_contract_values(dashboard) -> dict[str, float]:
    """symbol -> contract_value, from the public product list."""
    response = dashboard.req.get(f"{dashboard.API_BASE}/v2/products",
                                 params={"page_size": 1000}, timeout=20).json()
    values: dict[str, float] = {}
    for product in response.get("result") or []:
        symbol = product.get("symbol")
        try:
            value = float(product.get("contract_value"))
        except (TypeError, ValueError):
            continue
        if symbol and value > 0:
            values[str(symbol).upper()] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True,
                        help="account whose fill history to read")
    parser.add_argument("--days", type=int, default=180,
                        help="how far back to pull candles (default 180)")
    parser.add_argument("--json", dest="json_path",
                        help="also write the full result as JSON")
    parser.add_argument("--fallback-contract-value", type=float, default=0.001,
                        help="used only for symbols absent from /v2/products, "
                             "e.g. already-expired contracts (default 0.001)")
    args = parser.parse_args()

    import dashboard  # heavy import; safe -- loops are under __main__

    account = dashboard._find_account(args.user)
    if not account:
        print(f"no such account: {args.user}", file=sys.stderr)
        return 2
    if not account.get("api_key") or not account.get("api_secret"):
        print(f"account {args.user} has no Delta API credentials configured",
              file=sys.stderr)
        return 2
    # _fetch_complete_delta_fills resolves credentials through _active_creds,
    # which outside a request context falls back to DASH_USER. Point that at
    # the requested account so the correct key is used. Read-only.
    dashboard.DASH_USER = args.user

    print(f"reading fill ledger for {args.user} ...")
    fills = dashboard._fetch_complete_delta_fills()
    rows = dashboard._reconstruct_delta_trades(fills)
    closed = [r for r in rows if str(r.get("status", "")).upper() == "CLOSED"]
    print(f"  {len(fills):,} fills -> {len(rows):,} position cycles "
          f"({len(closed):,} closed)")

    if not closed:
        print("\nNo closed trades to attribute.")
        return 0

    stamps = [ta.parse_utc(r.get("entry_at_utc")) for r in closed]
    stamps += [ta.parse_utc(r.get("exit_at_utc")) for r in closed]
    stamps = [s for s in stamps if s]
    if not stamps:
        print("no usable timestamps on the closed trades", file=sys.stderr)
        return 1
    start = max(min(stamps) - timedelta(minutes=10),
                datetime.now(timezone.utc) - timedelta(days=args.days))
    end = max(stamps) + timedelta(minutes=10)

    print(f"fetching BTCUSD 1m candles {start:%Y-%m-%d} .. {end:%Y-%m-%d} ...")
    candles = fetch_candles(dashboard, start, end)
    underlying_at = ta.CandleUnderlyingLookup(candles)
    print(f"  {len(underlying_at):,} minutes of underlying prices")

    contract_values = build_contract_values(dashboard)
    print(f"  {len(contract_values):,} products with contract values")

    def contract_value_for(symbol: str) -> float:
        return contract_values.get(str(symbol).upper(),
                                   args.fallback_contract_value)

    report = ta.attribute_rows(closed, underlying_at=underlying_at,
                               contract_value_for=contract_value_for)

    print(f"\n{'':-<86}")
    print(f"ATTRIBUTED {len(report.attributed):,} TRADES   "
          f"(skipped {len(report.skipped):,})")
    print(f"{'':-<86}")

    if report.skipped:
        print("\nSkipped, by reason:")
        for reason, count in report.skip_reasons().items():
            print(f"  {count:>5,}  {reason}")
        if report.undecomposed_pnl:
            print(f"\n  P&L inside skipped trades: "
                  f"{report.undecomposed_pnl:,.2f} USD "
                  f"-- NOT included in the totals below")
    print(f"\ncoverage: {report.coverage:.0%} of gross P&L was decomposed")

    if not report.attributed:
        print("\nNothing could be decomposed. The reasons above say why.")
        return 0

    header = (f"\n{'date':<11}{'symbol':<22}{'side':<6}{'total':>10}"
              f"{'delta':>10}{'gamma':>9}{'vega':>10}{'theta':>9}{'hrs':>6}")
    print(header)
    print(f"{'':-<93}")
    for trade in sorted(report.attributed, key=lambda t: t.entry_at):
        a = trade.attribution
        flag = "" if a.reliable else "  <- unreliable"
        print(f"{trade.entry_at:%Y-%m-%d} {trade.symbol:<22}{trade.side:<6}"
              f"{_money(a.total)}{_money(a.delta)}{a.gamma:>9,.0f}"
              f"{_money(a.vega)}{a.theta:>9,.0f}{trade.held_hours:>6.1f}{flag}")

    def print_summary(title: str, summary: dict) -> None:
        print(f"\n{title}  ({summary['trades']:,} trades)")
        print(f"  total    {_money(summary['total'])}")
        print(f"  delta    {_money(summary['delta'])}   direction")
        print(f"  gamma    {_money(summary['gamma'])}   convexity")
        print(f"  vega     {_money(summary['vega'])}   implied vol "
              f"(incl. spread paid)")
        print(f"  theta    {_money(summary['theta'])}   time decay")
        print(f"  residual {_money(summary['residual'])}   fees (exact)")

    print(f"\n{'':=<86}")
    for kind, summary in report.by_kind().items():
        print_summary(f"{kind.upper()} book", summary)
    if len(report.by_kind()) > 1:
        print_summary("ALL TRADES", report.summary)
    unreliable = report.summary["unreliable_trades"]
    if unreliable:
        print(f"\n  note: {unreliable:,} trade(s) flagged unreliable are "
              f"included above; treat their split as indicative.")

    if args.json_path:
        payload = {
            "account": args.user,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": report.summary,
            "by_kind": report.by_kind(),
            "skipped": report.skip_reasons(),
            "trades": [
                {
                    "symbol": t.symbol, "kind": t.kind, "side": t.side,
                    "lots": t.lots, "strike": t.strike,
                    "entry_at": t.entry_at.isoformat(),
                    "exit_at": t.exit_at.isoformat(),
                    "entry_underlying": t.entry_underlying,
                    "exit_underlying": t.exit_underlying,
                    "held_hours": round(t.held_hours, 3),
                    **t.attribution.as_dict(),
                }
                for t in sorted(report.attributed, key=lambda t: t.entry_at)
            ],
        }
        Path(args.json_path).write_text(json.dumps(payload, indent=2),
                                        encoding="utf-8")
        print(f"\nwrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

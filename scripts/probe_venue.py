"""Re-verify the venue assumptions in docs/assumptions.md.

Read-only, unauthenticated, public market data only. Run before Phase 2 and
whenever an assumption is in doubt:

    python scripts/probe_venue.py

Exit code is 0 when every checked assumption still holds, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from typing import Any

REST = "https://api.india.delta.exchange"
WSS = "wss://socket.india.delta.exchange"

REQUIRED_CHANNELS = {
    "l2_updates", "l2_orderbook", "all_trades", "v2/ticker",
    "mark_price", "funding_rate",
}
REQUIRED_TICKER_FIELDS = {
    "oi", "oi_contracts", "oi_value_usd", "oi_change_usd_6h",
    "mark_price", "spot_price", "funding_rate",
}
# A3: perpetual is reconstructable, MOVE is not. Guard the gap, not a level count.
MIN_PERP_BOOK_LEVELS = 100


def _get(path: str, **params: str) -> Any:
    url = REST + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    request = urllib.request.Request(url, headers={"User-Agent": "probe-venue"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read())


async def _probe_ws(seconds: int = 30) -> dict[str, Any]:
    import websockets  # imported late so REST checks still run without it

    channels = [
        {"name": "l2_updates", "symbols": ["BTCUSD"]},
        {"name": "l2_orderbook", "symbols": ["BTCUSD"]},
        {"name": "all_trades", "symbols": ["BTCUSD"]},
        {"name": "v2/ticker", "symbols": ["BTCUSD"]},
        {"name": "mark_price", "symbols": ["MARK:BTCUSD"]},
        {"name": "funding_rate", "symbols": ["BTCUSD"]},
    ]
    first: dict[str, Any] = {}
    sequences: list[int] = []
    async with websockets.connect(WSS, open_timeout=15) as ws:
        await ws.send(json.dumps(
            {"type": "subscribe", "payload": {"channels": channels}}))
        try:
            async with asyncio.timeout(seconds):
                while True:
                    message = json.loads(await ws.recv())
                    first.setdefault(message.get("type", "?"), message)
                    if (message.get("type") == "l2_updates"
                            and message.get("action") == "update"):
                        sequences.append(int(message["sequence_no"]))
        except TimeoutError:
            pass
    first["_sequences"] = sequences
    return first


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  — ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    print("A1/A2/A7 — WebSocket channels, L2 integrity, derivatives fields")
    seen = asyncio.run(_probe_ws())
    check(REQUIRED_CHANNELS <= seen.keys(), "A1 required channels present",
          f"missing {sorted(REQUIRED_CHANNELS - seen.keys())}"
          if not REQUIRED_CHANNELS <= seen.keys() else "")

    l2 = seen.get("l2_updates", {})
    check({"action", "sequence_no", "cs"} <= l2.keys(),
          "A2 l2_updates carries action/sequence_no/cs")

    sequences = seen.get("_sequences") or []
    gaps = [(a, b) for a, b in zip(sequences, sequences[1:]) if b != a + 1]
    check(len(sequences) >= 2 and not gaps, "A2 sequence numbers are contiguous",
          f"{len(gaps)} gap(s) in {len(sequences)} updates" if gaps else
          f"{len(sequences)} updates observed")

    ticker = seen.get("v2/ticker", {})
    check(REQUIRED_TICKER_FIELDS <= ticker.keys(),
          "A7 ticker exposes OI and funding",
          f"missing {sorted(REQUIRED_TICKER_FIELDS - ticker.keys())}"
          if not REQUIRED_TICKER_FIELDS <= ticker.keys() else "")

    print("\nA3 — book depth: perpetual reconstructable, MOVE quote-only")
    perp = _get("/v2/l2orderbook/BTCUSD").get("result") or {}
    perp_levels = min(len(perp.get("buy") or []), len(perp.get("sell") or []))
    check(perp_levels >= MIN_PERP_BOOK_LEVELS,
          "A3 BTCUSD perpetual has a reconstructable book",
          f"{perp_levels} levels/side")

    products = (_get("/v2/products", contract_types="move_options",
                     states="live", page_size="100").get("result") or [])
    moves = [p for p in products if str(p.get("symbol", "")).startswith("MV-BTC-")]
    if moves:
        book = _get(f"/v2/l2orderbook/{moves[0]['symbol']}").get("result") or {}
        move_levels = min(len(book.get("buy") or []), len(book.get("sell") or []))
        check(move_levels < perp_levels / 10,
              "A3 MOVE book is thin enough to stay quote-only",
              f"{moves[0]['symbol']}: {move_levels} levels/side vs perp {perp_levels}")
    else:
        print("  [SKIP] A3 MOVE — no live MV-BTC contract right now")

    print("\nA4 — rate-limit headers")
    print("  [INFO] Delta India exposes none; enforce a client-side limiter")

    print(f"\n{'PASS — assumptions hold' if not failures else 'FAIL: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

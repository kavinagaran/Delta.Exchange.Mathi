# Assumptions register

Every assumption the Trend Engine build depends on, with how it was checked and
what it gates. Verified empirically against the live venue on **2026-07-25**
unless noted. Re-verify anything marked OPEN before the phase it gates.

Reproduce the probes with `scripts/probe_venue.py` (Phase 2).

---

## A1 — Delta India exposes a public WebSocket with the channels we need
**Status: CONFIRMED** · gates Phase 2 entirely

Endpoint `wss://socket.india.delta.exchange`. A 35-second subscription to
`BTCUSD` returned, unauthenticated:

| Channel | Messages / 35 s | Notes |
|---|---:|---|
| `l2_updates` | 104 | snapshot + incremental, see A2 |
| `all_trades` | 59 | plus one `all_trades_snapshot` |
| `l2_orderbook` | 36 | full-book refresh, carries `last_sequence_no` |
| `mark_price` | 19 | subscribe as `MARK:BTCUSD`, carries `price_band`, `best_bid/ask`, `annualized_basis` |
| `v2/ticker` | 7 | see A7 |
| `funding_rate` | 2 | `funding_rate`, `funding_rate_8h`, `predicted_funding_rate`, `next_funding_realization`, `funding_interval` |
| `candlestick_5m` | — | available; `candle_start_time`, OHLCV, `resolution` |

Timestamps are microseconds since epoch.

## A2 — `l2_updates` is incremental with sequence number and checksum
**Status: CONFIRMED (checksum algorithm OPEN — see A2b)** · gates §6.3, criterion 4

```
keys: action, asks, bids, cs, sequence_no, spot_index, symbol, timestamp, type
action = "snapshot" | "update"
```

Observed `sequence_no` incrementing by exactly 1 across consecutive updates
(7766407 → 7766408). Every message carries `cs`, an unsigned 32-bit integer.

**Level encoding differs by action — a real implementation trap:**
- `snapshot` bids/asks: `["63935.0", "2487"]` (positional `[price, size]`)
- `update` bids/asks: same positional form, partial level lists (13–23 levels)

The REST `/v2/l2orderbook/<symbol>` book uses a *different* shape —
`{"size": 1688, "depth": "1688", "price": "192.0"}` — and `depth` may arrive in
scientific notation (`"3.38E+3"`). Normalise both into one internal form and
parse every numeric with `Decimal`, never `float`.

### A2b — checksum formulation
**Status: OPEN** · gates checksum validation only, **not** Phase 2 as a whole

`cs` is present and changes per update, but 69 candidate formulations failed to
reproduce it against a live snapshot: CRC32 over top-{10,20,25,50,100,all}
levels, bids-then-asks / asks-then-bids / interleaved, `:` / `,` / no
separator, `price:size` and `size:price`, and integer-normalised prices.

Documentation lookup was unavailable at the time of writing (tooling fault, not
a docs gap). **Resolve before implementing checksum validation.**

Decision: Phase 2 ships **sequence-gap detection as the primary integrity
gate** — a gap triggers resubscribe-and-rebuild, and order-flow features are
marked unavailable until the book is valid again. Checksum validation is added
behind a config flag once the algorithm is confirmed. A missed sequence number
catches every dropped or reordered message; the checksum additionally catches
silent corruption, which is the rarer failure. Shipping seq-gap first is
defensible; claiming checksum validation we cannot compute would not be.

## A3 — MOVE contracts have book depth worth reconstructing
**Status: REFUTED — MOVE stays quote-only** · gates §9.4 scope, criterion 4

| Instrument | Book levels (buy / sell) |
|---|---|
| `BTCUSD` perpetual | **2217 / 2049** |
| `MV-BTC-63800-250726` | 10 / 10 |
| `MV-BTC-64000-250726` | 11 / 12 |
| `MV-BTC-64200-250726` | 11 / 12 |

Top-of-book sizes across three different MOVE strikes were 1688/1692/1677/1681
— near-identical, consistent with a single market maker quoting the series.

**Consequence.** L2 reconstruction and order-flow features are built for
`BTCUSD` perpetual only. MOVE is consumed as quotes via REST `/v2/tickers`, as
the existing code already does. Acceptance criterion 4 ("reconstructs and
validates the order book where supported") is met for the instrument where a
book meaningfully exists; the narrowing is deliberate and recorded here, not
silent. Order-flow microstructure inferred from a 10-level single-maker book
would be noise presented as signal.

## A4 — Shared REST rate-limit budget across four processes
**Status: PARTIALLY VERIFIED — quantify before Phase 2 capture** · gates P2 design

`/v2/products` returned 200 with **no rate-limit headers exposed** (no
`X-RATE-LIMIT-*`, no `Retry-After`). Budget therefore cannot be observed from
responses; it must be inferred from documentation and enforced client-side.

Four processes share one API key's budget: `dashboard.py`,
`Delta_Straddle_Live.py`, `tp_monitor.py`, and the new engine. The engine adds
continuous WebSocket traffic plus periodic REST bootstrap.

**Action for Phase 2:** instrument 24 h of current REST call volume, and put a
shared client-side token-bucket limiter in front of the engine's REST client.
The engine must never be the reason a live order or a TP-monitor poll is
throttled. Prefer WebSocket over REST polling wherever a channel exists.

## A5 — Every pinned dependency has a Python 3.14 wheel
**Status: CONFIRMED** · gates P2 pins, P5 pyarrow, P6 ML

Runtime is **Python 3.14.3** (`C:\Program Files\Python314`), newer than the
spec's 3.12+ target. Already installed globally: `websockets 16.0`,
`fastapi 0.138.1`, `uvicorn 0.49.0`, `pydantic 2.13.4`, `pydantic-settings 2.14.2`,
`httpx 0.28.1`, `SQLAlchemy 2.0.51`, `numpy 2.4.4`, `pandas 3.0.2`,
`pytest 9.1.1`, `pytest-asyncio 1.4.0`.

Missing but available: `alembic 1.18.5`, `orjson 3.11.9`, `structlog 26.1.0`,
`hypothesis 6.161.2`, `ruff 0.16.0`, `mypy 2.3.0`, `freezegun 1.5.5`,
`pyarrow 25.0.0`.

Install into a dedicated `.venv-engine`, never the global interpreter — the
legacy stack runs on global site-packages and must not have `numpy`/`pandas`
moved under it by an engine install. Phase 6 ML libraries (`scikit-learn`,
`lightgbm`) are unverified for 3.14 and remain descopable.

## A6 — EC2 has disk headroom for raw event capture
**Status: OPEN — cannot verify from this workstation** · gates P2 retention

Run on the EC2 host before enabling capture:

```bash
df -h /
```

Then size one day of capture from a local sample before turning it on in
production. `data/` must be gitignored, and `scripts/prune_data.py` must exist
*before* first capture, not after the volume fills. Hard stop: stop raw capture
(not signal generation) below 2 GB free.

## A7 — Open interest and funding cadence
**Status: CONFIRMED** · gates §9.5 feature availability

`v2/ticker` carries everything the derivatives feature group needs, on the
WebSocket, with no extra REST cost:

```
oi = 1186.5010            oi_contracts = 1186501
oi_value_usd = 75899519.7692                oi_change_usd_6h = 3713941.1200
mark_price = 63941.76852037                 spot_price = 63969.8
funding_rate = 0.01       turnover_usd, volume, greeks, price_band, tick_size
```

`oi_change_usd_6h` is supplied by the venue, so the price×OI interpretation
table in §9.5 is computable without maintaining our own OI history. The
dedicated `funding_rate` channel adds `predicted_funding_rate` and
`next_funding_realization`, giving "time until funding" directly.

## A8 — nginx in front of `mathibot.duckdns.org` needs no change
**Status: OPEN — verify on EC2** · gates nothing if the engine stays loopback

The engine binds `127.0.0.1:5055` and is never proxied. Confirm no config
change is needed, and confirm port 5055 is not already taken on the host.

## A9 — `/v2/positions/margined` reports `margin` and `liquidation_price`
**Status: OPEN — cannot verify from this workstation** · gates Exposure page
liquidation-buffer display only, not the reconciliation card

The Positions/Exposure page (`/api/all-positions`) reads `p.get("margin")` and
`p.get("liquidation_price")` from the raw venue response. A live probe from
this workstation returned `ip_not_whitelisted_for_api_key` (client IP not on
the key's allowlist — this only works from the EC2 host that key is scoped
to), and WebFetch/WebSearch were both unavailable at the time of writing
(tooling fault, same as A2b). Field names are unconfirmed.

**Consequence, by design:** both reads are defensive — `float(x or 0) or None`
— so a wrong or absent field name degrades to "not reported" in the UI
("—" for margin/liquidation/distance-to-liq) rather than a fabricated or
crashing value. Confirm the real field names from the EC2 host (or official
docs) before trusting the liquidation-buffer column for anything beyond
"the exchange reported a number and it looked plausible."

---

## Summary

| # | Assumption | Status | Gates |
|---|---|---|---|
| A1 | Public WS with required channels | **CONFIRMED** | Phase 2 |
| A2 | Incremental L2 + sequence + checksum | **CONFIRMED** | §6.3, criterion 4 |
| A2b | Checksum formulation reproducible | **OPEN** | checksum validation only |
| A3 | MOVE book worth reconstructing | **REFUTED** | §9.4 → perp only |
| A4 | Rate-limit budget | **PARTIAL** | P2 design |
| A5 | Python 3.14 wheels | **CONFIRMED** | P2 pins |
| A6 | EC2 disk headroom | **OPEN** | P2 retention |
| A7 | OI / funding on WS | **CONFIRMED** | §9.5 |
| A8 | nginx unchanged | **OPEN** | — |
| A9 | positions/margined margin/liquidation_price fields | **OPEN** | Exposure liq-buffer display |

Nothing OPEN blocks the start of Phase 2. A2b narrows one integrity check, A6
and A8 are operational checks on the deployment host. A9 narrows one display
column on the Exposure page — degrades to "—", never fabricates a number.

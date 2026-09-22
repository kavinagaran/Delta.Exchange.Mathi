# Sell-ATM-MOVE leg — measurement (2026-07-26)

The walk-forward backtest models the **perpetual** ([ADR 0005](adr/0005-backtest-scope.md)),
so a score inside the sideways band produces `direction = 0` and no simulated
trade. That leaves the sideways leg — **41.2% of all decisions**, and the only
leg with unbounded loss — completely unmeasured. This is that measurement.

## Method

Short an ATM MOVE on entering `SHORT_MOVE`; buy it back on zone change
(`zones.should_exit` — HOLD does not end an episode). Per episode:

```
P&L  ≈  theta earned  −  |S_exit − S_entry|
```

Premium is anchored to a **real observed quote**, not invented: at
2026-07-26 04:02 UTC, `MV-BTC-64400-260726` marked **$236.46** with 7.96h to
expiry and spot $64,543. The pricing model `0.7979·S·σ·√(T/8760)` reproduces
that at **$237.1** — so the premium curve is calibrated to the live venue.

**201 episodes over ~38 days.** Median duration 1.5h, mean 2.32h.

## Result

| Sold at implied vol | Mean theta | Mean realised \|ΔS\| | Net/episode | Win rate | Total |
|---|---|---|---|---|---|
| 40.9% (= realised, fair) | $111.9 | $262.8 | **−$150.8** | 28.4% | −$30,321 |
| 45% (+10% VRP) | $123.1 | $262.8 | −$139.6 | 31.8% | −$28,066 |
| 50% (+22% VRP) | $136.8 | $262.8 | −$125.9 | 33.3% | −$25,316 |
| 60% (+47% VRP) | $164.2 | $262.8 | −$98.6 | 35.3% | −$19,816 |
| 70% (+71% VRP) | $191.5 | $262.8 | −$71.2 | 37.8% | −$14,316 |

**Negative at every assumption tested**, including a +71% variance risk
premium far beyond anything crypto MOVE actually pays.

A first pass priced premium at *today's* 15.2% implied against a 40.9%
realised sample — unfair to the strategy, and corrected above. The corrected
numbers are the ones that count, and they still lose.

## Why — this is structural, not a bad sample

The episode ends when `|score|` leaves the sideways band, which is
**precisely when price starts moving**. So the strategy systematically
collects small theta through the calm and then buys back into the breakout:
realised move at exit ($262.8) is **2.3× the theta earned** at fair vol.
That is the classic short-gamma failure, and it is caused by the exit rule
itself, not by the market sampled.

## Limits of this measurement

- **The stop loss is not modelled.** A stop caps the tail but also locks
  losses in; with a mean this negative and a 28% win rate it is implausible
  that a stop reverses the sign, but that is an argument, not a measurement.
- Single ~38-day window; MOVE is priced by model between the calibration
  anchor points rather than from a historical MOVE quote series (Delta serves
  no MOVE history — the contracts expire daily).
- Early-exit buyback uses time-value decay plus intrinsic, not a full
  E|S_T−K| revaluation.

None of these plausibly flip a −$150/episode mean at a 28% win rate.

## Bearing on cutover

The directional legs were separately re-run against the new 35/25 bands:
**1 of 4 folds positive out-of-sample** (+5,928 / −1,012 / −1,714 / −2,749).

So both legs of the 2026-07-26 spec now have evidence, and neither supports
going live: the directional side is indistinguishable from noise, and the
sideways side is negative across every premium assumption tested.

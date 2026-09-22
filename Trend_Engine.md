# Pro-Grade Build Prompt: BTC Trend Engine

> **Purpose:** Paste this prompt into a capable coding agent to design, implement, test, and document a production-grade Bitcoin Trend Engine. The engine must produce reliable, explainable market-state and trend signals for BTC trading and integrate safely with an existing Delta Exchange bot that can LONG or SHORT MOVE contracts.

---

## 1. Role and Mission

Act as a **senior quantitative developer, algorithmic-trading systems architect, market-microstructure engineer, ML engineer, and production-risk specialist**.

Design and build a modular, production-grade **BTC Trend Engine** that:

1. Detects whether BTC is in an upward trend, downward trend, range, breakout, volatility shock, low-liquidity state, or degraded-data state.
2. Estimates direction, trend strength, confidence, expected return, expected absolute movement, volatility, jump risk, and invalidation level.
3. Produces a structured `TrendSnapshot` for downstream trading systems.
4. Remains completely separate from position sizing, risk approval, and order execution.
5. Integrates with an existing Delta Exchange bot that already supports `LONG MOVE` and `SHORT MOVE`.
6. Uses realistic trading costs, market data, risk constraints, and event-driven replay.
7. Fails closed whenever data, exchange state, account state, or internal state is uncertain.

Do not build a simplistic indicator crossover bot. Build a **regime-aware, multi-timeframe, cost-aware, explainable decision engine**.

---

## 2. Existing Context

Assume the following unless the existing repository indicates otherwise:

- Primary market: Bitcoin.
- Exchange: Delta Exchange.
- Existing bot functionality: LONG MOVE and SHORT MOVE orders already exist.
- The Trend Engine should be instrument-independent wherever practical.
- Preferred implementation: Python 3.12+, asynchronous architecture, strict typing, testable modules, structured logging, configuration-driven behavior, and Docker support.
- The engine may later support BTC perpetual futures in addition to MOVE contracts.

Before modifying anything:

1. Inspect the existing repository and folder structure.
2. Identify existing exchange clients, configuration files, order-management logic, data models, databases, tests, and logging conventions.
3. Reuse compatible modules instead of duplicating functionality.
4. Do not overwrite unrelated files.
5. Create a clear implementation plan based on the actual repository.
6. Confirm all endpoint names, WebSocket channels, message schemas, rate limits, authentication methods, order flags, and exchange behavior against the **current official Delta Exchange API documentation** before coding them.

---

## 3. Non-Negotiable Design Principles

The implementation must follow these principles:

### 3.1 Separation of concerns

Maintain strict boundaries between:

- Market-data ingestion.
- Data-quality validation.
- Feature computation.
- Regime classification.
- Direction and magnitude forecasting.
- Signal aggregation.
- Risk approval.
- Position sizing.
- Order execution.
- Account and position reconciliation.
- Backtesting.
- Monitoring and alerts.

The Trend Engine must never directly place an order.

### 3.2 Fail closed

Block new entries when any of the following occurs:

- Public feed is stale.
- Private feed is stale.
- Order book cannot be reconstructed or verified.
- Exchange system status is unsafe.
- Clock drift exceeds tolerance.
- Account state is not reconciled.
- Open orders or positions do not match local state.
- Feature computation is incomplete.
- Model artifacts are missing or incompatible.
- Risk manager is unavailable.
- Data quality is below the configured threshold.

### 3.3 Explainability

Every signal must include:

- Regime.
- Direction.
- Trend score.
- Confidence.
- Expected return.
- Expected absolute movement.
- Volatility estimate.
- Jump probability.
- Entry permission.
- Invalidation level.
- Signal expiry.
- Data-quality status.
- Human-readable reason codes.
- Model/version identifiers.

### 3.4 Cost awareness

No signal is tradeable unless expected edge exceeds:

- Exchange fees.
- Bid-ask spread.
- Expected slippage.
- Funding where relevant.
- Execution uncertainty.
- Configured safety margin.

### 3.5 Conservative live behavior

- No martingale.
- No averaging down.
- No uncontrolled online learning.
- No automatic parameter optimization in production.
- No use of future data.
- No candle-close fills in backtests unless that is how the actual order would have executed.
- No assumption that a profitable backtest guarantees live profitability.

---

## 4. High-Level Architecture

Implement the following logical flow:

```text
Delta Public WebSocket + REST Bootstrap
                    ↓
          Market Data Normalizer
                    ↓
      Data Quality and Clock Monitor
                    ↓
      Event Store + Candle Aggregator
                    ↓
             Feature Engine
                    ↓
    Regime / Direction / Magnitude Models
                    ↓
          Signal Ensemble + Hysteresis
                    ↓
             TrendSnapshot
                    ↓
              Risk Manager
                    ↓
         Order Management System
                    ↓
           Delta Exchange Execution
                    ↓
      Private Feed + REST Reconciliation
```

The Trend Engine owns everything up to `TrendSnapshot`. Risk and execution are downstream consumers.

---

## 5. Core Data Contracts

Use typed, versioned schemas. Pydantic models or equivalent are preferred.

### 5.1 Market event

```python
class MarketEvent:
    event_id: str
    exchange: str
    symbol: str
    event_type: str
    exchange_timestamp: datetime
    receive_timestamp: datetime
    sequence_id: int | None
    payload: dict
    schema_version: str
```

### 5.2 Feature vector

```python
class FeatureVector:
    symbol: str
    as_of: datetime
    horizon: str
    features: dict[str, float | int | bool | None]
    missing_features: list[str]
    data_quality_score: float
    feature_set_version: str
```

### 5.3 Trend snapshot

```json
{
  "symbol": "BTCUSD",
  "timestamp": "2026-07-25T10:15:00Z",
  "regime": "TREND_UP",
  "direction": 1,
  "trend_score": 72.0,
  "confidence": 0.68,
  "forecast_horizon_seconds": 900,
  "expected_return_bps": 18.0,
  "expected_absolute_move_bps": 43.0,
  "forecast_volatility_bps": 39.0,
  "jump_probability": 0.07,
  "invalidation_price": 116250.0,
  "suggested_stop_bps": 29.0,
  "entry_allowed": true,
  "signal_ttl_seconds": 300,
  "reason_codes": [
    "1H_TREND_UP",
    "15M_BREAKOUT_CONFIRMED",
    "5M_FLOW_CONFIRMATION"
  ],
  "data_quality": "OK",
  "feature_set_version": "v1.0.0",
  "model_version": "trend-rules-v1.1.0"
}
```

### 5.4 Risk decision

```python
class RiskDecision:
    approved: bool
    reason_codes: list[str]
    maximum_notional: Decimal
    maximum_quantity: Decimal
    stop_price: Decimal | None
    daily_loss_remaining: Decimal
    risk_model_version: str
```

### 5.5 Order intent

```python
class OrderIntent:
    intent_id: str
    strategy_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: str
    maximum_slippage_bps: Decimal
    signal_id: str
    expires_at: datetime
```

---

## 6. Market-Data Layer

Build a resilient asynchronous market-data layer.

### 6.1 Required data categories

Collect, normalize, and persist where available:

- Trades.
- Best bid and ask.
- Level-2 order-book snapshots and updates.
- Candlesticks.
- Mark price.
- Index or spot reference price.
- Funding rate and funding countdown.
- Open interest and open-interest change.
- Instrument metadata.
- Exchange system status.
- Private orders.
- Fills.
- Positions.
- Balance or margin data.
- Liquidation or risk events, if exposed.

### 6.2 Time handling

For every message, retain:

- Exchange timestamp.
- Local receipt timestamp.
- Processing timestamp.
- Sequence number where available.
- Connection/session ID.

Use UTC internally. Never use local timezone for calculation or storage.

### 6.3 Order-book reconstruction

Where supported:

- Bootstrap the order book from a snapshot.
- Apply incremental updates in order.
- Validate sequence continuity.
- Validate checksum.
- Rebuild on mismatch.
- Mark order-flow features unavailable until the book is valid.

### 6.4 Reconnection behavior

On disconnect:

1. Block new entries.
2. Reconnect with capped exponential backoff and jitter.
3. Re-authenticate private channels.
4. Rebuild order-book state.
5. Reconcile open orders and positions through REST.
6. Resume only after all health checks pass.

### 6.5 Stale-data detection

Use dynamic thresholds based on recent message cadence:

```text
warning_threshold = 3 × rolling median message interval
hard_threshold = max(warning_threshold, configured safety floor)
```

Example behavior:

- Stale L1: block entries.
- Stale L2: disable order-flow-dependent entries.
- Stale private feed: cancel entry orders and reconcile through REST.
- Stale mark price: block leveraged entries.
- Unsafe exchange state: cancel pending entry orders and remain flat.

---

## 7. Multi-Timeframe Framework

Use the following initial functional timeframes, configurable through YAML or environment variables:

| Timeframe | Function |
|---|---|
| 1-hour | Structural bull or bear environment |
| 30-minute | Primary intraday trend direction and strength |
| 15-minute | Setup, breakout, and continuation quality |
| 5-minute | Entry trigger and invalidation refinement |
| Tick / 1-minute | Order flow, liquidity, and execution timing |

Rules:

1. Higher timeframes determine whether a trend is structurally valid.
2. Lower timeframes determine whether a tradeable setup exists now.
3. Tick-level data confirms or rejects execution timing.
4. A 1-minute spike must not override a contradictory 1-hour regime.
5. All timeframes must be generated from timestamp-safe event data.
6. Use closed candles for decisions unless the feature is explicitly designed for live intrabar use.

Example long alignment:

```text
1H structure       = Bullish or neutral
30M trend          = Bullish
15M setup          = Bullish continuation or breakout
5M trigger         = Bullish close or pullback recovery
Order flow         = Positive or non-opposing
Liquidity          = Acceptable
Data quality       = Healthy
```

---

## 8. Regime Classification

Classify the market into exactly one primary regime at a time:

```text
TREND_UP
TREND_DOWN
RANGE
BREAKOUT_UP
BREAKOUT_DOWN
HIGH_VOL_SHOCK
LOW_LIQUIDITY
DEGRADED
```

### 8.1 Regime inputs

Use a compact, non-redundant feature set:

- Directional efficiency ratio.
- EMA slope normalized by ATR.
- Separation between fast and slow EMA normalized by ATR.
- Price distance from rolling or anchored VWAP.
- ADX or equivalent trend-strength measure.
- Donchian-channel position.
- Realized-volatility percentile.
- Short-term versus long-term realized volatility.
- Range compression and subsequent expansion.
- Breakout candle quality.
- Spread deterioration.
- Depth deterioration.
- Order-flow persistence.
- Mark-to-index divergence.
- Funding extremity.
- Open-interest behavior.

### 8.2 Hysteresis

Prevent regime flapping.

Example:

```text
Enter TREND_UP when trend score >= +60
Remain TREND_UP until trend score < +30
Enter TREND_DOWN when trend score <= -60
Remain TREND_DOWN until trend score > -30
```

Regime transitions must be explicit, testable, and logged.

### 8.3 Range treatment

The Trend Engine must return neutral or deny trend entries in `RANGE`. Do not mix mean-reversion logic into the same strategy module.

---

## 9. Feature Engineering

Avoid using many indicators that measure the same phenomenon. Organize features into independent groups.

### 9.1 Higher-timeframe trend features

Implement:

- EMA20 versus EMA50.
- EMA20 and EMA50 slope divided by ATR.
- Market structure: higher highs/higher lows or lower highs/lower lows.
- Donchian 20- or 30-bar position.
- Price relative to anchored VWAP.
- Price distance from trend reference divided by ATR.
- Directional efficiency ratio.
- Trend persistence over configurable lookback.

Suggested normalized formulas:

```text
ema_distance = (EMA_fast - EMA_slow) / ATR
ema_slope = (EMA_fast_now - EMA_fast_n_bars_ago) / ATR
vwap_distance = (close - VWAP) / ATR
```

### 9.2 Setup-quality features

On the 15-minute timeframe, calculate:

- Breakout above or below recent structure.
- Breakout body size divided by ATR.
- Close location within the candle range.
- Relative volume versus rolling median.
- Number of failed breakout attempts.
- Volatility compression before breakout.
- Distance from VWAP.
- Available space before major support or resistance.
- Retest success or failure.
- Trend continuation versus exhaustion score.

### 9.3 Entry-timing features

On the 5-minute timeframe, calculate:

- Pullback to EMA, VWAP, or breakout level.
- Recovery close in trend direction.
- Momentum acceleration or deceleration.
- Close beyond local structure.
- Rejection wick quality.
- Short-term relative volume.
- Distance from mean to prevent chasing.
- Recent adverse excursion.
- Trigger candle quality.

### 9.4 Order-flow features

Where order-book quality permits, calculate:

- Taker buy volume minus taker sell volume.
- Cumulative volume delta.
- Trade-flow imbalance over 10 seconds, 30 seconds, 2 minutes, and 5 minutes.
- Bid versus ask depth imbalance.
- Microprice relative to midpoint.
- Spread in basis points.
- Depth within configurable basis-point bands.
- Book pressure persistence.
- Order-book replenishment rate.
- Large aggressive trade concentration.
- Short-term cancellation intensity, if reliable.
- Slippage estimate for target order size.

Use order flow as confirmation and magnitude information, not as an unconditional standalone entry signal.

### 9.5 Derivatives and crowding features

Calculate where available:

- Mark price minus index price.
- Perpetual basis.
- Funding rate percentile.
- Time until funding.
- Open-interest change.
- Price change combined with open-interest change.
- Volume and liquidation-like spike measures.
- Abnormal basis expansion.
- Funding and OI divergence.

Interpret open interest jointly with price:

| Price | Open interest | Candidate interpretation |
|---|---|---|
| Rising | Rising | New long participation |
| Falling | Rising | New short participation |
| Rising | Falling | Short covering |
| Falling | Falling | Long liquidation |

Treat these as probabilistic context, never certain explanations.

### 9.6 Volatility and jump features

Implement:

- ATR.
- Realized volatility over multiple horizons.
- Parkinson or range-based volatility where appropriate.
- Volatility-of-volatility.
- Volatility percentile.
- Upside and downside semivariance.
- Short-term versus long-term volatility ratio.
- Jump score based on standardized returns and volume.
- Gap or discontinuity detection.

---

## 10. Model Architecture

Build three separate prediction heads. Do not start with deep reinforcement learning.

### 10.1 Model 1: Regime model

Output probabilities for:

```text
P(TREND_UP)
P(TREND_DOWN)
P(RANGE)
P(BREAKOUT_UP)
P(BREAKOUT_DOWN)
P(HIGH_VOL_SHOCK)
```

Initial production version:

- Transparent rule-based state machine, or
- Calibrated logistic regression, or
- Small gradient-boosted tree.

A Hidden Markov Model may be included as a research benchmark, not as an unvalidated production dependency.

### 10.2 Model 2: Direction model

Output:

- Probability of a sufficiently profitable upward move.
- Probability of a sufficiently profitable downward move.
- Expected net return over 5-minute, 15-minute, and 60-minute horizons.

Use cost-aware labels:

```text
UP:
future_return > fees + spread + expected_slippage + safety_margin

DOWN:
future_return < -(fees + spread + expected_slippage + safety_margin)

FLAT:
everything between those thresholds
```

Prevent leakage by ensuring labels use strictly future data and features use only information available at prediction time.

### 10.3 Model 3: Magnitude and volatility model

Output:

- Expected absolute move.
- Expected realized volatility.
- Probability of an abnormal jump.
- Expected favourable excursion.
- Expected adverse excursion.
- Optional quantiles such as p10, p50, p90, and p99.

This model is especially important for MOVE-contract decisions.

### 10.4 Model lifecycle

Implement:

- Versioned feature sets.
- Versioned training datasets.
- Versioned model artifacts.
- Reproducible training seeds.
- Calibration reports.
- Feature-importance reports.
- Drift monitoring.
- Rollback capability.
- Champion/challenger evaluation.
- No automatic replacement of the production model without explicit approval.

---

## 11. Initial Transparent Trend Score

The first production version must include an explainable score from `-100` to `+100`.

Initial weighting:

```text
30% higher-timeframe trend
15% market structure
15% lower-timeframe momentum
20% order-flow confirmation
10% breakout quality
10% derivatives context
```

Normalize using:

```text
trend_score = 100 × tanh(raw_score)
```

Initial interpretation:

| Trend score | Meaning |
|---:|---|
| +65 to +100 | Strong bullish trend |
| +35 to +64 | Moderate bullish bias |
| -34 to +34 | No-trade zone |
| -35 to -64 | Moderate bearish bias |
| -65 to -100 | Strong bearish trend |

All weights and thresholds must be configurable and validated using walk-forward testing. Do not optimize them against the entire history.

---

## 12. Signal Generation

### 12.1 Long eligibility

A directional BTC long may be eligible only when all configured conditions pass:

```text
Regime            = TREND_UP or BREAKOUT_UP
Trend score       >= +65
Confidence        >= 0.62
1H and 15M        aligned bullish
5M trigger        confirmed
Expected return   > all-in cost + safety margin
Order flow        positive or non-opposing
Spread/depth      acceptable
Data              fresh and synchronized
Exchange state    healthy
Risk lock         not active
```

### 12.2 Short eligibility

```text
Regime            = TREND_DOWN or BREAKOUT_DOWN
Trend score       <= -65
Confidence        >= 0.62
1H and 15M        aligned bearish
5M trigger        confirmed
Expected net edge > all-in cost + safety margin
Order flow        negative or non-opposing
Spread/depth      acceptable
Data              fresh and synchronized
Exchange state    healthy
Risk lock         not active
```

### 12.3 Signal hysteresis

Use different entry, hold, exit, and reversal thresholds:

```text
Enter long       at score >= +65
Hold long        while score > +25
Exit long        at score <= +25 or invalidation
Consider short   only at score <= -55

Enter short      at score <= -65
Hold short       while score < -25
Exit short       at score >= -25 or invalidation
Consider long    only at score >= +55
```

### 12.4 Signal expiry

Every signal must include a TTL. The consumer must reject expired signals.

### 12.5 Reason codes

Use stable machine-readable reason codes such as:

```text
1H_STRUCTURE_BULLISH
30M_TREND_ALIGNED
1H_EMA_SLOPE_POSITIVE
15M_BREAKOUT_CONFIRMED
5M_PULLBACK_RECOVERY
ORDER_FLOW_BUYER_DOMINANT
SPREAD_TOO_WIDE
DEPTH_INSUFFICIENT
DATA_STALE
REGIME_RANGE
HIGH_VOL_SHOCK
EXPECTED_EDGE_BELOW_COST
```

---

## 13. Exit and Invalidation Logic

Exit logic must be independent from entry logic.

### 13.1 Structural invalidation

For a long position, invalidate when one or more configured conditions occur:

- Price breaks the pullback swing low.
- Price closes below the breakout level.
- Higher-timeframe structure turns bearish.
- Trend score falls below the hold threshold.

Use symmetric logic for shorts.

### 13.2 Volatility stop

Initial research range:

```text
stop_distance = 1.2 to 1.8 × current ATR
```

Determine the production value through chronological walk-forward testing.

### 13.3 Signal deterioration

Exit or de-risk when:

- Trend score deteriorates.
- Order flow reverses materially.
- Regime changes to `RANGE`, `HIGH_VOL_SHOCK`, or `DEGRADED`.
- Expected net edge becomes non-positive.
- Liquidity becomes unsafe.

### 13.4 Time stop

Exit if expected follow-through does not begin within the configured number of setup bars.

### 13.5 Trailing stop

Activate only after unrealized profit exceeds:

```text
fees + spread + estimated slippage + minimum locked-profit buffer
```

---

## 14. Risk Manager

Risk approval must occur after signal generation and before execution.

### 14.1 Risk-based sizing

Use:

```text
risk_capital = account_equity × risk_per_trade
position_notional = risk_capital / stop_distance_percentage
```

Final notional must be the minimum of:

```text
risk_based_notional
available_margin_cap
maximum_leverage_cap
liquidity_cap
portfolio_exposure_cap
instrument_cap
```

### 14.2 Initial configurable safety defaults

Use conservative defaults for initial paper trading and small live deployment:

- Risk per trade: 0.25% to 0.50% of equity.
- Maximum daily realized plus unrealized loss: approximately 1%.
- Maximum weekly loss: approximately 3%.
- Pause after two or three consecutive strategy losses.
- No averaging down.
- No martingale.
- Lower size in breakout and elevated-volatility regimes.
- No entries in `HIGH_VOL_SHOCK`, `LOW_LIQUIDITY`, or `DEGRADED`.

Treat these as configurable starting constraints, not universally optimal values.

### 14.3 Volatility scaling

```text
size_multiplier = target_volatility / forecast_volatility
```

Cap the multiplier to prevent excessive leverage during low-volatility periods.

### 14.4 Kill switches

Implement kill switches for:

- Daily loss breach.
- Weekly loss breach.
- Consecutive losses.
- Unexpected position.
- Reconciliation failure.
- Stale private feed.
- Exchange maintenance.
- Abnormal slippage.
- Latency spike.
- Rapid order rejection.
- Model or feature drift.
- Manual emergency stop.

---

## 15. Execution and Order Management

The execution layer must be separate from the Trend Engine but supplied as an integration interface.

### 15.1 Preferred entry order behavior

Support marketable IOC limit orders with:

- Maximum slippage limit.
- Maximum spread threshold.
- Minimum depth relative to quantity.
- Maximum quote age.
- Signal-expiry validation.
- Partial-fill handling.
- No unlimited price chasing.
- Unique idempotent `client_order_id`.
- Full audit trail.

### 15.2 Order-state machine

Implement an explicit state machine:

```text
CREATED
RISK_APPROVED
SUBMITTED
ACKNOWLEDGED
PARTIALLY_FILLED
FILLED
CANCEL_REQUESTED
CANCELLED
REJECTED
EXPIRED
RECONCILIATION_REQUIRED
```

No impossible state transitions should be permitted.

### 15.3 Reconciliation

After every order event:

- Reconcile local open orders.
- Reconcile fills.
- Reconcile current position.
- Reconcile margin and available balance when relevant.
- Stop new orders on any mismatch.

### 15.4 Mark-price risk

Use exchange-defined mark price for leverage, liquidation-buffer, and risk monitoring where applicable. Never use only the last traded price for liquidation safety.

---

## 16. MOVE Contract Integration

The existing bot can already LONG or SHORT MOVE. Do not replace the MOVE product with a synthetic call-plus-put implementation.

The Trend Engine must expose these inputs to the MOVE decision layer:

```text
mu         = forecast directional drift
sigma      = forecast realized volatility
p_jump     = probability of abnormal movement
regime     = current market regime
confidence = calibrated confidence
```

Apply confidence shrinkage:

```text
drift_used = predicted_drift × calibrated_confidence
```

### 16.1 LONG MOVE eligibility

LONG MOVE may be considered only when:

```text
conservative_expected_payoff
    > executable_ask
    + fees
    + slippage
    + safety_margin
```

Supportive Trend Engine conditions may include:

- High expected absolute movement.
- Breakout or directional trend regime.
- Rising volatility forecast.
- Acceptable jump-risk model.
- Sufficient liquidity.
- Fresh underlying and settlement inputs.

### 16.2 SHORT MOVE eligibility

SHORT MOVE may be considered only when:

```text
executable_bid
    > conservative_upper_fair_value
    + required_edge
```

Also require:

- Low jump probability.
- Stable or falling realized volatility.
- No active breakout or volatility-shock regime.
- Acceptable p99 payoff stress.
- Large liquidation buffer.
- Settlement-proximity restrictions satisfied.
- Adequate liquidity.

### 16.3 MOVE payoff model

Build or integrate a Monte Carlo or distributional payoff estimator using:

- Current BTC price.
- Conservative drift.
- Forecast volatility.
- Jump component.
- Time to settlement.
- Actual MOVE settlement mechanics.
- Trading fees and slippage.
- Model uncertainty.

Output fair value, confidence interval, expected edge, p95/p99 loss, and scenario diagnostics.

---

## 17. Storage and Event Replay

Persist sufficient information for exact event-driven replay.

Store:

- Every trade event.
- L1 and L2 updates.
- Sequence/checksum state.
- Mark and index prices.
- Funding updates.
- Open-interest updates.
- Candle updates.
- Exchange timestamps.
- Local receive timestamps.
- Feature vectors.
- Trend snapshots.
- Risk decisions.
- Orders submitted.
- Acknowledgements.
- Rejections.
- Partial fills.
- Full fills.
- Position changes.
- Balance changes.
- Reconciliation events.
- System-health events.

Recommended storage approach:

- Append-only raw event store.
- Relational database for normalized state and trade records.
- Columnar files such as Parquet for research datasets.
- Optional TimescaleDB or equivalent for time-series queries.

All data models must have migration support and schema versions.

---

## 18. Backtesting Engine

Build an event-driven backtester that reuses production feature, signal, and risk logic.

### 18.1 Execution realism

Model:

- Bid/ask execution.
- Fees.
- Funding.
- Spread.
- Slippage.
- Partial fills.
- IOC cancellation.
- Decision latency.
- Network latency.
- Signal expiry.
- Gaps.
- Market impact approximation.
- Position and margin constraints.
- Rejected orders.
- Data gaps.
- Reconnects.
- Reconciliation delays.

Do not assume a buy fills at candle close. Use the best executable price available at the simulated decision time, adjusted for size and latency.

### 18.2 Chronological validation

Use:

```text
Training period
      ↓
Validation period
      ↓
Rolling walk-forward out-of-sample periods
      ↓
Final untouched holdout
      ↓
Live shadow mode
      ↓
Very small live allocation
```

Never repeatedly tune against the final holdout.

### 18.3 Required performance metrics

Report:

- Net return.
- Net expectancy per trade.
- Profit factor.
- Sharpe and Sortino ratios.
- Deflated Sharpe ratio where feasible.
- Maximum drawdown.
- Recovery factor.
- Calmar ratio.
- Average adverse excursion.
- Average favourable excursion.
- Tail loss.
- Value-at-Risk and expected shortfall where appropriate.
- Long versus short performance.
- Performance by regime.
- Performance by hour and day of week.
- Turnover.
- Fill rate.
- Rejection rate.
- Slippage distribution.
- Fee and funding impact.
- Probability calibration.
- Live-versus-backtest performance decay.

### 18.4 Overfitting controls

Implement:

- Parameter-count tracking.
- Multiple-testing awareness.
- Sensitivity analysis.
- Parameter-stability charts.
- Purged or embargoed validation where label overlap exists.
- Bootstrap confidence intervals.
- Walk-forward stability checks.
- Deflated performance measures where feasible.

Reject a strategy that works only at one narrow parameter combination.

---

## 19. Monitoring and Observability

Provide structured logs, metrics, dashboards, and alerts.

### 19.1 Operational metrics

Monitor:

- WebSocket connection status.
- Message age.
- Processing latency.
- Event backlog.
- Order-book checksum status.
- Clock drift.
- REST error rate.
- Rate-limit usage.
- Private-feed health.
- Reconciliation status.
- Model inference latency.
- Feature missingness.
- Current regime.
- Current trend score.
- Current risk lock status.

### 19.2 Trading metrics

Monitor:

- Signals generated.
- Signals rejected by risk.
- Orders submitted.
- Fill percentage.
- Slippage.
- Fees.
- Funding.
- P&L.
- Drawdown.
- Consecutive losses.
- Position exposure.
- Expected versus realized movement.
- Model calibration.

### 19.3 Drift monitoring

Track:

- Feature distribution drift.
- Prediction distribution drift.
- Confidence drift.
- Regime-frequency drift.
- Slippage drift.
- Hit-rate and calibration drift.
- Performance by model version.

Production drift should create alerts and may reduce size or block entries, but must not trigger uncontrolled retraining.

---

## 20. Configuration

Make all strategy, risk, data-quality, and execution parameters configurable.

Example:

```yaml
engine:
  symbol: BTCUSD
  base_currency: BTC
  quote_currency: USD
  timezone: UTC

signals:
  long_entry_score: 65
  short_entry_score: -65
  long_hold_score: 25
  short_hold_score: -25
  reversal_score: 55
  minimum_confidence: 0.62
  default_ttl_seconds: 300

features:
  ema_fast: 20
  ema_slow: 50
  atr_period: 14
  donchian_period: 20
  order_flow_windows_seconds: [10, 30, 120, 300]

risk:
  risk_per_trade: 0.0025
  maximum_daily_loss: 0.01
  maximum_weekly_loss: 0.03
  maximum_consecutive_losses: 3
  maximum_leverage: 2.0
  averaging_down_allowed: false
  martingale_allowed: false

execution:
  order_type: marketable_limit
  time_in_force: IOC
  maximum_spread_bps: 8
  maximum_slippage_bps: 12
  maximum_quote_age_ms: 500
  allow_price_chasing: false

data_quality:
  fail_closed: true
  maximum_clock_drift_ms: 500
  require_valid_order_book: true
  require_private_feed_for_live_trading: true

move:
  minimum_long_edge_bps: 25
  minimum_short_edge_bps: 35
  maximum_short_jump_probability: 0.05
  use_confidence_shrinkage: true
```

Validate configuration on startup. Refuse to start live trading with unsafe or incomplete settings.

---

## 21. Recommended Project Structure

Adapt to the existing repository, but use the following separation where practical:

```text
btc_trend_engine/
│
├── market_data/
│   ├── delta_public_ws.py
│   ├── delta_private_ws.py
│   ├── delta_rest.py
│   ├── normalizer.py
│   ├── orderbook.py
│   ├── candle_aggregator.py
│   └── data_quality.py
│
├── storage/
│   ├── event_store.py
│   ├── repositories.py
│   ├── feature_store.py
│   └── migrations/
│
├── features/
│   ├── trend.py
│   ├── structure.py
│   ├── volatility.py
│   ├── order_flow.py
│   ├── derivatives.py
│   ├── liquidity.py
│   └── pipeline.py
│
├── models/
│   ├── regime_model.py
│   ├── direction_model.py
│   ├── magnitude_model.py
│   ├── calibration.py
│   ├── registry.py
│   └── artifacts/
│
├── signals/
│   ├── trend_snapshot.py
│   ├── weighted_score.py
│   ├── ensemble.py
│   ├── hysteresis.py
│   └── reason_codes.py
│
├── risk/
│   ├── position_sizer.py
│   ├── exposure_limits.py
│   ├── drawdown_guard.py
│   ├── kill_switch.py
│   └── risk_manager.py
│
├── execution/
│   ├── oms.py
│   ├── order_state_machine.py
│   ├── ioc_executor.py
│   ├── slippage_guard.py
│   └── reconciliation.py
│
├── move/
│   ├── payoff_model.py
│   ├── monte_carlo.py
│   ├── long_move_policy.py
│   └── short_move_policy.py
│
├── backtest/
│   ├── event_replay.py
│   ├── fill_simulator.py
│   ├── latency_model.py
│   ├── walk_forward.py
│   └── performance.py
│
├── monitoring/
│   ├── health.py
│   ├── metrics.py
│   ├── alerts.py
│   └── drift.py
│
├── api/
│   ├── app.py
│   ├── routes.py
│   └── schemas.py
│
├── config/
│   ├── default.yaml
│   ├── paper.yaml
│   └── live.yaml
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── replay/
│   └── property/
│
├── scripts/
│   ├── collect_data.py
│   ├── run_shadow.py
│   ├── train_models.py
│   ├── backtest.py
│   └── reconcile.py
│
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── README.md
└── .env.example
```

---

## 22. APIs and Interfaces

Provide clean interfaces for the existing bot.

Minimum endpoints or equivalent internal service methods:

```text
GET  /health
GET  /status
GET  /trend/latest?symbol=BTCUSD
GET  /trend/history?symbol=BTCUSD
GET  /regime/latest?symbol=BTCUSD
GET  /features/latest?symbol=BTCUSD
GET  /risk/status
POST /signals/evaluate
POST /admin/kill-switch
POST /admin/resume
```

A message-bus interface may be used instead of HTTP, but the schema must remain documented and versioned.

---

## 23. Testing Requirements

### 23.1 Unit tests

Test:

- Indicator calculations.
- ATR normalization.
- Market-structure detection.
- Order-flow imbalance.
- Trend score.
- Hysteresis.
- Regime transitions.
- Cost-aware labels.
- Position sizing.
- Stop calculation.
- TTL validation.
- State-machine transitions.
- MOVE payoff calculations.

### 23.2 Integration tests

Test:

- Public WebSocket reconnect.
- Private WebSocket reconnect.
- REST bootstrap.
- Order-book rebuild.
- Sequence gaps.
- Checksum failure.
- Stale-feed behavior.
- Position reconciliation.
- Partial fill.
- Rejection.
- Duplicate order acknowledgement.
- Kill-switch behavior.

### 23.3 Replay tests

Build deterministic replay fixtures for:

- Normal bullish trend.
- Normal bearish trend.
- Range-bound market.
- False breakout.
- Liquidation cascade.
- Sudden spread widening.
- Order-book corruption.
- Data outage.
- Private-feed outage.
- Exchange maintenance.
- High-latency interval.

### 23.4 Property and invariant tests

Examples:

- Position size must never exceed configured caps.
- An expired signal must never create an order.
- `DEGRADED` regime must never permit a new entry.
- A reconciliation mismatch must activate a risk lock.
- Order states must follow valid transitions.
- No calculation may use a timestamp later than the decision timestamp.

Aim for high coverage on safety-critical modules, but prioritize meaningful tests over cosmetic coverage percentages.

---

## 24. Security and Secrets

- Never hard-code API keys.
- Use environment variables or a secure secret manager.
- Provide `.env.example`, never a real `.env`.
- Redact secrets and personally identifiable account data from logs.
- Validate all external payloads.
- Use request signing exactly as specified by current official documentation.
- Use least-privilege exchange keys.
- Disable withdrawal permission on trading keys.
- Separate paper and live credentials.
- Require an explicit live-mode flag.
- Require an additional production confirmation setting before live order placement.

---

## 25. Implementation Phases

Execute the work in phases. Each phase must produce working, tested software.

### Phase 1: Repository assessment and technical design

Deliver:

- Existing-code assessment.
- Architecture decision record.
- Integration map.
- Risks and assumptions.
- Implementation plan.

### Phase 2: Market data and data quality

Deliver:

- Public/private data clients.
- Normalized events.
- Order-book reconstruction.
- Staleness detection.
- Event persistence.
- Replay fixtures.

### Phase 3: Transparent feature and regime engine

Deliver:

- Multi-timeframe candles.
- Feature pipeline.
- Rule-based regime classifier.
- Weighted trend score.
- TrendSnapshot API.
- Explanation/reason codes.

### Phase 4: Risk and execution integration

Deliver:

- Risk manager.
- Sizing rules.
- Kill switches.
- OMS integration contract.
- Reconciliation.
- Paper-trading workflow.

### Phase 5: Event-driven backtesting

Deliver:

- Replay engine.
- Realistic fill simulator.
- Cost model.
- Walk-forward framework.
- Metrics and report generation.

### Phase 6: Statistical and ML models

Deliver:

- Regime model benchmark.
- Direction model.
- Magnitude/volatility model.
- Calibration.
- Model registry.
- Drift reports.
- Champion/challenger comparison against the transparent baseline.

### Phase 7: MOVE integration

Deliver:

- Trend inputs to MOVE policy.
- Fair-value/payoff estimator.
- LONG MOVE eligibility logic.
- SHORT MOVE eligibility logic.
- Stress tests.

### Phase 8: Shadow and controlled live deployment

Deliver:

- Shadow mode.
- Alerting.
- Live-versus-simulated comparison.
- Very-small-size deployment controls.
- Rollback procedure.

Do not proceed to unrestricted live operation merely because a backtest is profitable.

---

## 26. Acceptance Criteria

The system is acceptable only when all of the following are demonstrated:

1. It generates versioned, explainable TrendSnapshot objects.
2. Regime, direction, magnitude, and risk are separate modules.
3. It blocks trading on stale or inconsistent data.
4. It reconstructs and validates the order book where supported.
5. It performs REST reconciliation after reconnects and state mismatches.
6. It uses cost-aware labels and cost-aware trade eligibility.
7. Backtests simulate bid/ask, fees, slippage, latency, and partial fills.
8. Walk-forward results are reported separately from training results.
9. Parameter sensitivity is shown.
10. Risk limits cannot be bypassed by the signal engine.
11. The kill switch is tested.
12. An expired signal cannot create an order.
13. Paper and live modes are unmistakably separated.
14. The existing LONG/SHORT MOVE functionality continues to work.
15. No unrelated repository files are overwritten.
16. Unit, integration, replay, and invariant tests pass.
17. Setup, configuration, operations, and recovery procedures are documented.
18. The system can be run in shadow mode without placing orders.
19. Every order can be traced back to its signal, features, model version, and risk decision.
20. The implementation is maintainable, typed, linted, and production-oriented.

---

## 27. Required Deliverables

Produce:

1. Working source code.
2. Repository assessment.
3. Architecture document.
4. Data-flow diagram.
5. Configuration files for development, paper, shadow, and live modes.
6. Database schemas and migrations.
7. Unit, integration, replay, and invariant tests.
8. Event-driven backtester.
9. Walk-forward evaluation report.
10. Performance and risk report.
11. Model cards for any ML models.
12. API/interface documentation.
13. Operational runbook.
14. Incident and recovery procedures.
15. Paper-trading and shadow-mode instructions.
16. Security checklist.
17. Example TrendSnapshot payloads.
18. Example logs and monitoring dashboards.
19. A list of known limitations and future improvements.
20. A final change summary listing every file created or modified.

---

## 28. Coding Standards

- Use strict typing.
- Prefer small, composable modules.
- Use dependency injection for exchange, clock, storage, and model dependencies.
- Use UTC-aware datetimes.
- Use `Decimal` for monetary and quantity calculations where precision matters.
- Use structured JSON logs.
- Add docstrings to public classes and functions.
- Add validation to all external inputs.
- Avoid global mutable state.
- Make asynchronous tasks cancellable.
- Implement graceful shutdown.
- Pin dependencies.
- Include linting, formatting, and type-check commands.
- Use deterministic seeds for research.
- Ensure production and backtest paths share the same core calculation code.

---

## 29. Output Instructions for the Coding Agent

Begin by presenting:

1. Your assessment of the existing codebase.
2. The proposed architecture.
3. Assumptions that must be verified.
4. A file-by-file implementation plan.
5. The first safe implementation phase.

Then implement the project incrementally. After each phase:

- Summarize changes.
- List files created or modified.
- Run tests and show results.
- Report unresolved issues honestly.
- Do not claim live readiness without evidence.

Do not return only pseudocode. Produce complete, runnable, tested code with clear setup instructions.

---

## 30. Initial Production Baseline

Use this as the first transparent baseline, subject to repository compatibility and walk-forward validation:

```text
Primary instrument       BTCUSD perpetual or configured BTC reference
Regime timeframe         30 minutes
Structural timeframe     1 hour
Setup timeframe          15 minutes
Trigger timeframe        5 minutes
Order-flow horizons      Tick, 10s, 30s, 2m, and 5m
Trend model              Transparent weighted score
Direction model          Optional calibrated logistic or boosted model
Magnitude model          Realized-volatility and jump forecast
Entry threshold          |score| >= 65
Hold threshold           |score| > 25 for current direction
Minimum confidence       0.62
Sizing                   Volatility and stop-distance based
Execution                Marketable IOC limit with slippage cap
Unsafe-state behavior    Fail closed
Retraining               Offline and walk-forward validated
Live adaptation          Risk scaling only; no uncontrolled online learning
```

The engine’s primary objective is **not to trade frequently**. Its objective is to identify the minority of market conditions where expected edge is sufficiently strong, explainable, liquid, and robust after costs—and to remain flat otherwise.

---

## 31. Final Safety Statement

This is a high-risk financial system. Build it as decision-support and controlled execution infrastructure, not as a guarantee of profit. Require paper trading, shadow validation, conservative risk limits, human oversight, and explicit live-mode activation before any real capital is exposed.

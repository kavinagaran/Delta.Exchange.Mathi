"""Walk-forward threshold selection and parameter sensitivity (plan Phase 5).

The transparent score has no *fitted* parameters — it is rule-based — but it
does have chosen thresholds (`entry_score`, `hold_score`,
`minimum_confidence`). Choosing those on the same data used to report results
is overfitting by a less obvious name, so selection happens strictly inside
each training window and is then measured on the untouched window that
follows.

The report separates training from out-of-sample deliberately and always
prints both. A strategy that only looks good in-sample has told you something
important, and hiding that behind a single blended number is the failure this
module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Sequence

from ..market_data.messages import Candle
from ..signals.snapshot import SignalConfig
from .event_replay import ReplayConfig, replay
from .performance import Performance, evaluate

# Bracketed around the 2026-07-29 operator spec's |40| directional entry, so
# the sweep answers "is 40 a cliff or a plateau?" rather than re-testing the
# superseded 65. Was (55, 65, 75).
DEFAULT_ENTRY_SCORES = (35.0, 40.0, 50.0)
DEFAULT_MIN_CONFIDENCE = (0.55, 0.62, 0.70)


@dataclass(frozen=True, slots=True)
class Candidate:
    entry_score: float
    minimum_confidence: float

    def applied_to(self, base: SignalConfig) -> SignalConfig:
        return replace(base, entry_score=self.entry_score,
                       minimum_confidence=self.minimum_confidence)

    def label(self) -> str:
        return f"entry={self.entry_score:g},conf={self.minimum_confidence:g}"


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    selected: Candidate
    train: Performance
    test: Performance


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    folds: list[Fold] = field(default_factory=list)
    sensitivity: dict[str, Performance] = field(default_factory=dict)
    pooled_oos: Performance | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def positive_oos_folds(self) -> int:
        return sum(1 for f in self.folds if f.test.total_pnl > 0)


def default_candidates() -> list[Candidate]:
    return [Candidate(entry, confidence)
            for entry in DEFAULT_ENTRY_SCORES
            for confidence in DEFAULT_MIN_CONFIDENCE]


def _objective(performance: Performance) -> float:
    """Selection objective. Total P&L alone would happily pick a config with
    two lucky trades, so a config with too few trades is disqualified."""
    if performance.trades < 3:
        return float("-inf")
    return performance.total_pnl


def run_walk_forward(
    candles: Sequence[Candle],
    *,
    base_config: ReplayConfig | None = None,
    candidates: Sequence[Candidate] | None = None,
    folds: int = 4,
    train_fraction: float = 0.7,
    warmup: int = 320,
    sensitivity_candles: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> WalkForwardReport:
    base_config = base_config or ReplayConfig()
    candidates = list(candidates or default_candidates())
    ordered = sorted(candles, key=lambda c: c.start)
    warnings: list[str] = []

    # Each fold's TEST window alone must exceed the warmup, and the test
    # window is (1 - train_fraction) of the fold. So one fold needs about
    # warmup/(1-train_fraction) candles, and the whole run needs that times
    # the fold count.
    per_fold_minimum = int((warmup + 50) / (1 - train_fraction)) + 1
    if len(ordered) < per_fold_minimum * folds:
        warnings.append(
            f"only {len(ordered):,} candles supplied; {folds} folds at "
            f"train_fraction={train_fraction} need roughly "
            f"{per_fold_minimum * folds:,} for each test window to clear the "
            f"{warmup:,}-candle warmup")

    total = len(ordered)
    fold_size = total // folds
    results: list[Fold] = []

    for fold_index in range(folds):
        window = ordered[fold_index * fold_size: (fold_index + 1) * fold_size]
        if len(window) < warmup + 50:
            warnings.append(f"fold {fold_index}: too few candles ({len(window)}); skipped")
            continue
        split = int(len(window) * train_fraction)
        train_window, test_window = window[:split], window[split:]
        if len(test_window) < warmup + 10:
            # The test window needs its own warmup: the producer cannot emit
            # a usable snapshot without history, and borrowing the training
            # tail would leak it into out-of-sample.
            warnings.append(
                f"fold {fold_index}: test window ({len(test_window)}) is shorter "
                f"than the {warmup}-candle warmup; skipped rather than leaking "
                "training data into it")
            continue

        best: tuple[float, Candidate, Performance] | None = None
        for candidate in candidates:
            config = replace(base_config, signal=candidate.applied_to(base_config.signal))
            performance = evaluate(replay(train_window, config, warmup=warmup).trades)
            score = _objective(performance)
            if best is None or score > best[0]:
                best = (score, candidate, performance)
        if best is None or best[0] == float("-inf"):
            warnings.append(
                f"fold {fold_index}: no candidate produced enough training trades "
                "to select on; skipped")
            continue

        _, selected, train_performance = best
        test_config = replace(base_config, signal=selected.applied_to(base_config.signal))
        test_performance = evaluate(replay(test_window, test_config, warmup=warmup).trades)

        results.append(Fold(
            index=fold_index,
            train_start=train_window[0].start.isoformat(),
            train_end=train_window[-1].start.isoformat(),
            test_start=test_window[0].start.isoformat(),
            test_end=test_window[-1].start.isoformat(),
            selected=selected, train=train_performance, test=test_performance))
        if progress:
            progress(f"fold {fold_index}: selected {selected.label()} "
                     f"train={train_performance.total_pnl:+.2f} "
                     f"oos={test_performance.total_pnl:+.2f}")

    # The sensitivity sweep re-runs the whole series once per candidate, so it
    # is the most expensive part of the report by a wide margin. Bounding it
    # to a recent window keeps the report re-runnable; the bound is recorded
    # in the report rather than left implicit.
    sensitivity_window = (ordered if sensitivity_candles is None
                          else ordered[-sensitivity_candles:])
    sensitivity: dict[str, Performance] = {}
    for candidate in candidates:
        config = replace(base_config, signal=candidate.applied_to(base_config.signal))
        sensitivity[candidate.label()] = evaluate(
            replay(sensitivity_window, config, warmup=warmup).trades)
        if progress:
            progress(f"sensitivity {candidate.label()}: "
                     f"{sensitivity[candidate.label()].total_pnl:+.2f}")

    # Pool the OOS trades themselves rather than averaging fold summaries:
    # a mean-of-means would weight a 3-trade fold equally with a 30-trade one.
    pooled_trades = []
    for fold in results:
        window = ordered[fold.index * fold_size: (fold.index + 1) * fold_size]
        split = int(len(window) * train_fraction)
        test_config = replace(base_config,
                              signal=fold.selected.applied_to(base_config.signal))
        pooled_trades.extend(replay(window[split:], test_config, warmup=warmup).trades)
    pooled = evaluate(pooled_trades) if pooled_trades else None

    if sensitivity_candles is not None and len(ordered) > sensitivity_candles:
        warnings.append(
            f"sensitivity sweep covers the most recent {len(sensitivity_window):,} "
            f"candles, not the full {len(ordered):,} (runtime bound)")

    return WalkForwardReport(folds=results, sensitivity=sensitivity,
                             pooled_oos=pooled, warnings=warnings)

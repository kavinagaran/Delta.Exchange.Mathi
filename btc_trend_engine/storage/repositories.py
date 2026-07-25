"""Typed write/read helpers over the Phase 2 tables."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from ..market_data.messages import Candle
from ..market_data.normalizer import decimal_str
from .models import CandleRow, HealthEvent, MarketSnapshot


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repositories:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    # ── health ───────────────────────────────────────────────────────────
    def record_health_event(self, now: datetime, category: str, detail: str) -> None:
        with self._sessions() as session:
            session.add(HealthEvent(
                occurred_at_utc=_utc_iso(now), category=category,
                detail=detail[:2000], created_at_utc=_utc_iso(now)))
            session.commit()

    def recent_health_events(self, limit: int = 50) -> list[HealthEvent]:
        with self._sessions() as session:
            rows = session.execute(
                select(HealthEvent).order_by(HealthEvent.id.desc()).limit(limit)
            ).scalars().all()
            return list(rows)

    # ── candles ──────────────────────────────────────────────────────────
    def upsert_candle(self, candle: Candle, source: str, now: datetime) -> None:
        """Idempotent on (symbol, resolution, start): a re-bootstrap or replay
        must not duplicate rows, and live never overwrites bootstrap silently —
        identical identity means identical closed candle, so first write wins."""
        with self._sessions() as session:
            statement = sqlite_insert(CandleRow).values(
                symbol=candle.symbol, resolution=candle.resolution,
                start_utc=_utc_iso(candle.start),
                open=decimal_str(candle.open), high=decimal_str(candle.high),
                low=decimal_str(candle.low), close=decimal_str(candle.close),
                volume=decimal_str(candle.volume),
                trade_count=candle.trade_count, source=source,
                created_at_utc=_utc_iso(now),
            ).on_conflict_do_nothing(
                index_elements=["symbol", "resolution", "start_utc"])
            session.execute(statement)
            session.commit()

    def candle_count(self, symbol: str, resolution: str) -> int:
        with self._sessions() as session:
            rows = session.execute(
                select(CandleRow.id).where(
                    CandleRow.symbol == symbol,
                    CandleRow.resolution == resolution)
            ).all()
            return len(rows)

    # ── market snapshots ─────────────────────────────────────────────────
    def record_market_snapshot(self, snapshot: MarketSnapshot) -> None:
        with self._sessions() as session:
            session.add(snapshot)
            session.commit()

    def latest_market_snapshot(self, symbol: str) -> MarketSnapshot | None:
        with self._sessions() as session:
            return session.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.symbol == symbol)
                .order_by(MarketSnapshot.id.desc()).limit(1)
            ).scalars().first()

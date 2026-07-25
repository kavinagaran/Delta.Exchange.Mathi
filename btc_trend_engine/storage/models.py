"""Relational schema (§17, ADR 0002).

Monetary values are stored as canonical decimal strings — SQLite would coerce
NUMERIC through float.  Timestamps are UTC ISO-8601 strings with a ``Z``
suffix; every row carries ``created_at_utc``.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = "2026.07.p2"


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(32))


class MarketSnapshot(Base):
    """Periodic top-of-book + ticker state for /status and research."""

    __tablename__ = "market_snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    captured_at_utc: Mapped[str] = mapped_column(String(32))
    best_bid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    best_ask: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mark_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    spot_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    open_interest: Mapped[str | None] = mapped_column(String(32), nullable=True)
    funding_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    book_sequence_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_quality: Mapped[str] = mapped_column(String(32))
    created_at_utc: Mapped[str] = mapped_column(String(32))


class CandleRow(Base):
    __tablename__ = "candle"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    resolution: Mapped[str] = mapped_column(String(8))
    start_utc: Mapped[str] = mapped_column(String(32))
    open: Mapped[str] = mapped_column(String(32))
    high: Mapped[str] = mapped_column(String(32))
    low: Mapped[str] = mapped_column(String(32))
    close: Mapped[str] = mapped_column(String(32))
    volume: Mapped[str] = mapped_column(String(32))
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16))  # "bootstrap" | "live"
    created_at_utc: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_candle_identity", "symbol", "resolution", "start_utc",
              unique=True),
    )


class HealthEvent(Base):
    """Reconnects, gaps, rebuilds, staleness transitions, capture stops."""

    __tablename__ = "health_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at_utc: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str] = mapped_column(Text)
    created_at_utc: Mapped[str] = mapped_column(String(32))


# Placeholders for later phases keep the schema-version story honest: they are
# created now, so Phase 3+ writes are additive rather than restructuring.
class FeatureVectorRow(Base):
    __tablename__ = "feature_vector"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    as_of_utc: Mapped[str] = mapped_column(String(32), index=True)
    horizon: Mapped[str] = mapped_column(String(8))
    features_json: Mapped[str] = mapped_column(Text)
    missing_features_json: Mapped[str] = mapped_column(Text, default="[]")
    data_quality_score: Mapped[str] = mapped_column(String(16), default="0")
    feature_set_version: Mapped[str] = mapped_column(String(16))
    created_at_utc: Mapped[str] = mapped_column(String(32))


class TrendSnapshotRow(Base):
    __tablename__ = "trend_snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(32), unique=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    candle_close_utc: Mapped[str] = mapped_column(String(32), index=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    created_at_utc: Mapped[str] = mapped_column(String(32))


class ShadowComparisonRow(Base):
    """One completed candle, as seen by the legacy engine and by this one.

    Adding this table needs no SCHEMA_VERSION bump: ``init_schema`` runs
    ``create_all`` before the version check, so a purely additive table
    appears on existing data stores automatically. The version guards
    *restructuring* of existing tables, which this is not.

    Keyed on ``candle_close_utc`` so the join is on the candle, never on wall
    clock, and so re-posting the same candle updates rather than duplicates.
    """

    __tablename__ = "shadow_comparison"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candle_close_utc: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    recorded_at_utc: Mapped[str] = mapped_column(String(32))

    # Legacy side (posted by dashboard.py).
    legacy_signal_key: Mapped[str] = mapped_column(String(128))
    legacy_direction: Mapped[int] = mapped_column(Integer)
    legacy_zone: Mapped[str] = mapped_column(String(32))
    legacy_score: Mapped[str] = mapped_column(String(32))
    legacy_regime: Mapped[str] = mapped_column(String(32))
    legacy_dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    # Engine side, resolved from this engine's own snapshot for that candle.
    # Nullable on purpose: "the engine had no snapshot for this candle" is a
    # real and important outcome, distinct from a disagreement, and must not
    # be silently scored as one.
    engine_present: Mapped[bool] = mapped_column(Boolean, default=False)
    engine_signal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    engine_direction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engine_regime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    engine_score: Mapped[str | None] = mapped_column(String(32), nullable=True)
    engine_entry_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    engine_data_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)

    agreed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    disagreement_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

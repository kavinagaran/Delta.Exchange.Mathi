"""Relational schema (§17, ADR 0002).

Monetary values are stored as canonical decimal strings — SQLite would coerce
NUMERIC through float.  Timestamps are UTC ISO-8601 strings with a ``Z``
suffix; every row carries ``created_at_utc``.
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, String, Text
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

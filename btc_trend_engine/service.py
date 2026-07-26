"""Engine service: wires the market-data pipeline together (§4, Phase 2 scope).

    Delta WS ─▶ normalize ─▶ dispatch ─▶ order book (seq-gap → rebuild)
                                     ├─▶ candle aggregator (closed → SQLite)
                                     ├─▶ data-quality monitor
                                     └─▶ append-only event store

Everything downstream of ``dispatch`` is synchronous and owned by one asyncio
task, so no locking is needed (single-writer, ADR 0002).  The service exposes
read-only state for the API layer; it produces no signals in Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone
from typing import Any

import websockets

from . import __version__
from .clock import Clock, SystemClock
from .config import EngineConfig
from .market_data.candle_aggregator import CandleAggregator
from .market_data.data_quality import DataQualityMonitor
from .market_data.delta_public_ws import DeltaPublicWs
from .market_data.delta_rest import DeltaRestClient, RestError
from .market_data.messages import (
    Candle,
    EventType,
    MarketEvent,
    NormalizationError,
)
from .market_data.normalizer import decimal_str, resolution_seconds, to_book_delta, to_trades
from .market_data.orderbook import ApplyResult, OrderBook
from .signals.producer import TRIGGER as TRIGGER_RESOLUTION
from .signals.producer import SnapshotProducer, spread_bps_from
from .storage.db import create_db_engine, init_schema, make_session_factory
from .storage.event_store import EventStore, finalize_stale_plain_files
from .storage.lock import DataDirectoryLock
from .storage.models import MarketSnapshot
from .storage.repositories import Repositories

log = logging.getLogger(__name__)

_EVENT_FEED = {
    EventType.TRADE: "trades",
    EventType.L2_UPDATE: "l2",
    EventType.TICKER: "ticker",
    EventType.MARK_PRICE: "mark",
    EventType.FUNDING: "funding",
}


class EngineService:
    def __init__(self, config: EngineConfig, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.started_at = self.clock.now()
        symbol = config.engine.symbol

        data_path = config.storage.data_path
        # Claim single-writer ownership BEFORE touching any file: startup
        # finalization would otherwise destroy a running engine's active
        # capture file (ADR 0002).
        self.data_lock = DataDirectoryLock(data_path)
        self.data_lock.acquire()
        self.event_store = EventStore(
            data_path, symbol, clock=self.clock,
            fsync_interval_seconds=config.storage.fsync_interval_seconds)
        finalize_stale_plain_files(data_path, symbol)

        engine = create_db_engine(data_path)
        init_schema(engine)
        self.repos = Repositories(make_session_factory(engine))

        self.book = OrderBook(
            symbol, validate_checksums=config.orderbook.validate_checksums)
        self.candles = CandleAggregator(
            symbol, config.market_data.candle_resolutions,
            on_close=self._on_candle_close)
        self.data_quality = DataQualityMonitor(
            config.data_quality, self.clock,
            feeds=["trades", "l2", "ticker", "mark", "funding"])

        self.rest = DeltaRestClient(
            config.market_data.rest_url, config.market_data.rate_limit,
            clock=self.clock)
        self.ws = DeltaPublicWs(
            connect=self._connect_ws,
            channels=self._channel_spec(),
            on_event=self.handle_event,
            on_disconnect=self._on_disconnect,
            reconnect=config.market_data.reconnect,
            clock=self.clock,
        )

        self.producer = SnapshotProducer(symbol)
        self.last_ticker: dict[str, Any] = {}
        self.live_view: dict[str, Any] | None = None
        self.raw_capture_enabled = True
        self.dispatch_errors = 0
        self.snapshot_errors = 0
        self._tasks: list[asyncio.Task[None]] = []

    # ── wiring ───────────────────────────────────────────────────────────
    def _channel_spec(self) -> list[dict[str, Any]]:
        symbol = self.config.engine.symbol
        spec = []
        for channel in self.config.market_data.channels:
            symbols = [f"MARK:{symbol}"] if channel == "mark_price" else [symbol]
            spec.append({"name": channel, "symbols": symbols})
        return spec

    async def _connect_ws(self) -> Any:
        return await websockets.connect(
            self.config.market_data.websocket_url,
            open_timeout=15, ping_interval=20, max_queue=4096)

    # ── event pipeline (single task) ─────────────────────────────────────
    def handle_event(self, event: MarketEvent) -> None:
        feed = _EVENT_FEED.get(event.event_type)
        if feed is not None:
            self.data_quality.record(
                feed, event.exchange_timestamp.timestamp())
        if self.raw_capture_enabled:
            self.event_store.append(event)
        try:
            self._dispatch(event)
        except NormalizationError as exc:
            self.dispatch_errors += 1
            self._health("normalization", str(exc))

    def _dispatch(self, event: MarketEvent) -> None:
        if event.event_type is EventType.L2_UPDATE:
            result = self.book.apply(to_book_delta(event))
            if result is ApplyResult.REJECTED_SEQUENCE_GAP:
                self._health("book_gap",
                             f"sequence gap at {event.sequence_no}; rebuilding")
                self.ws.request_restart()
            elif result is ApplyResult.REJECTED_CHECKSUM:
                self._health("book_checksum", "checksum mismatch; rebuilding")
                self.ws.request_restart()
        elif event.event_type is EventType.TRADE:
            for trade in to_trades(event):
                self.candles.add_trade(trade)
        elif event.event_type is EventType.TICKER:
            self.last_ticker = event.payload

    def _on_candle_close(self, candle: Candle) -> None:
        self.repos.upsert_candle(candle, source="live", now=self.clock.now())
        # The trigger timeframe closing is the decision moment (§7 rule 6).
        if candle.resolution == "5m":
            self.produce_snapshot()

    def produce_snapshot(self) -> dict[str, Any] | None:
        """Build and persist one TrendSnapshot. Never raises into the feed
        loop: a snapshot failure must not take down market-data capture."""
        top = self.book.top()
        spread = spread_bps_from(
            float(top.bid.price) if top and top.bid else None,
            float(top.ask.price) if top and top.ask else None,
        )
        try:
            snapshot = self.producer.produce(
                now=self.clock.now(),
                candles={resolution: series.closed_candles()
                         for resolution, series in self.candles.series.items()},
                ticker=self.last_ticker,
                data_quality=self.current_data_quality(),
                book_valid=self.book.is_valid,
                spread_bps=spread,
            )
        except Exception as exc:
            self.snapshot_errors += 1
            self._health("snapshot_failed", f"{type(exc).__name__}: {exc}")
            return None
        if snapshot is None:
            return None
        try:
            self.repos.record_trend_snapshot(snapshot, now=self.clock.now())
        except Exception:
            log.exception("trend snapshot write failed")
        return snapshot

    def refresh_live_view(self, now: datetime) -> dict[str, Any] | None:
        """Recompute the provisional display score (every housekeeping tick,
        i.e. ~5s). Never raises into the loop, and never touches the committed
        snapshot — see SnapshotProducer.produce_live."""
        try:
            self.live_view = self.producer.produce_live(
                now=now,
                candles={resolution: series.closed_candles()
                         for resolution, series in self.candles.series.items()},
                forming=self.candles.series[TRIGGER_RESOLUTION].forming(),
                data_quality=self.current_data_quality(),
            )
        except Exception:
            log.exception("live view refresh failed")
        return self.live_view

    def _on_disconnect(self, reason: str) -> None:
        # Block-entries-first (§6.4 step 1): the book is invalid the moment the
        # feed drops, before any reconnect attempt.
        self.book.invalidate(reason)
        self._health("ws_disconnect", reason)

    def _health(self, category: str, detail: str) -> None:
        log.warning("%s: %s", category, detail)
        try:
            self.repos.record_health_event(self.clock.now(), category, detail)
        except Exception:
            log.exception("health event write failed")

    # ── bootstrap ────────────────────────────────────────────────────────
    async def bootstrap_candles(self) -> None:
        symbol = self.config.engine.symbol
        limit = self.config.market_data.candle_bootstrap_limit
        end = int(self.clock.now().timestamp())
        for resolution in self.config.market_data.candle_resolutions:
            start = end - resolution_seconds(resolution) * limit
            try:
                candles = await self.rest.history_candles(
                    symbol, resolution, start, end)
            except RestError as exc:
                self._health("bootstrap_failed", f"{resolution}: {exc}")
                continue
            # The most recent row may still be forming; only closed candles
            # are usable for decisions (§7 rule 6).
            cutoff = self.clock.now().timestamp()
            closed = [c for c in candles
                      if c.start.timestamp() + resolution_seconds(resolution) <= cutoff]
            self.candles.seed_closed(resolution, closed)
            for candle in closed:
                self.repos.upsert_candle(candle, source="bootstrap",
                                         now=self.clock.now())
            log.info("bootstrapped %d %s candles", len(closed), resolution)

    # ── periodic housekeeping ────────────────────────────────────────────
    async def _housekeeping_loop(self) -> None:
        interval = 5.0
        snapshot_every = 60.0
        last_snapshot = 0.0
        while True:
            await asyncio.sleep(interval)
            now = self.clock.now()
            self.candles.roll_clock(now)
            self.event_store.flush()
            self._check_disk()
            self.refresh_live_view(now)
            mono = self.clock.monotonic()
            if mono - last_snapshot >= snapshot_every:
                last_snapshot = mono
                self._record_market_snapshot(now)

    def _check_disk(self) -> None:
        usage = shutil.disk_usage(self.config.storage.data_path)
        free_gb = usage.free / (1 << 30)
        if free_gb < self.config.storage.min_free_gb and self.raw_capture_enabled:
            self.raw_capture_enabled = False
            self._health("capture_stopped",
                         f"free disk {free_gb:.2f} GB below "
                         f"{self.config.storage.min_free_gb} GB; raw capture stopped")
        elif free_gb >= self.config.storage.min_free_gb * 2 and not self.raw_capture_enabled:
            self.raw_capture_enabled = True
            self._health("capture_resumed", f"free disk {free_gb:.2f} GB")

    def _record_market_snapshot(self, now: datetime) -> None:
        top = self.book.top()
        ticker = self.last_ticker
        snapshot = MarketSnapshot(
            symbol=self.config.engine.symbol,
            captured_at_utc=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            best_bid=decimal_str(top.bid.price) if top and top.bid else None,
            best_ask=decimal_str(top.ask.price) if top and top.ask else None,
            mark_price=str(ticker.get("mark_price")) if ticker.get("mark_price") is not None else None,
            spot_price=str(ticker.get("spot_price")) if ticker.get("spot_price") is not None else None,
            open_interest=str(ticker.get("oi")) if ticker.get("oi") is not None else None,
            funding_rate=str(ticker.get("funding_rate")) if ticker.get("funding_rate") is not None else None,
            book_sequence_no=top.sequence_no if top else None,
            data_quality=self.current_data_quality(),
            created_at_utc=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        try:
            self.repos.record_market_snapshot(snapshot)
        except Exception:
            log.exception("market snapshot write failed")

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        await self.bootstrap_candles()
        self._tasks = [
            asyncio.create_task(self.ws.run(), name="ws-consumer"),
            asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
        ]

    async def stop(self) -> None:
        self.ws.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.event_store.close()
        await self.rest.aclose()
        self.data_lock.release()

    # ── status for the API ───────────────────────────────────────────────
    def current_data_quality(self) -> str:
        return self.data_quality.data_quality(book_valid=self.book.is_valid)

    def status(self) -> dict[str, Any]:
        verdicts = self.data_quality.verdicts()
        bid_levels, ask_levels = self.book.level_counts()
        return {
            "version": __version__,
            "symbol": self.config.engine.symbol,
            "started_at": self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_seconds": round(
                (self.clock.now() - self.started_at).total_seconds(), 1),
            "data_quality": self.current_data_quality(),
            "book": {
                "state": self.book.state.value,
                "sequence_no": self.book.sequence_no,
                "bid_levels": bid_levels,
                "ask_levels": ask_levels,
                "rebuilds": self.book.rebuild_count,
                "gaps": self.book.gap_count,
            },
            "feeds": {
                feed: {
                    "freshness": verdict.freshness.value,
                    "age_seconds": round(verdict.age_seconds, 3)
                    if verdict.age_seconds is not None else None,
                }
                for feed, verdict in verdicts.items()
            },
            "clock_drift_ms": self.data_quality.clock_drift.drift_ms(),
            "ws_connects": self.ws.connect_count,
            "normalization_errors": self.ws.normalization_errors + self.dispatch_errors,
            "snapshots_produced": self.producer.produced,
            "snapshot_errors": self.snapshot_errors,
            "events_written": self.event_store.written,
            "raw_capture_enabled": self.raw_capture_enabled,
            "candles": {
                resolution: series.last_closed().start.strftime("%Y-%m-%dT%H:%M:%SZ")
                if series.last_closed() else None
                for resolution, series in self.candles.series.items()
            },
        }

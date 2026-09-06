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
import json
import logging
import math
import os
import shutil
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

_PREVIEW_SCORE_HISTORY_LIMIT = 288  # 24 hours of five-minute candles
_PREVIEW_SCORE_HISTORY_FILE = "preview_score_history.json"


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
        self._restore_signal_state()
        self.last_ticker: dict[str, Any] = {}
        self.live_view: dict[str, Any] | None = None
        # The Preview Decision has a separate display-only history. Completed
        # bars are persisted so a routine service restart does not erase the
        # chart's rolling 24-hour context. This file is never read by the
        # producer or order consumer and contains no signal/permission fields.
        self._preview_score_history_path = (
            config.storage.data_path / _PREVIEW_SCORE_HISTORY_FILE
        )
        self._preview_score_candles: deque[dict[str, Any]] = deque(
            maxlen=_PREVIEW_SCORE_HISTORY_LIMIT,
        )
        self._preview_score_forming: dict[str, Any] | None = None
        self._restore_preview_score_history()
        self.raw_capture_enabled = True
        self.dispatch_errors = 0
        self.snapshot_errors = 0
        self._tasks: list[asyncio.Task[None]] = []

    def _restore_signal_state(self) -> None:
        """Continue committed signal state across a controlled restart.

        Snapshot rows are append-only and already validate the engine's
        history.  Bad historical JSON is ignored rather than preventing market
        capture from starting; the producer then warms up fail-closed.
        """
        try:
            rows = self.repos.recent_trend_snapshots(
                self.config.engine.symbol, limit=500,
            )
            snapshots = []
            for row in reversed(rows):
                try:
                    parsed = json.loads(row.snapshot_json)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    snapshots.append(parsed)
            restored = self.producer.restore_history(snapshots)
            if restored:
                log.info("restored %d committed trend snapshots", restored)
        except Exception:
            log.exception("trend signal state restore failed; starting fail-closed")

    def _restore_preview_score_history(self) -> None:
        """Restore validated completed display bars from the rolling file.

        Corrupt display history must never prevent the market-data engine from
        starting. Each row is normalised defensively and decision-shaped keys
        are deliberately discarded.
        """
        try:
            payload = json.loads(
                self._preview_score_history_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError):
            log.warning("preview score history restore skipped: invalid file")
            return

        rows = payload.get("candles") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            log.warning("preview score history restore skipped: invalid rows")
            return

        restored: dict[str, dict[str, Any]] = {}
        for row in rows[-_PREVIEW_SCORE_HISTORY_LIMIT:]:
            if not isinstance(row, dict):
                continue
            start = row.get("start_utc")
            if not isinstance(start, str) or not start:
                continue
            try:
                open_score = float(row["open"])
                high_score = float(row["high"])
                low_score = float(row["low"])
                close_score = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in (
                open_score, high_score, low_score, close_score,
            )):
                continue
            try:
                samples = max(1, int(row.get("samples") or 1))
            except (TypeError, ValueError):
                samples = 1
            restored[start] = {
                "start_utc": start,
                "end_utc": (
                    row.get("end_utc")
                    if isinstance(row.get("end_utc"), str)
                    else self._preview_candle_end(start)
                ),
                "open": open_score,
                "high": max(open_score, high_score, low_score, close_score),
                "low": min(open_score, high_score, low_score, close_score),
                "close": close_score,
                "samples": samples,
                "partial": bool(row.get("partial")),
                "last_sample_utc": str(row.get("last_sample_utc") or ""),
                "data_quality": row.get("data_quality"),
                "forming": False,
            }
        for row in sorted(restored.values(), key=lambda item: item["start_utc"]):
            self._preview_score_candles.append(row)
        if restored:
            log.info(
                "restored %d display-only preview score candles",
                len(self._preview_score_candles),
            )

    def _persist_preview_score_history(self) -> None:
        """Atomically persist completed display-only bars.

        Failure is logged and ignored: chart retention cannot interrupt market
        capture or change a trading decision.
        """
        path = Path(self._preview_score_history_path)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = {
            "schema_version": 1,
            "symbol": self.config.engine.symbol,
            "resolution": "5m",
            "display_only": True,
            "candles": list(self._preview_score_candles),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError:
            log.exception("preview score history write failed")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

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
            view = self.producer.produce_live(
                now=now,
                candles={resolution: series.closed_candles()
                         for resolution, series in self.candles.series.items()},
                forming=self.candles.series[TRIGGER_RESOLUTION].forming(),
                ticker=self.last_ticker,
                data_quality=self.current_data_quality(),
            )
            if view is not None:
                self._record_preview_score(view)
            self.live_view = view
        except Exception:
            log.exception("live view refresh failed")
        return self.live_view

    def _record_preview_score(self, view: dict[str, Any]) -> None:
        """Fold one provisional sample into a 5-minute score OHLC candle.

        This deliberately consumes only the display-only ``produce_live``
        result. The records have no signal ID or entry permission and are
        returned only by the display-history API.
        """
        start = view.get("forming_candle_start")
        if not isinstance(start, str) or not start:
            return
        try:
            # Chart precision is deliberately separate from the rounded dial
            # score.  Retain a compatibility fallback for test fixtures and
            # older producer responses during a rolling deployment.
            raw_score = view.get("chart_score")
            if raw_score is None:
                raw_score = view["live_score"]
            score = float(raw_score)
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(score):
            return

        as_of = view.get("as_of")
        if not isinstance(as_of, str) or not as_of:
            as_of = self.clock.now().astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        current = self._preview_score_forming
        if current is None or current["start_utc"] != start:
            if current is not None:
                completed = dict(current)
                completed["forming"] = False
                self._preview_score_candles.append(completed)
                self._persist_preview_score_history()
            self._preview_score_forming = {
                "start_utc": start,
                "end_utc": self._preview_candle_end(start),
                "open": score,
                "high": score,
                "low": score,
                "close": score,
                "samples": 1,
                # A service restart can begin recording part way through a
                # market candle.  Keep that fact with the display bar so the
                # renderer does not present one late sample as a full OHLC.
                "partial": self._preview_score_started_late(start, as_of),
                "last_sample_utc": as_of,
                "data_quality": view.get("data_quality"),
                "forming": True,
            }
            return

        current["high"] = max(float(current["high"]), score)
        current["low"] = min(float(current["low"]), score)
        current["close"] = score
        current["samples"] = int(current.get("samples") or 0) + 1
        current["last_sample_utc"] = as_of
        current["data_quality"] = view.get("data_quality")

    @staticmethod
    def _preview_score_started_late(start: str, as_of: str) -> bool:
        """Whether preview capture began after the first 15s of its bar.

        This flag is visual metadata only.  It neither affects the score nor
        reaches a committed snapshot or an order consumer.
        """
        try:
            bar_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            sample_at = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            return True
        return sample_at.astimezone(timezone.utc) > (
            bar_start.astimezone(timezone.utc) + timedelta(seconds=15))

    @staticmethod
    def _preview_candle_end(start: str) -> str:
        try:
            parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
            return (parsed.astimezone(timezone.utc) + timedelta(minutes=5)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            # A malformed display timestamp should not affect market capture.
            # The UI still receives the originating timestamp and simply omits
            # an end label rather than fabricating a time.
            return ""

    def live_score_history(
        self,
        limit: int = _PREVIEW_SCORE_HISTORY_LIMIT,
    ) -> dict[str, Any]:
        """Return recent display-only Preview Decision score candles.

        Completed display bars survive restarts for visual continuity. They
        remain non-decision data and are never presented as trade evidence.
        """
        safe_limit = max(1, min(int(limit), _PREVIEW_SCORE_HISTORY_LIMIT))
        candles = [dict(candle) for candle in self._preview_score_candles]
        if self._preview_score_forming is not None:
            candles.append(dict(self._preview_score_forming))
        return {
            "symbol": self.config.engine.symbol,
            "resolution": "5m",
            "display_only": True,
            "candles": candles[-safe_limit:],
        }

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

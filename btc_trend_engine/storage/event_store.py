"""Append-only raw event store (§17, ADR 0002).

Layout: ``data/events/<symbol>/<YYYY-MM-DD>/<HH>.ndjson`` for the active hour,
gzipped to ``.ndjson.gz`` on rotation.  Plain NDJSON for the live file is
deliberate: an append of one full line is crash-safe under a hard kill, whereas
an interrupted gzip member poisons everything after it.  Replay tolerates a
torn final line (the only line that can be torn) and drops it.

Single writer — the engine process — per ADR 0002.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..clock import Clock, SystemClock
from ..market_data.messages import MarketEvent


class EventStore:
    def __init__(self, root: Path, symbol: str,
                 clock: Clock | None = None,
                 fsync_interval_seconds: float = 5.0) -> None:
        self._root = root / "events" / symbol
        self._symbol = symbol
        self._clock = clock or SystemClock()
        self._fsync_interval = fsync_interval_seconds
        self._handle: Any = None
        self._current_hour: tuple[str, str] | None = None  # (date, hour)
        self._last_fsync = self._clock.monotonic()
        self.written = 0

    # ── writing ──────────────────────────────────────────────────────────
    def append(self, event: MarketEvent) -> None:
        stamp = event.receive_timestamp.astimezone(timezone.utc)
        hour_key = (stamp.strftime("%Y-%m-%d"), stamp.strftime("%H"))
        if hour_key != self._current_hour:
            self._rotate(hour_key)
        line = json.dumps(event.to_record(), separators=(",", ":"),
                          default=str) + "\n"
        self._handle.write(line.encode())
        self.written += 1
        now = self._clock.monotonic()
        if now - self._last_fsync >= self._fsync_interval:
            self.flush()

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._last_fsync = self._clock.monotonic()

    def close(self) -> None:
        if self._handle is not None:
            self.flush()
            self._handle.close()
            self._handle = None

    def _rotate(self, hour_key: tuple[str, str]) -> None:
        previous = self._current_path()
        self.close()
        if previous is not None and previous.exists():
            _gzip_finalize(previous)
        date_str, hour_str = hour_key
        directory = self._root / date_str
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{hour_str}.ndjson"
        # Append mode: after a crash mid-hour the same file continues.
        self._handle = open(path, "ab")
        self._current_hour = hour_key

    def _current_path(self) -> Path | None:
        if self._current_hour is None:
            return None
        date_str, hour_str = self._current_hour
        return self._root / date_str / f"{hour_str}.ndjson"

    # ── reading ──────────────────────────────────────────────────────────
    def replay(self, date_str: str) -> Iterator[dict[str, Any]]:
        """All records for one UTC date, oldest-first, deterministic order.

        A torn final line in a plain ``.ndjson`` file (hard-kill artefact) is
        dropped; a torn line anywhere else raises, because that is corruption
        rather than an interrupted append.
        """
        directory = self._root / date_str
        if not directory.exists():
            return
        for path in sorted(directory.iterdir()):
            if path.suffix == ".gz":
                with gzip.open(path, "rb") as handle:
                    raw_lines = handle.read().split(b"\n")
                plain = False
            elif path.suffix == ".ndjson":
                raw_lines = path.read_bytes().split(b"\n")
                plain = True
            else:
                continue
            if raw_lines and raw_lines[-1] == b"":
                raw_lines.pop()
            for index, raw in enumerate(raw_lines):
                try:
                    yield json.loads(raw)
                except ValueError as exc:
                    if plain and index == len(raw_lines) - 1:
                        return  # torn final append from a hard kill
                    raise CorruptEventFile(f"{path} line {index + 1}") from exc


class CorruptEventFile(RuntimeError):
    pass


def _gzip_finalize(path: Path) -> None:
    # Append mode: concatenated gzip members are valid and read back
    # transparently. This matters after a crash — startup finalizes the
    # interrupted hour, the restarted process recreates the same hour file,
    # and the next rotation must extend the archive, not overwrite it.
    gz_path = path.with_suffix(".ndjson.gz")
    with open(path, "rb") as src, gzip.open(gz_path, "ab", compresslevel=6) as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    path.unlink()


def finalize_stale_plain_files(root: Path, symbol: str,
                               active_path: Path | None = None) -> int:
    """Gzip any plain .ndjson left behind by a crash (except the active one)."""
    base = root / "events" / symbol
    count = 0
    if not base.exists():
        return 0
    for path in sorted(base.rglob("*.ndjson")):
        if active_path is not None and path == active_path:
            continue
        _gzip_finalize(path)
        count += 1
    return count

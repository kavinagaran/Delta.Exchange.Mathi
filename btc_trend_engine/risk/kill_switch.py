"""Latching kill switches (Trend_Engine.md §14.3/§24 "kill switch tested
before any live stage").

Unlike ``risk_controls.evaluate_entry``'s daily-loss/consecutive-loss blocks —
which are automatically re-evaluated per trading day and clear on rollover — a
fired kill switch stays fired across restarts and trading days until an
operator explicitly resumes it. State is a small JSON file under the engine's
own storage directory; this module never reads or writes ``users/``.
"""

from __future__ import annotations

import enum
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class KillSwitchName(enum.StrEnum):
    MAX_DAILY_LOSS = "max_daily_loss"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    DATA_QUALITY = "data_quality"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    name: str
    fired_at_utc: str
    last_seen_utc: str
    reason: str
    fired_count: int = 1


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class KillSwitchStore:
    """Atomic, file-backed latch registry. One store per engine data dir."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"switches": {}, "history": []}
        if not isinstance(raw, dict):
            return {"switches": {}, "history": []}
        raw.setdefault("switches", {})
        raw.setdefault("history", [])
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".kill_switch_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def status(self) -> dict[str, KillSwitchState]:
        data = self._read()
        return {
            name: KillSwitchState(
                name=name,
                fired_at_utc=record.get("fired_at_utc", ""),
                last_seen_utc=record.get("last_seen_utc", ""),
                reason=record.get("reason", ""),
                fired_count=int(record.get("fired_count", 1)),
            )
            for name, record in data["switches"].items()
        }

    def active_names(self) -> list[str]:
        return sorted(self._read()["switches"].keys())

    def any_active(self) -> bool:
        return bool(self._read()["switches"])

    def fire(self, name: str, reason: str, now: datetime | None = None) -> KillSwitchState:
        """Idempotent: refiring an already-latched switch bumps the count and
        the latest reason/timestamp but never resets ``fired_at_utc`` — the
        operator resuming it needs to know how long it has really been down."""
        now = now or datetime.now(timezone.utc)
        stamp = _iso(now)
        data = self._read()
        existing = data["switches"].get(name)
        if existing:
            existing["last_seen_utc"] = stamp
            existing["reason"] = reason
            existing["fired_count"] = int(existing.get("fired_count", 1)) + 1
            record = existing
        else:
            record = {
                "fired_at_utc": stamp, "last_seen_utc": stamp,
                "reason": reason, "fired_count": 1,
            }
            data["switches"][name] = record
        data["history"].append({"event": "fire", "name": name, "reason": reason, "at_utc": stamp})
        self._write(data)
        return KillSwitchState(
            name=name, fired_at_utc=record["fired_at_utc"],
            last_seen_utc=record["last_seen_utc"], reason=record["reason"],
            fired_count=record["fired_count"])

    def resume(self, name: str, now: datetime | None = None,
               resumed_by: str = "operator") -> bool:
        """Clear one named switch. Returns False if it was not active — the
        caller can distinguish "already clear" from "just cleared"."""
        now = now or datetime.now(timezone.utc)
        data = self._read()
        if name not in data["switches"]:
            return False
        del data["switches"][name]
        data["history"].append({
            "event": "resume", "name": name, "resumed_by": resumed_by,
            "at_utc": _iso(now),
        })
        self._write(data)
        return True

    def resume_all(self, now: datetime | None = None,
                   resumed_by: str = "operator") -> list[str]:
        now = now or datetime.now(timezone.utc)
        data = self._read()
        names = sorted(data["switches"].keys())
        for name in names:
            del data["switches"][name]
            data["history"].append({
                "event": "resume", "name": name, "resumed_by": resumed_by,
                "at_utc": _iso(now),
            })
        if names:
            self._write(data)
        return names

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._read()["history"][-max(limit, 0):]

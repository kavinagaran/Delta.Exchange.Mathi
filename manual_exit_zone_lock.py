"""Arm score-zone locks when a Cockpit position exits with the trend decision.

The policy is shared by the dashboard close path and the live TP/SL/TSL
monitor. It deliberately has no exchange mutations: a failure here must
never prevent or undo a position close.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import trend_engine_client
from risk_controls import account_entry_lock, account_file_lock, audit_event
from trend_score_auto import CE_2_ITM, PE_2_ITM, SHORT_MOVE, score_zone

LEDGER_FILE = "trend_score_auto_ledger.json"
MANUAL_OWNERSHIPS = frozenset({
    "manual_cockpit_live",
    "manual_cockpit_dry_run",
})
ACTION_ZONES = {
    "buy_ce": CE_2_ITM,
    "sell_pe": CE_2_ITM,
    "buy_pe": PE_2_ITM,
    "sell_ce": PE_2_ITM,
    "sell_move": SHORT_MOVE,
    # Buy MOVE is long volatility. Score automation has only SHORT_MOVE.
    "buy_move": None,
}
_MAX_EVALUATIONS = 256


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(
                str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _is_manual_cockpit_position(state: dict[str, Any]) -> bool:
    ownership = str(state.get("ownership") or "").strip().lower()
    trigger = str(state.get("entry_trigger") or "").strip().lower()
    classification = str(
        state.get("entry_classification") or ""
    ).strip().lower()
    return (
        ownership in MANUAL_OWNERSHIPS
        or trigger.startswith("manual_cockpit_")
        or classification == "manual_cockpit"
    )


def _manual_action(state: dict[str, Any]) -> str:
    # Legacy Cockpit records may predate manual_cockpit_action. Their option
    # type and filled side are also the authoritative market direction if
    # descriptive entry metadata ever disagrees with the actual position.
    symbol = str(state.get("symbol") or "").strip().upper()
    side = str(state.get("side") or "").strip().lower()
    if symbol.startswith("C-") and side in {"long", "short"}:
        return "sell_ce" if side == "short" else "buy_ce"
    if symbol.startswith("P-") and side in {"long", "short"}:
        return "sell_pe" if side == "short" else "buy_pe"
    if symbol.startswith("MV-") and side in {"long", "short"}:
        return "sell_move" if side == "short" else "buy_move"
    action = str(state.get("manual_cockpit_action") or "").strip().lower()
    if action in ACTION_ZONES:
        return action
    trigger = str(state.get("entry_trigger") or "").strip().lower()
    prefix = "manual_cockpit_"
    if trigger.startswith(prefix):
        candidate = trigger[len(prefix):]
        if candidate in ACTION_ZONES:
            return candidate
    return ""


def position_automatic_zone(state: dict[str, Any]) -> str | None:
    """Return the automatic zone matching a manual position's direction."""
    if not isinstance(state, dict) or not _is_manual_cockpit_position(state):
        return None
    return ACTION_ZONES.get(_manual_action(state))


def _committed_zone(snapshot: dict[str, Any]) -> tuple[str | None, str]:
    if not isinstance(snapshot, dict):
        return None, "committed snapshot is not an object"
    quality = str(snapshot.get("data_quality") or "").strip().upper()
    if quality != "OK":
        return None, f"committed snapshot data quality is {quality or 'missing'}"
    try:
        score = float(snapshot.get("trend_score"))
    except (TypeError, ValueError, OverflowError):
        return None, "committed score is not numeric"
    if not math.isfinite(score):
        return None, "committed score is not finite"
    expected = score_zone(score)
    supplied = str(snapshot.get("zone") or "").strip().upper()
    if supplied != expected:
        return None, (
            "committed score and zone disagree "
            f"({score:+.1f} maps to {expected}, received {supplied or 'missing'})"
        )
    if not str(snapshot.get("signal_id") or "").strip():
        return None, "committed snapshot has no signal id"
    return supplied, ""


def _position_identity(state: dict[str, Any]) -> str:
    for key in (
        "position_cycle_id", "simulation_id", "entry_client_order_id",
        "client_order_id", "entry_order_id", "order_id",
    ):
        value = str(state.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    stable = "|".join(str(state.get(key) or "") for key in (
        "slot", "symbol", "entry_date", "entry_time_utc", "entry_mark", "lots",
    ))
    return "legacy:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 1,
            "signals": {},
            "notifications": {},
            "current_transition": None,
            "no_fill_setup": None,
            "setup_lock": None,
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("Trend score-auto ledger is invalid")
    if not isinstance(value.get("signals", {}), dict):
        raise RuntimeError("Trend score-auto signal ledger is invalid")
    if not isinstance(value.get("notifications", {}), dict):
        raise RuntimeError("Trend score-auto notification ledger is invalid")
    evaluations = value.get("manual_exit_lock_evaluations", {})
    if not isinstance(evaluations, dict):
        raise RuntimeError("manual-exit lock evaluation ledger is invalid")
    return value


def _trim_evaluations(evaluations: dict[str, Any]) -> dict[str, Any]:
    if len(evaluations) <= _MAX_EVALUATIONS:
        return evaluations
    ordered = sorted(
        evaluations.items(),
        key=lambda item: str((item[1] or {}).get("evaluated_at_utc") or ""),
    )
    return dict(ordered[-_MAX_EVALUATIONS:])


def apply_manual_exit_zone_lock(
    data_dir: Path,
    state: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
    snapshot_getter: Callable[[str], dict[str, Any]] | None = None,
    account_lock_held: bool = False,
) -> dict[str, Any]:
    """Evaluate and, when directions match, durably arm one setup lock.

    The returned result is diagnostic. All failures are contained so the
    caller can complete the exit regardless of engine or filesystem health.
    """
    result: dict[str, Any] = {
        "eligible": False,
        "matched": False,
        "locked": False,
        "reason": "not a closed Cockpit position",
    }
    if (
        not isinstance(state, dict)
        or str(state.get("status") or "").strip().upper() != "CLOSED"
        or not _is_manual_cockpit_position(state)
    ):
        return result

    result["eligible"] = True
    action = _manual_action(state)
    position_zone = ACTION_ZONES.get(action)
    result.update({"manual_action": action, "position_zone": position_zone})
    if not action:
        result["reason"] = "manual option action could not be identified"

    getter = snapshot_getter or trend_engine_client.get_snapshot
    try:
        committed = snapshot if snapshot is not None else getter("BTCUSD")
        committed_zone, snapshot_error = _committed_zone(committed)
    except Exception as exc:
        committed = {}
        committed_zone, snapshot_error = None, f"snapshot lookup failed: {exc}"
    result["committed_zone"] = committed_zone
    result["signal_id"] = str((committed or {}).get("signal_id") or "")
    if snapshot_error:
        result["reason"] = snapshot_error
    elif not action:
        pass
    elif position_zone is None:
        result["reason"] = "long MOVE has no same-direction automatic zone"
    elif committed_zone != position_zone:
        result["reason"] = "committed decision is not in the exited trade direction"
    else:
        result["matched"] = True
        result["reason"] = "same-direction committed decision"

    data_dir = Path(data_dir)
    identity = _position_identity(state)
    owner = f"manual-exit-zone-lock:{os.getpid()}:{time.time_ns()}"

    def persist() -> None:
        with account_file_lock(
            data_dir, "score-setup-lock", owner,
            stale_after_sec=30, wait_sec=2,
        ) as acquired:
            if not acquired:
                raise RuntimeError("score setup-lock ledger is busy")
            ledger_path = data_dir / LEDGER_FILE
            ledger = _read_ledger(ledger_path)
            evaluations = ledger.setdefault("manual_exit_lock_evaluations", {})
            if identity in evaluations:
                prior = evaluations[identity]
                result.update({
                    "already_evaluated": True,
                    "matched": bool(prior.get("matched")),
                    # Do not re-arm a lock the operator subsequently reset.
                    "locked": False,
                    "previously_locked": bool(prior.get("locked")),
                    "reason": "this position exit was already evaluated",
                    "committed_zone": prior.get("committed_zone"),
                })
                return
            evaluated_at = _utc_now()
            if result["matched"]:
                ledger["setup_lock"] = {
                    "recorded_at_utc": evaluated_at,
                    "target_zone": committed_zone,
                    "mode_revision": "",
                    "source_signal_key": str(
                        (committed or {}).get("signal_id") or ""
                    ),
                    "source_transition_id": identity,
                    "source_action": "MANUAL_EXIT_MATCH",
                    "source_position_action": action,
                    "source_position_symbol": state.get("symbol"),
                    "source_exit_trigger": state.get("exit_trigger"),
                }
                result["locked"] = True
            evaluations[identity] = {
                "evaluated_at_utc": evaluated_at,
                "manual_action": action,
                "position_zone": position_zone,
                "committed_zone": committed_zone,
                "signal_id": result["signal_id"],
                "matched": result["matched"],
                "locked": result["locked"],
                "reason": result["reason"],
            }
            ledger["manual_exit_lock_evaluations"] = _trim_evaluations(
                evaluations
            )
            ledger["schema_version"] = 1
            ledger["updated_at_utc"] = evaluated_at
            _atomic_write_json(ledger_path, ledger)

    try:
        if account_lock_held:
            persist()
        else:
            acquired = False
            account_dir = data_dir.parent if data_dir.name == "dry_run" else data_dir
            for _ in range(40):
                with account_entry_lock(account_dir, owner) as entry_acquired:
                    if entry_acquired:
                        acquired = True
                        persist()
                        break
                time.sleep(0.05)
            if not acquired:
                raise RuntimeError("account entry lock is busy")

        audit_dir = data_dir.parent if data_dir.name == "dry_run" else data_dir
        with suppress(Exception):
            audit_event(audit_dir, "manual_cockpit_exit_zone_lock_evaluated", {
                "position_identity": identity,
                "execution_mode": "dry_run" if data_dir.name == "dry_run" else "live",
                "symbol": state.get("symbol"),
                "manual_action": action,
                "position_zone": position_zone,
                "committed_zone": result.get("committed_zone"),
                "signal_id": result.get("signal_id"),
                "matched": result.get("matched"),
                "lock_armed": result.get("locked"),
                "reason": result.get("reason"),
                "order_submitted": False,
                "exchange_api_called": False,
            })
    except Exception as exc:
        result["error"] = str(exc)[:300]
        result["locked"] = False
    return result

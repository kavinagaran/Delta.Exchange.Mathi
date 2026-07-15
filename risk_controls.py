"""Shared, fail-closed portfolio risk controls for every strategy entry.

The scheduled MOVE process and the dashboard Trend worker are separate OS
processes.  This module deliberately owns the small amount of cross-process
coordination they need: an atomic entry lock, a common trading-day view, a
deduplicated history reader, risk-based sizing helpers and an append-only audit
trail.  It has no Flask or exchange dependencies, so the rules are easy to
unit-test and cannot silently place an order.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SLOTS = ("morning", "evening", "trend")
STATE_NAMES = {
    "morning": "morning_state.json",
    "evening": "straddle_state.json",
    "trend": "trend_state.json",
}


class RiskDataError(RuntimeError):
    """Risk-critical persisted data exists but cannot be trusted."""


def cfg_float(config: dict[str, Any], key: str, default: float) -> float:
    try:
        value = config.get(key, default)
        return float(default if value in (None, "") else value)
    except (TypeError, ValueError):
        return float(default)


def cfg_int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        value = config.get(key, default)
        return int(float(default if value in (None, "") else value))
    except (TypeError, ValueError):
        return int(default)


def cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = config.get(key)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _trade_key(trade: dict[str, Any]) -> tuple[Any, ...]:
    """Prefer exchange identity, then a stable composite for legacy rows."""
    for key in ("client_order_id", "order_id", "entry_order_id"):
        if trade.get(key) not in (None, "", 0, "0"):
            return (key, str(trade[key]))
    return (
        "legacy",
        str(trade.get("slot") or ""),
        str(trade.get("symbol") or ""),
        str(trade.get("entry_date") or trade.get("date") or ""),
        str(trade.get("entry_time_utc") or trade.get("entry_time") or ""),
        str(trade.get("lots") or ""),
        str(trade.get("entry_mark") or ""),
    )


def dedupe_trades(trades: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        key = _trade_key(trade)
        if key in seen:
            continue
        seen.add(key)
        out.append(trade)
    return out


def load_history(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "trade_history.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RiskDataError(f"trade history is unreadable: {path}") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RiskDataError(f"trade history has an invalid schema: {path}")
    return dedupe_trades(rows)


def load_states(data_dir: Path) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for slot, filename in STATE_NAMES.items():
        path = data_dir / filename
        if not path.exists():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise RiskDataError(f"{slot} state is unreadable: {path}") from exc
        if not isinstance(state, dict):
            raise RiskDataError(f"{slot} state has an invalid schema: {path}")
        states[slot] = state
    return states


def trading_date(now: datetime, offset_minutes: int = 330) -> str:
    """Account trading day; defaults to IST rather than a surprising UTC cut."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) + timedelta(minutes=offset_minutes)).date().isoformat()


def _row_date(row: dict[str, Any], offset_minutes: int) -> str:
    # New records persist trading_date.  Legacy records only have their UTC
    # entry date, which is still a safer approximation than dropping them.
    return str(row.get("trading_date") or row.get("entry_date") or row.get("date") or "")


def _pnl(row: dict[str, Any]) -> float | None:
    try:
        gross = float(row.get("pnl_usd"))
    except (TypeError, ValueError):
        return None
    try:
        fees = float(row.get("fees_usd") or row.get("commission_usd") or 0)
    except (TypeError, ValueError):
        fees = 0.0
    return gross - fees if not row.get("pnl_includes_fees") else gross


def position_risk_usd(state: dict[str, Any], default_short_risk: float) -> float:
    """Conservative open risk: stop risk, else paid premium, else short cap."""
    if str(state.get("status", "")).upper() != "OPEN":
        return 0.0
    protection = state.get("protection_config") or {}
    explicit = []
    for value in (
        protection.get("sl_target_pnl"), state.get("sl_target_pnl"),
        state.get("risk_at_entry_usd"),
    ):
        try:
            value = abs(float(value))
            if value > 0:
                explicit.append(value)
        except (TypeError, ValueError):
            pass
    if str(state.get("side", "long")).lower() == "short":
        return max([default_short_risk, *explicit, 0.0])
    try:
        catastrophe = max(float(state.get("total_cost_usd") or 0), 0.0)
    except (TypeError, ValueError):
        catastrophe = 0.0
    return max([catastrophe, *explicit, 0.0])


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    trading_date: str
    trades_today: int
    daily_pnl_usd: float
    open_risk_usd: float
    consecutive_losses: int
    cooldown_remaining_sec: int = 0


def evaluate_entry(
    data_dir: Path,
    proposed_risk_usd: float,
    config: dict[str, Any],
    now: datetime | None = None,
    unrealized_pnl_usd: float = 0.0,
) -> RiskDecision:
    """Evaluate all account-level limits.  Missing/invalid limits are safe defaults."""
    now = now or datetime.now(timezone.utc)
    offset = cfg_int(config, "RISK_DAY_TZ_OFFSET_MIN", 330)
    day = trading_date(now, offset)
    history = [row for row in load_history(data_dir) if not cfg_bool(row, "dry_run", False)]
    today = [t for t in history if _row_date(t, offset) == day]
    pnls = [p for t in today if (p := _pnl(t)) is not None]

    states = {slot: state for slot, state in load_states(data_dir).items()
              if not cfg_bool(state, "dry_run", False)}
    history_keys = {_trade_key(t) for t in today}
    unlogged_today = [
        state for state in states.values()
        if str(state.get("status", "")).upper() in {"OPEN", "CLOSED"}
        and _row_date(state, offset) == day
        and _trade_key(state) not in history_keys
    ]
    # A just-closed state may briefly precede its history append, or a disk
    # error may prevent that append altogether. Count the state so concurrent
    # strategies cannot exploit the read/modify/write gap to exceed account
    # limits. Include its realised P&L when available for the same reason.
    state_pnls = [p for state in unlogged_today
                  if str(state.get("status", "")).upper() == "CLOSED"
                  and (p := _pnl(state)) is not None]
    daily_pnl = round(sum(pnls) + sum(state_pnls)
                      + float(unrealized_pnl_usd or 0), 2)
    trades_today = len(today) + len(unlogged_today)
    max_trades = max(cfg_int(config, "MAX_TRADES_PER_DAY_GLOBAL",
                             cfg_int(config, "MAX_TRADES_PER_DAY", 3)), 1)
    default_short = cfg_float(config, "SHORT_MAX_RISK_USD", 0.0)
    state_risks = [position_risk_usd(s, default_short) for s in states.values()]
    open_risk = round(sum(state_risks), 2)
    unknown_open_risk = any(
        str(state.get("status", "")).upper() == "OPEN" and risk <= 0
        for state, risk in zip(states.values(), state_risks)
    )

    ordered = sorted(
        history,
        key=lambda t: (
            str(t.get("exit_date") or t.get("entry_date") or t.get("date") or ""),
            str(t.get("exit_time_utc") or t.get("exit_time") or ""),
        ),
    )
    consecutive = 0
    for row in reversed(ordered):
        p = _pnl(row)
        if p is None:
            continue
        if p < 0:
            consecutive += 1
        else:
            break

    base = dict(
        trading_date=day,
        trades_today=trades_today,
        daily_pnl_usd=daily_pnl,
        open_risk_usd=open_risk,
        consecutive_losses=consecutive,
    )
    if unknown_open_risk and cfg_bool(config, "RISK_FAIL_CLOSED", True):
        return RiskDecision(False, "an open position has no verifiable risk amount", **base)
    if trades_today >= max_trades:
        return RiskDecision(False, f"global daily trade cap reached ({trades_today}/{max_trades})", **base)

    max_daily_loss = max(cfg_float(config, "MAX_DAILY_LOSS_USD", 500.0), 0.0)
    if max_daily_loss and daily_pnl <= -max_daily_loss:
        return RiskDecision(False, f"daily loss lock reached (${daily_pnl:.2f})", **base)

    max_losses = max(cfg_int(config, "MAX_CONSECUTIVE_LOSSES", 3), 0)
    if max_losses and consecutive >= max_losses:
        return RiskDecision(False, f"consecutive-loss lock reached ({consecutive})", **base)

    cooldown_min = max(cfg_int(config, "LOSS_COOLDOWN_MINUTES", 30), 0)
    closed_ordered = [row for row in ordered if _pnl(row) is not None]
    if consecutive and cooldown_min and closed_ordered:
        last = closed_ordered[-1]
        stamp = str(last.get("exit_at_utc") or "")
        if not stamp:
            d = str(last.get("exit_date") or last.get("entry_date") or last.get("date") or "")
            t = str(last.get("exit_time_utc") or last.get("exit_time") or "")
            stamp = f"{d}T{t}+00:00" if d and t else ""
        try:
            ended = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            remaining = int((ended + timedelta(minutes=cooldown_min) - now).total_seconds())
        except (TypeError, ValueError):
            remaining = 0
        if remaining > 0:
            return RiskDecision(False, "post-loss cooldown active", cooldown_remaining_sec=remaining, **base)

    max_open = max(cfg_float(config, "MAX_OPEN_RISK_USD", 500.0), 0.0)
    if proposed_risk_usd <= 0:
        return RiskDecision(False, "proposed trade has no verified risk budget", **base)
    if max_open and open_risk + proposed_risk_usd > max_open:
        return RiskDecision(
            False,
            f"portfolio open-risk cap exceeded (${open_risk + proposed_risk_usd:.2f} > ${max_open:.2f})",
            **base,
        )
    return RiskDecision(True, "risk checks passed", **base)


def risk_based_lots(
    configured: int,
    affordable: int,
    liquidity_cap: int,
    max_order_lots: int,
    risk_budget_usd: float,
    stop_loss_usd: float,
    premium_per_lot: float,
    round_trip_fee_per_lot: float,
    slippage_per_lot: float,
    short: bool = False,
) -> int:
    """Return the minimum of every independent cap; zero means do not trade.

    TP monitors express SL in total-position USD, so an SL alone does not
    shrink with lots.  We also cap the capital exposed per lot.  For a long,
    paid premium is the catastrophe risk if local protection is unavailable;
    shorts must always have an explicit positive stop/risk budget.
    """
    caps = [configured, affordable, liquidity_cap, max_order_lots]
    if any(int(c) <= 0 for c in caps) or risk_budget_usd <= 0:
        return 0
    if short and stop_loss_usd <= 0:
        return 0
    per_lot = max(premium_per_lot, 0.0) + max(round_trip_fee_per_lot, 0.0) + max(slippage_per_lot, 0.0)
    if per_lot <= 0:
        return 0
    capital_cap = math.floor(risk_budget_usd / per_lot)
    if stop_loss_usd > risk_budget_usd:
        return 0
    return max(min(*(int(c) for c in caps), capital_cap), 0)


def _pid_is_alive(pid: int) -> bool:
    """Conservatively determine whether a local PID still owns a lock.

    A permission error means the process exists.  Any unexpected platform
    error is treated as alive so a mutex is never stolen merely because its
    ownership could not be proved dead.
    """
    if pid <= 0:
        return True
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                # ERROR_INVALID_PARAMETER is Windows' response for a PID that
                # does not exist.  Access denied proves that it does exist.
                return ctypes.get_last_error() != 87
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_lock_record(path: Path) -> tuple[dict[str, Any], str] | None:
    """Strict lock read; malformed ownership must remain fail-closed."""
    try:
        raw = path.read_text(encoding="utf-8")
        record = json.loads(raw)
        pid = record.get("pid") if isinstance(record, dict) else None
        owner = record.get("owner") if isinstance(record, dict) else None
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        if not isinstance(owner, str) or not owner:
            return None
        return record, raw
    except (OSError, ValueError, TypeError):
        return None


def _reclaim_dead_stale_lock(path: Path, stale_after_sec: float) -> bool:
    """Remove a stale mutex only after its readable owner is proven dead."""
    try:
        before = path.stat()
    except OSError:
        return False
    if time.time() - before.st_mtime <= max(float(stale_after_sec), 0.0):
        return False
    parsed = _read_lock_record(path)
    if parsed is None:
        return False
    record, raw = parsed
    if _pid_is_alive(record["pid"]):
        return False
    try:
        # Recheck both metadata and the ownership payload immediately before
        # unlinking; this avoids deleting a lock that changed during inspection.
        current = path.stat()
        if (
            current.st_mtime_ns != before.st_mtime_ns
            or current.st_size != before.st_size
            or path.read_text(encoding="utf-8") != raw
        ):
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _account_lock_path(data_dir: Path, name: str) -> Path:
    clean = str(name).strip().lower()
    if not clean or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in clean):
        raise ValueError("lock name may contain only letters, digits, '_' and '-'")
    return data_dir / f".{clean}.lock"


@contextmanager
def account_file_lock(
    data_dir: Path,
    name: str,
    owner: str,
    stale_after_sec: int = 90,
    wait_sec: float = 0.0,
    poll_sec: float = 0.05,
):
    """Atomic account-scoped named mutex; yields whether it was acquired.

    ``wait_sec`` lets short read/modify/write operations, such as trade-history
    appends, serialize across processes.  A stale file is reclaimed only when
    it contains readable ownership and that PID is confirmed dead.
    """
    data_dir = Path(data_dir)
    path = _account_lock_path(data_dir, name)
    data_dir.mkdir(parents=True, exist_ok=True)
    owner = str(owner).strip()
    if not owner:
        raise ValueError("lock owner is required")
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(float(wait_sec), 0.0)
    acquired = False

    while True:
        fd = None
        created = False
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            created = True
            payload = json.dumps({
                "owner": owner,
                "pid": os.getpid(),
                "token": token,
                "ts": time.time(),
            }).encode("utf-8")
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
            os.close(fd)
            fd = None
            acquired = True
            break
        except FileExistsError:
            if _reclaim_dead_stale_lock(path, stale_after_sec):
                continue
        except OSError:
            # Failure to establish known ownership must be fail-closed.  It is
            # safe to remove only a file this attempt itself just created.
            if created:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            if fd is not None:
                os.close(fd)

        if time.monotonic() >= deadline:
            break
        time.sleep(max(min(float(poll_sec), deadline - time.monotonic()), 0.001))

    try:
        yield acquired
    finally:
        if acquired:
            parsed = _read_lock_record(path)
            if parsed is not None:
                current, _ = parsed
                if (
                    current.get("pid") == os.getpid()
                    and current.get("owner") == owner
                    and current.get("token") == token
                ):
                    try:
                        path.unlink()
                    except OSError:
                        pass


@contextmanager
def account_entry_lock(data_dir: Path, owner: str, stale_after_sec: int = 90):
    """Atomic cross-process strategy-entry mutex; yields acquired bool."""
    with account_file_lock(
        data_dir, "entry", owner, stale_after_sec=stale_after_sec, wait_sec=0.0
    ) as acquired:
        yield acquired


def audit_event(data_dir: Path, event: str, details: dict[str, Any]) -> None:
    """Append one compact JSON event with flush/fsync for post-mortem safety."""
    path = data_dir / "strategy_audit.jsonl"
    record = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **details,
    }
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def decision_dict(decision: RiskDecision) -> dict[str, Any]:
    return asdict(decision)

"""Typed engine configuration: config/default.toml + ENGINE_* env overrides.

Validated on load (Trend_Engine.md §20: refuse to start with unsafe or
incomplete settings).  TOML rather than YAML so parsing is stdlib-only.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.toml"
ENV_PREFIX = "ENGINE_"


class ReconnectConfig(BaseModel):
    initial_backoff_seconds: float = Field(gt=0)
    max_backoff_seconds: float = Field(gt=0)
    jitter_fraction: float = Field(ge=0, le=1)

    @field_validator("max_backoff_seconds")
    @classmethod
    def _max_at_least_initial(cls, v: float, info: Any) -> float:
        initial = info.data.get("initial_backoff_seconds")
        if initial is not None and v < initial:
            raise ValueError("max_backoff must be >= initial_backoff")
        return v


class RateLimitConfig(BaseModel):
    requests_per_second: float = Field(gt=0)
    burst: int = Field(ge=1)


class MarketDataConfig(BaseModel):
    websocket_url: str
    rest_url: str
    channels: list[str] = Field(min_length=1)
    candle_resolutions: list[str] = Field(min_length=1)
    candle_bootstrap_limit: int = Field(ge=50, le=2000)
    reconnect: ReconnectConfig
    rate_limit: RateLimitConfig

    @field_validator("websocket_url")
    @classmethod
    def _wss_only(cls, v: str) -> str:
        if not v.startswith("wss://"):
            raise ValueError("websocket_url must use wss://")
        return v

    @field_validator("rest_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("rest_url must use https://")
        return v


class OrderbookConfig(BaseModel):
    validate_checksums: bool
    feature_depth_levels: int = Field(ge=5, le=500)


class DataQualityConfig(BaseModel):
    stale_multiplier: float = Field(gt=1)
    stale_floor_seconds: float = Field(gt=0)
    median_window: int = Field(ge=8, le=4096)
    max_clock_drift_ms: float = Field(gt=0)
    fail_closed: bool

    @field_validator("fail_closed")
    @classmethod
    def _must_fail_closed(cls, v: bool) -> bool:
        # §3.2 is non-negotiable; the knob exists only to make the stance
        # explicit in configuration review.
        if not v:
            raise ValueError("fail_closed=false is not a supported configuration")
        return v


class StorageConfig(BaseModel):
    data_dir: str
    raw_retention_days: int = Field(ge=1)
    snapshot_retention_days: int = Field(ge=7)
    market_snapshot_retention_days: int = Field(ge=1)
    min_free_gb: float = Field(ge=0.5)
    fsync_interval_seconds: float = Field(gt=0)

    @property
    def data_path(self) -> Path:
        path = Path(self.data_dir)
        return path if path.is_absolute() else REPO_ROOT / path


class EngineSection(BaseModel):
    symbol: str = Field(min_length=1)
    base_currency: str
    quote_currency: str
    host: str
    port: int = Field(ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # ADR 0001: the engine is never exposed beyond the host.
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("engine must bind loopback only (ADR 0001)")
        return v


class EngineConfig(BaseModel):
    engine: EngineSection
    market_data: MarketDataConfig
    orderbook: OrderbookConfig
    data_quality: DataQualityConfig
    storage: StorageConfig
    # Loopback request token; not a trust boundary (ADR 0001). Empty means
    # unset, which api.app refuses at startup.
    token: str = ""


def _apply_env_overrides(raw: dict[str, Any], environ: dict[str, str]) -> None:
    """ENGINE_SECTION__KEY=value overrides raw[section][key] (case-insensitive)."""
    for name, value in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        path = name[len(ENV_PREFIX):].lower().split("__")
        node: Any = raw
        for part in path[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if not isinstance(node, dict):
            continue
        key = path[-1]
        if key not in node:
            continue
        current = node[key]
        if isinstance(current, bool):
            node[key] = value.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int):
            node[key] = int(value)
        elif isinstance(current, float):
            node[key] = float(value)
        elif isinstance(current, list):
            node[key] = [item.strip() for item in value.split(",") if item.strip()]
        else:
            node[key] = value


def load_config(
    path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> EngineConfig:
    env = dict(os.environ) if environ is None else environ
    config_path = path or DEFAULT_CONFIG_PATH
    with open(config_path, "rb") as handle:
        raw = tomllib.load(handle)
    _apply_env_overrides(raw, env)
    raw["token"] = env.get("ENGINE_TOKEN", raw.get("token", ""))
    return EngineConfig.model_validate(raw)

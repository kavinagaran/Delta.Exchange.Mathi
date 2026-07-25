"""FastAPI app + ``python -m btc_trend_engine.api.app`` entry point.

Loopback only (ADR 0001).  ``/health`` is open; everything else requires
``X-Engine-Token``.  The token is not a trust boundary — it stops a stray
request on the host from becoming an input to a trading decision.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request

from .. import __version__
from ..config import EngineConfig, load_config
from ..risk.kill_switch import KillSwitchName, KillSwitchStore
from ..service import EngineService

log = logging.getLogger(__name__)


def require_token(request: Request) -> None:
    expected = request.app.state.token
    if request.headers.get("X-Engine-Token") != expected:
        raise HTTPException(status_code=401, detail="missing or invalid engine token")


def create_app(config: EngineConfig,
               service: EngineService | None = None) -> FastAPI:
    if not config.token:
        raise RuntimeError(
            "ENGINE_TOKEN is not set; refusing to start (Trend_Engine.md §20)")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_service = app.state.service is None
        if owns_service:
            app.state.service = EngineService(config)
        if owns_service:
            await app.state.service.start()
        try:
            yield
        finally:
            if owns_service:
                await app.state.service.stop()

    app = FastAPI(title="btc-trend-engine", version=__version__, lifespan=lifespan)
    app.state.token = config.token
    app.state.service = service
    app.state.kill_switches = KillSwitchStore(
        config.storage.data_path / "risk_kill_switches.json")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "version": __version__}

    def _service(request: Request) -> EngineService:
        engine_service: EngineService | None = request.app.state.service
        if engine_service is None:
            raise HTTPException(status_code=503, detail="service not started")
        return engine_service

    @app.get("/status", dependencies=[Depends(require_token)])
    async def status(request: Request) -> dict[str, object]:
        return _service(request).status()

    @app.get("/trend/latest", dependencies=[Depends(require_token)])
    async def trend_latest(request: Request, symbol: str | None = None
                           ) -> dict[str, object]:
        engine_service = _service(request)
        _require_symbol(engine_service, symbol)
        snapshot = engine_service.producer.latest()
        if snapshot is None:
            # No closed trigger candle yet. 503 rather than a fabricated
            # neutral snapshot: "nothing to say yet" and "no trade" are
            # different facts, and the client fails closed on both.
            raise HTTPException(status_code=503,
                                detail="no snapshot yet; awaiting a closed 5m candle")
        return snapshot

    @app.get("/trend/history", dependencies=[Depends(require_token)])
    async def trend_history(request: Request, symbol: str | None = None,
                            limit: int = 50) -> dict[str, object]:
        engine_service = _service(request)
        _require_symbol(engine_service, symbol)
        return {"symbol": engine_service.config.engine.symbol,
                "snapshots": engine_service.producer.history(
                    max(1, min(limit, 500)))}

    @app.get("/regime/latest", dependencies=[Depends(require_token)])
    async def regime_latest(request: Request, symbol: str | None = None
                            ) -> dict[str, object]:
        engine_service = _service(request)
        _require_symbol(engine_service, symbol)
        snapshot = engine_service.producer.latest() or {}
        return {
            "symbol": engine_service.config.engine.symbol,
            "regime": engine_service.producer.classifier.current.value,
            "regime_since": snapshot.get("regime_since"),
            "trend_score": snapshot.get("trend_score"),
            "data_quality": engine_service.current_data_quality(),
        }

    @app.get("/features/latest", dependencies=[Depends(require_token)])
    async def features_latest(request: Request, symbol: str | None = None
                              ) -> dict[str, object]:
        engine_service = _service(request)
        _require_symbol(engine_service, symbol)
        snapshot = engine_service.producer.latest()
        if snapshot is None:
            raise HTTPException(status_code=503, detail="no snapshot yet")
        return {"symbol": snapshot["symbol"],
                "as_of": snapshot["candle_close_utc"],
                "feature_set_version": snapshot["feature_set_version"],
                "components": snapshot["components"],
                "timeframes": snapshot["timeframes"]}

    def _kill_switches(request: Request) -> KillSwitchStore:
        return request.app.state.kill_switches

    @app.get("/risk/status", dependencies=[Depends(require_token)])
    async def risk_status(request: Request) -> dict[str, object]:
        store = _kill_switches(request)
        status_map = store.status()
        return {
            "any_active": bool(status_map),
            "active": sorted(status_map.keys()),
            "switches": {
                name: {
                    "fired_at_utc": state.fired_at_utc,
                    "last_seen_utc": state.last_seen_utc,
                    "reason": state.reason,
                    "fired_count": state.fired_count,
                }
                for name, state in status_map.items()
            },
        }

    @app.post("/admin/kill-switch", dependencies=[Depends(require_token)])
    async def admin_kill_switch(request: Request) -> dict[str, object]:
        body = await request.json()
        name = str(body.get("name") or "")
        reason = str(body.get("reason") or "manual operator action")
        valid_names = {n.value for n in KillSwitchName}
        if name not in valid_names:
            raise HTTPException(
                status_code=400,
                detail=f"name must be one of {sorted(valid_names)}, got {name!r}")
        state = _kill_switches(request).fire(name, reason)
        log.warning("kill switch fired: %s (%s)", name, reason)
        return {"ok": True, "name": state.name, "fired_at_utc": state.fired_at_utc,
                "reason": state.reason, "fired_count": state.fired_count}

    @app.post("/admin/resume", dependencies=[Depends(require_token)])
    async def admin_resume(request: Request) -> dict[str, object]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str(body.get("name") or "").strip() if isinstance(body, dict) else ""
        store = _kill_switches(request)
        if name:
            cleared = store.resume(name)
            log.warning("kill switch resumed: %s (cleared=%s)", name, cleared)
            return {"ok": True, "cleared": [name] if cleared else []}
        cleared = store.resume_all()
        log.warning("all kill switches resumed: %s", cleared)
        return {"ok": True, "cleared": cleared}

    return app


def _require_symbol(engine_service: EngineService, symbol: str | None) -> None:
    """This engine instance serves exactly one symbol; asking for another must
    be an error, never silently answered with the configured one."""
    configured = engine_service.config.engine.symbol
    if symbol is not None and symbol != configured:
        raise HTTPException(
            status_code=404,
            detail=f"this engine serves {configured}, not {symbol}")


def _watch_stop_sentinel(service_getter, stop_path: Path,
                         loop: asyncio.AbstractEventLoop) -> None:
    """WMI-created Windows children get no console signals (ADR 0001); a
    sentinel file is the portable shutdown channel."""
    import threading
    import time

    def poll() -> None:
        while True:
            if stop_path.exists():
                stop_path.unlink(missing_ok=True)
                log.info("stop sentinel seen; shutting down")
                loop.call_soon_threadsafe(loop.stop)
                return
            time.sleep(2)

    threading.Thread(target=poll, name="stop-sentinel", daemon=True).start()


def _load_dotenv_stdlib(path: Path) -> None:
    """Minimal .env loader (existing os.environ wins). systemd supplies the
    environment via EnvironmentFile; the Windows WMI launch path supplies
    nothing, so the engine reads .env itself without a dotenv dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ENGINE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _load_dotenv_stdlib(Path(__file__).resolve().parent.parent.parent / ".env")
    config = load_config()
    app = create_app(config)

    import uvicorn

    uvicorn_config = uvicorn.Config(
        app, host=config.engine.host, port=config.engine.port,
        log_level="info", access_log=False)
    server = uvicorn.Server(uvicorn_config)

    stop_path = config.storage.data_path / "engine.stop"
    stop_path.unlink(missing_ok=True)

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        for sig in (getattr(signal, "SIGTERM", None), signal.SIGINT):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, server.handle_exit, sig, None)
            except NotImplementedError:
                pass  # Windows event loop: rely on the sentinel below
        _watch_stop_sentinel(lambda: app.state.service, stop_path, loop)
        await server.serve()

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())

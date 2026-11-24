"""Web control panel + REST API.

All /api routes require:  Authorization: Bearer <DASHBOARD_TOKEN>
"""
from __future__ import annotations

import hmac
import logging
import secrets as pysecrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import Secrets
from .engine import Engine
from .store import Store

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web"


def create_app(secrets: Secrets, engine: Engine | None = None, start_engine: bool = True) -> FastAPI:
    if not secrets.dashboard_token:
        secrets.dashboard_token = pysecrets.token_urlsafe(24)
        log.warning("DASHBOARD_TOKEN not set - generated one for this run: %s", secrets.dashboard_token)
        print(f"\n  Dashboard token (set DASHBOARD_TOKEN in .env to keep it): {secrets.dashboard_token}\n")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.engine is None:
            app.state.engine = Engine(secrets, Store(secrets.db_path))
        if start_engine:
            await app.state.engine.start()
        yield
        if start_engine:
            await app.state.engine.stop()

    app = FastAPI(title="Meme Coin Sniper Bot", lifespan=lifespan)
    app.state.engine = engine

    def auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, secrets.dashboard_token):
            raise HTTPException(401, "unauthorized")

    def eng() -> Engine:
        return app.state.engine

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg", include_in_schema=False)
    async def icon():
        return FileResponse(WEB / "icon.svg", media_type="image/svg+xml")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/state", dependencies=[Depends(auth)])
    async def state():
        return eng().snapshot()

    @app.post("/api/toggle", dependencies=[Depends(auth)])
    async def toggle(body: dict[str, Any]):
        on = bool(body.get("enabled"))
        if on and eng().risk.tripped:
            raise HTTPException(409, f"Risk lock active: {eng().risk.trip_reason}. Reset it first.")
        eng().set_enabled(on)
        return {"enabled": eng().settings.enabled}

    @app.post("/api/settings", dependencies=[Depends(auth)])
    async def settings(body: dict[str, Any]):
        body.pop("enabled", None)  # use /api/toggle
        try:
            changed = eng().update_settings(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"changed": changed, "settings": eng().settings.to_dict()}

    @app.post("/api/risk/reset", dependencies=[Depends(auth)])
    async def risk_reset():
        eng().resume_risk()
        return {"ok": True}

    @app.post("/api/positions/{pid}/sell", dependencies=[Depends(auth)])
    async def sell_position(pid: int, body: dict[str, Any] | None = None):
        pos = eng().store.position(pid)
        if not pos or pos["status"] != "open":
            raise HTTPException(404, "position not open")
        fraction = float((body or {}).get("fraction", 1.0))
        ok = await eng().sell(pos, max(0.01, min(fraction, 1.0)), "manual sell (dashboard)")
        if not ok:
            raise HTTPException(502, "sell failed - see events")
        return {"ok": True}

    @app.post("/api/sell-all", dependencies=[Depends(auth)])
    async def sell_all():
        eng().set_enabled(False)
        n = await eng().sell_all("panic sell (dashboard)")
        return {"sold": n}

    @app.post("/api/investigate", dependencies=[Depends(auth)])
    async def investigate(body: dict[str, Any]):
        mint = str(body.get("mint", "")).strip()
        if not 32 <= len(mint) <= 44:
            raise HTTPException(400, "paste a Solana token mint address")
        return await eng().investigate(mint)

    @app.post("/api/buy", dependencies=[Depends(auth)])
    async def manual_buy(body: dict[str, Any]):
        mint = str(body.get("mint", "")).strip()
        if not 32 <= len(mint) <= 44:
            raise HTTPException(400, "paste a Solana token mint address")
        res = await eng().manual_buy(mint)
        if "error" in res:
            raise HTTPException(400, res["error"])
        return res

    @app.post("/api/paper/reset", dependencies=[Depends(auth)])
    async def paper_reset():
        try:
            eng().reset_paper()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/test-notification", dependencies=[Depends(auth)])
    async def test_notification():
        eng().notifier.send("info", "Test notification", "If you can read this, notifications work.")
        return {"channels": eng().notifier.channels}

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):  # pragma: no cover
        log.exception("unhandled API error")
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app

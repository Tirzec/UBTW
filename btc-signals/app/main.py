"""FastAPI app: dashboard, REST and a push websocket. Run: python -m app.main"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import load_config
from .engine import Engine

WEB = Path(__file__).resolve().parent.parent / "web"
log = logging.getLogger("app")


def _clean(o):
    """JSON-safe: NaN/inf -> None."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def create_app(cfg: dict | None = None, start_engine: bool = True) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = Engine(cfg)
        if start_engine:
            await app.state.engine.start()
        yield
        if start_engine:
            await app.state.engine.stop()

    app = FastAPI(title="BTC Up/Down Signals", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/api/state")
    async def state():
        return _clean(app.state.engine.snapshot())

    @app.get("/api/stats")
    async def stats():
        return _clean(app.state.engine.stats())

    @app.get("/api/signals")
    async def signals(limit: int = 50):
        return app.state.engine.db.query("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/paper")
    async def paper(limit: int = 100):
        return app.state.engine.db.query("SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?",
                                         (limit,))

    class PositionIn(BaseModel):
        duration: str = Field(pattern="^(5m|15m)$")
        side: str = Field(pattern="^(?i:up|down)$")
        entry_price: float = Field(gt=0, lt=1)
        shares: float = Field(gt=0)
        slug: str | None = None

    @app.post("/api/positions")
    async def add_position(p: PositionIn):
        try:
            pid = app.state.engine.add_position(p.duration, p.side, p.entry_price, p.shares, p.slug)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"id": pid}

    @app.post("/api/positions/{pid}/close")
    async def close_position(pid: int):
        app.state.engine.close_position(pid)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            while True:
                await sock.send_text(json.dumps(_clean(app.state.engine.snapshot())))
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    sc = cfg.get("server", {})
    uvicorn.run(create_app(cfg), host=sc.get("host", "127.0.0.1"), port=int(sc.get("port", 8000)),
                log_level="warning")


if __name__ == "__main__":
    main()

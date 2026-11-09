"""FastAPI app: dashboard, REST and a push websocket. Run: python -m app.main"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import load_config
from .engine import Engine

WEB = Path(__file__).resolve().parent.parent / "web"
log = logging.getLogger("app")


class PositionIn(BaseModel):
    """Must live at module level: FastAPI can't resolve a locally defined body model."""
    duration: str = Field(pattern="^(5m|15m)$")
    side: str = Field(pattern="^(?i:up|down)$")
    entry_price: float = Field(gt=0, lt=1)
    shares: float = Field(gt=0)
    slug: Optional[str] = None  # Optional[] (not `str | None`) keeps Python 3.9 working


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

    @app.post("/api/positions")
    async def add_position(p: PositionIn):
        try:
            pid = app.state.engine.add_position(p.duration, p.side, p.entry_price, p.shares, p.slug)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"id": pid}

    @app.post("/api/bot/stop")
    async def bot_stop():
        t = app.state.engine.trader
        if t is None:
            raise HTTPException(400, "auto-trader not running")
        n = await t.kill("STOP button on dashboard")
        return {"ok": True, "canceled": n}

    @app.post("/api/bot/resume")
    async def bot_resume():
        t = app.state.engine.trader
        if t is None:
            raise HTTPException(400, "auto-trader not running")
        t.resume()
        return {"ok": True}

    @app.get("/api/bot/trades")
    async def bot_trades(limit: int = 200):
        return app.state.engine.db.query("SELECT * FROM bot_positions ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/bot/events")
    async def bot_events(limit: int = 200):
        return app.state.engine.db.query("SELECT * FROM bot_events ORDER BY id DESC LIMIT ?", (limit,))

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


def ensure_self_signed_cert(cert: Path, key: Path) -> None:
    """Create a localhost certificate with the openssl CLI (built into macOS) if missing."""
    import shutil
    import subprocess

    if cert.exists() and key.exists():
        return
    if not shutil.which("openssl"):
        raise SystemExit("--https needs the `openssl` command (built into macOS; "
                         "on Linux: apt install openssl)")
    cert.parent.mkdir(parents=True, exist_ok=True)
    base = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-days", "825", "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost"]
    san = ["-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"]
    r = subprocess.run(base + san, capture_output=True, text=True)
    if r.returncode != 0:          # very old openssl without -addext
        r = subprocess.run(base, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"could not create certificate: {r.stderr.strip()}")
    log.info("created self-signed certificate %s", cert)


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    from .config import resolve_path

    ap = argparse.ArgumentParser(prog="python -m app.main")
    ap.add_argument("--https", action="store_true",
                    help="serve the dashboard over https with a self-signed certificate "
                         "(use when your browser forces https even for localhost)")
    ap.add_argument("--port", type=int, help="override server.port")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    from .trading.store import install_redaction
    install_redaction()
    tc = cfg.get("trading", {}) or {}
    if tc.get("enabled", True) and tc.get("mode", "paper") == "live":
        from .trading.live_gate import check_live_gates
        cfg["_live_confirmed"] = check_live_gates(
            cfg, str(resolve_path(cfg, cfg.get("db_path", "data/signals.db"))))
        print(f"\n  *** LIVE TRADING ENABLED on {cfg.get('venue', 'polymarket_intl')} - real orders "
              "will be placed ***\n", flush=True)
    sc = cfg.get("server", {})
    host = sc.get("host", "127.0.0.1")
    port = int(args.port or sc.get("port", 8000))
    use_https = args.https or bool(sc.get("https", False))
    ssl = {}
    if use_https:
        cert = resolve_path(cfg, sc.get("ssl_certfile", "data/localhost-cert.pem"))
        key = resolve_path(cfg, sc.get("ssl_keyfile", "data/localhost-key.pem"))
        ensure_self_signed_cert(cert, key)
        ssl = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    scheme = "https" if use_https else "http"
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"\n  Dashboard: {scheme}://{shown}:{port}\n", flush=True)
    if use_https:
        print("  (self-signed certificate: your browser will warn once - choose "
              "'Show Details' / 'Advanced' and continue to localhost)\n", flush=True)
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning", **ssl)


if __name__ == "__main__":
    main()

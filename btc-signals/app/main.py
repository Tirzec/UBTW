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
        e = app.state.engine
        if not e.traders:
            raise HTTPException(400, "auto-trader not running")
        n = await e.kill_all("STOP button on dashboard")
        return {"ok": True, "canceled": n}

    @app.post("/api/bot/resume")
    async def bot_resume():
        e = app.state.engine
        if not e.traders:
            raise HTTPException(400, "auto-trader not running")
        e.resume_all()
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
    """Create a localhost certificate if missing. Pure Python (the `cryptography` package), so it
    works the same on Windows, macOS and Linux; no openssl command needed."""
    if cert.exists() and key.exists():
        return
    import datetime
    import ipaddress

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise SystemExit('--https needs the cryptography package: pip install "cryptography>=42"')
    cert.parent.mkdir(parents=True, exist_ok=True)
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(pk.public_key())
           .serial_number(x509.random_serial_number())
           .not_valid_before(now - datetime.timedelta(minutes=5))
           .not_valid_after(now + datetime.timedelta(days=825))
           .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                          critical=False)
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .sign(pk, hashes.SHA256()))
    key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
    cert.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
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
    ap.add_argument("--no-keep-awake", action="store_true",
                    help="allow the computer to idle-sleep while the bot runs")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    args = ap.parse_args(argv)

    from .console import safe_console
    safe_console()

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
              "'Advanced' and continue to localhost)\n", flush=True)
    # informational only: the clock check never blocks or changes trading
    try:
        from .clock import check_clock
        cr = check_clock(ntp_timeout=1.5)
        print(f"  Clock: [{cr.status}] {cr.detail}" + ("" if cr.status == "PASS" else
              "  -> run `python -m app.clock` for how to fix it"), flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  Clock: could not check ({e})", flush=True)
    from . import keepawake
    if not args.no_keep_awake and sc.get("keep_awake", True):
        print(f"  Keep awake: {keepawake.enable()}", flush=True)
    url = f"{scheme}://{shown}:{port}"
    if not args.no_browser and sc.get("open_browser", False):
        import threading
        import webbrowser
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    print("  Stop: press Ctrl+C here (or run `python -m app.trader stop` for the kill switch)\n", flush=True)
    import os
    pid_file = resolve_path(cfg, "data/bot.pid")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")      # used by windows\stop-bot.ps1
    try:
        uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning", **ssl)
    finally:
        keepawake.disable()
        try:
            if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()

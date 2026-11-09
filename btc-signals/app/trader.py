"""Auto-trader command line.

  python -m app.trader stop      # KILL SWITCH: cancel all open orders, halt trading
  python -m app.trader stop --wait   # ... and wait (up to 15 s) until the running bot confirms
  python -m app.trader resume    # clear a stop / pause
  python -m app.trader status    # mode, risk state, open positions
  python -m app.trader stats     # paper + live results
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time

from .config import load_config, resolve_path
from .trading.live_gate import format_stats
from .trading.store import SCHEMA, bot_stats


def _db(cfg):
    c = sqlite3.connect(str(resolve_path(cfg, cfg.get("db_path", "data/signals.db"))))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _set(c, key, value):
    c.execute("INSERT INTO bot_state (key,value,updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
              "SET value=excluded.value, updated=excluded.updated", (key, json.dumps(value), time.time()))
    c.commit()


async def _direct_cancel_all(cfg: dict | None = None) -> str:
    """Belt and braces for LIVE: cancel on the exchange even if the app isn't running."""
    if (cfg or {}).get("venue") == "polymarket_us":
        kid, sec = os.environ.get("POLYMARKET_KEY_ID"), os.environ.get("POLYMARKET_SECRET_KEY")
        if not (kid and sec):
            return "no Polymarket US keys in .env - nothing to cancel on the exchange"
        from .trading.us_broker import UsLiveBroker
        b = UsLiveBroker(kid, sec, lambda t: {})
        try:
            return f"Polymarket US: canceled {await b.cancel_all()} open orders"
        finally:
            await b.close()
    key, wallet = os.environ.get("POLYMARKET_PRIVATE_KEY"), os.environ.get("POLYMARKET_WALLET_ADDRESS")
    if not key:
        return "no live credentials in .env - nothing to cancel on the exchange"
    try:
        from polymarket import AsyncSecureClient  # type: ignore
    except ImportError:
        return "official SDK not installed - could not cancel on the exchange directly"
    client = await AsyncSecureClient.create(private_key=key, wallet=wallet or None)
    try:
        r = await client.cancel_all()
        return f"exchange: canceled {len(r.canceled)} orders, not canceled {dict(r.not_canceled)}"
    finally:
        await client.close()


def _wait_for_kill(c, t0: float, timeout: float = 15.0) -> str:
    """Wait for the running bot's 'kill' events (one per strategy module) after t0."""
    deadline = time.time() + timeout
    seen: list = []
    while time.time() < deadline:
        seen = [dict(r) for r in c.execute("SELECT module, message, data FROM bot_events "
                                          "WHERE kind='kill' AND ts >= ?", (t0 - 0.5,))]
        if seen and time.time() > t0 + 3:          # give every module a chance to report
            break
        time.sleep(0.5)
    if not seen:
        return ("No confirmation from a running bot (is it running?). The stop stays set: the bot "
                "halts as soon as it starts.")
    out = []
    for r in seen:
        n = (json.loads(r["data"] or "{}") or {}).get("canceled", "?")
        out.append(f"  confirmed: {r['module'] or 'crypto_updown'} halted, {n} open orders canceled")
    return "\n".join(out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.trader")
    ap.add_argument("command", choices=["stop", "resume", "status", "stats"])
    ap.add_argument("--reason", default="STOP from command line")
    ap.add_argument("--wait", action="store_true", help="stop: wait until the running bot confirms the halt")
    a = ap.parse_args(argv)
    from .console import safe_console
    safe_console()
    if a.command != "stop":               # the kill switch must always work
        from .env_guard import check_env_safety
        check_env_safety()
    cfg = load_config()
    c = _db(cfg)
    if a.command == "stop":
        t0 = time.time()
        _set(c, "kill_requested", {"ts": t0, "reason": a.reason})
        print("Kill switch set: the running bot cancels all orders and halts within ~1 second.")
        if a.wait:
            print(_wait_for_kill(c, t0))
        if (cfg.get("trading", {}) or {}).get("mode") == "live":
            print(asyncio.run(_direct_cancel_all(cfg)))
        print("Open positions are NOT sold automatically; manage them in the Polymarket app.")
    elif a.command == "resume":
        _set(c, "kill_requested", None)
        _set(c, "resume_requested", time.time())
        print("Resume requested: the running bot resumes within ~1 second.")
    elif a.command == "status":
        risk = c.execute("SELECT value FROM bot_state WHERE key='risk'").fetchone()
        print("mode:", (cfg.get("trading", {}) or {}).get("mode", "paper"))
        print("risk:", json.dumps(json.loads(risk["value"]) if risk else {}, indent=2))
        for r in c.execute("SELECT * FROM bot_positions WHERE status IN ('opening','open','closing','resolving')"):
            print(f"  #{r['id']} {r['mode']} {r['slug']} {r['side']} {r['status']} shares={r['shares']:.2f} "
                  f"entry={r['entry_price'] or 0:.2f} last_pnl={r['last_pnl'] or 0:+.3f}")
    else:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(bot_positions)")}
        vcol = "COALESCE(venue,'polymarket_intl')" if "venue" in cols else "'polymarket_intl'"
        print("current venue:", cfg.get("venue", "polymarket_intl"))
        for venue in ("polymarket_us", "polymarket_intl"):
            for mode in ("paper", "live"):
                rows = [dict(r) for r in c.execute(f"SELECT * FROM bot_positions WHERE mode=? AND {vcol}=? "
                                                   "AND status='closed' ORDER BY closed_ts", (mode, venue))]
                print(f"{venue} {mode.upper()}:\n{format_stats(len(rows), bot_stats(rows))}")


if __name__ == "__main__":
    main()

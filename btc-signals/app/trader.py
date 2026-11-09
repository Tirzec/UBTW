"""Auto-trader command line.

  python -m app.trader stop      # KILL SWITCH: cancel all open orders, halt trading
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


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.trader")
    ap.add_argument("command", choices=["stop", "resume", "status", "stats"])
    ap.add_argument("--reason", default="STOP from command line")
    a = ap.parse_args(argv)
    cfg = load_config()
    c = _db(cfg)
    if a.command == "stop":
        _set(c, "kill_requested", {"ts": time.time(), "reason": a.reason})
        print("Kill switch set: the running bot cancels all orders and halts within ~1 second.")
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

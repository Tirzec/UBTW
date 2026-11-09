"""Download historical data for the backtester into data/backtest.db.

Sources (all verified live, Nov 2026):
  * Gamma events (one per window): priceToBeat / finalPrice (Chainlink 60s TWAP at each
    boundary), resolved outcome, token ids, fee schedule, rules.
  * Data API v2 /v2/trades?condition=...: the trade tape for each window (cursor paged).
    v1 /trades was retired on 2026-11-02.
  * Binance Vision 1s klines (BTCUSDT): the BTC path inside each window. USDT != USD and
    Binance != Chainlink, so the replay re-anchors the path to each window's priceToBeat.

Usage: python -m backtest collect --days 3 [--durations 5m 15m]
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

import httpx

from app.ingest.discovery import DURATION_SECONDS, Window, apply_event, slug_for

log = logging.getLogger("backtest.data")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_windows (
  slug TEXT PRIMARY KEY, duration TEXT, start INTEGER, end INTEGER, condition_id TEXT,
  token_up TEXT, token_down TEXT, price_to_beat REAL, final_price REAL, outcome TEXT,
  fee_schedule TEXT, rules_ok INTEGER, rules_mismatches TEXT, trades_done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bt_trades (
  slug TEXT, ts INTEGER, outcome TEXT, side TEXT, price REAL, size REAL, tx TEXT
);
CREATE INDEX IF NOT EXISTS bt_trades_slug ON bt_trades(slug, ts);
CREATE TABLE IF NOT EXISTS bt_bars (
  sec INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL, bv REAL, sv REAL
);
"""


def open_db(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    return c


class Throttle:
    def __init__(self, per_sec: float):
        self.gap = 1.0 / per_sec
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if now < self.next:
                await asyncio.sleep(self.next - now)
            self.next = max(now, self.next) + self.gap


async def get_json(client: httpx.AsyncClient, th: Throttle, url: str, params: dict | None = None,
                   tries: int = 8):
    for i in range(tries):
        await th.wait()
        try:
            r = await client.get(url, params=params)
        except httpx.HTTPError as e:
            log.debug("http error %s, retrying", e)
            await asyncio.sleep(min(60, 2 ** i))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("retry-after", 0))
            except ValueError:
                wait = 0
            wait = max(wait, min(60, 2 ** (i + 1)))
            log.debug("HTTP %s from %s, waiting %.0fs", r.status_code, url, wait)
            await asyncio.sleep(wait)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"giving up on {url} {params}")


async def collect_windows(c: sqlite3.Connection, client, th, cfg: dict, durations: list[str],
                          t0: int, t1: int) -> None:
    gamma = cfg["polymarket"]["gamma_url"]
    expected = cfg["polymarket"].get("expected_rules", {})
    have = {r[0] for r in c.execute("SELECT slug FROM bt_windows WHERE outcome IS NOT NULL")}
    todo = []
    for d in durations:
        step = DURATION_SECONDS[d]
        s = (t0 // step) * step
        while s + step <= t1:
            slug = slug_for(d, s)
            if slug not in have:
                todo.append((d, s, slug))
            s += step
    log.info("fetching %d window events from Gamma", len(todo))

    async def one(d, s, slug):
        ev = await get_json(client, th, f"{gamma}/events/slug/{slug}")
        if not ev:
            return
        w = apply_event(Window(d, s, s + DURATION_SECONDS[d], slug), ev, expected)
        if w.outcome is None:
            return
        c.execute("INSERT OR REPLACE INTO bt_windows (slug,duration,start,end,condition_id,token_up,"
                  "token_down,price_to_beat,final_price,outcome,fee_schedule,rules_ok,"
                  "rules_mismatches,trades_done) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                  (slug, d, s, s + DURATION_SECONDS[d], w.condition_id, w.token_up, w.token_down,
                   w.gamma_ptb, w.final_price, w.outcome, json.dumps(w.fee_schedule),
                   int(w.rules.ok), json.dumps(w.rules.mismatches)))

    for i in range(0, len(todo), 50):
        await asyncio.gather(*(one(*x) for x in todo[i:i + 50]))
        c.commit()
        log.info("  windows %d/%d", min(i + 50, len(todo)), len(todo))


async def collect_trades(c: sqlite3.Connection, client, th, cfg: dict, min_usd: float) -> None:
    api = cfg["polymarket"].get("data_api_url", "https://data-api.polymarket.com")
    rows = c.execute("SELECT slug, condition_id FROM bt_windows WHERE trades_done=0").fetchall()
    log.info("fetching trade tapes for %d windows", len(rows))

    failed = 0

    async def one(slug, cond):
        nonlocal failed
        try:
            await _one(slug, cond)
        except Exception as e:  # noqa: BLE001 - leave trades_done=0; a rerun retries it
            failed += 1
            log.warning("trade tape for %s failed (%s) - will retry on next run", slug, e)

    async def _one(slug, cond):
        cursor, n = None, 0
        out = []
        while True:
            p = {"condition": cond, "limit": 1000}
            if min_usd > 0:
                p.update(filter_type="CASH", filter_amount=min_usd)
            if cursor:
                p["cursor"] = cursor
            d = await get_json(client, th, f"{api}/v2/trades", p)
            if not d:
                break
            for t in d.get("data", []):
                out.append((slug, int(t["timestamp"]), t.get("outcome"), t.get("side"),
                            float(t["price"]), float(t["size"]), t.get("transaction_hash")))
            n += 1
            cursor = (d.get("pagination") or {}).get("next_cursor")
            if not cursor or n > 40:
                break
        c.execute("DELETE FROM bt_trades WHERE slug=?", (slug,))
        c.executemany("INSERT INTO bt_trades VALUES (?,?,?,?,?,?,?)", out)
        c.execute("UPDATE bt_windows SET trades_done=1 WHERE slug=?", (slug,))

    for i in range(0, len(rows), 10):
        await asyncio.gather(*(one(*r) for r in rows[i:i + 10]))
        c.commit()
        log.info("  tapes %d/%d", min(i + 10, len(rows)), len(rows))
    if failed:
        log.warning("%d tapes failed; run collect again to retry them", failed)


async def collect_bars(c: sqlite3.Connection, client, th, t0: int, t1: int) -> None:
    """Binance Vision 1s klines; index 9 = taker buy base volume."""
    url = "https://data-api.binance.vision/api/v3/klines"
    have = {r[0] for r in c.execute("SELECT sec FROM bt_bars WHERE sec BETWEEN ? AND ?", (t0, t1))}
    start = t0
    chunks = 0
    while start < t1:
        end = min(start + 1000, t1)
        if all(s in have for s in range(start, end, 97)):
            start = end
            continue
        d = await get_json(client, th, url, {"symbol": "BTCUSDT", "interval": "1s",
                                             "startTime": start * 1000, "endTime": end * 1000 - 1,
                                             "limit": 1000})
        rows = []
        for k in d or []:
            v, bv = float(k[5]), float(k[9])
            rows.append((k[0] // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                         v, bv, v - bv))
        c.executemany("INSERT OR REPLACE INTO bt_bars VALUES (?,?,?,?,?,?,?,?)", rows)
        chunks += 1
        if chunks % 20 == 0:
            c.commit()
            log.info("  bars up to %s", time.strftime("%Y-%m-%d %H:%M", time.gmtime(end)))
        start = end
    c.commit()


async def collect(cfg: dict, db_path: str, days: float, durations: list[str],
                  min_trade_usd: float = 0.0, end: int | None = None) -> None:
    t1 = int(end or time.time()) - 1800       # leave time for resolution
    t0 = int(t1 - days * 86400)
    c = open_db(db_path)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "btc-signals-backtest/1.0"}) as client:
        await collect_windows(c, client, Throttle(8), cfg, durations, t0, t1)
        await collect_bars(c, client, Throttle(8), t0 - 1200, t1)
        await collect_trades(c, client, Throttle(2), cfg, min_trade_usd)
    n = c.execute("SELECT COUNT(*) FROM bt_windows").fetchone()[0]
    log.info("done: %d windows in %s", n, db_path)

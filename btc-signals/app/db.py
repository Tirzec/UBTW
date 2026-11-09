"""SQLite storage (stdlib sqlite3, WAL). Small writes from the event loop are fine."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
  slug TEXT PRIMARY KEY, duration TEXT, start INTEGER, end INTEGER, condition_id TEXT,
  token_up TEXT, token_down TEXT, price_to_beat REAL, ptb_source TEXT, gamma_ptb REAL,
  final_price REAL, outcome TEXT, rules_ok INTEGER, rules_mismatches TEXT, rules_text TEXT,
  fee_schedule TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS ticks (
  ts REAL, slug TEXT, seconds_left REAL, ref_spot REAL, ref_twap REAL, ref_source TEXT,
  spot REAL, spot_source TEXT, up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL,
  p_up REAL, degraded INTEGER
);
CREATE INDEX IF NOT EXISTS ticks_slug ON ticks(slug, ts);
CREATE TABLE IF NOT EXISTS predictions (
  slug TEXT, ts REAL, seconds_left REAL, p_up REAL, p_prior REAL, mkt_up_mid REAL,
  degraded INTEGER, outcome TEXT
);
CREATE INDEX IF NOT EXISTS pred_slug ON predictions(slug);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, slug TEXT, duration TEXT, action TEXT,
  p_up REAL, p_side REAL, mkt_price REAL, edge REAL, seconds_left REAL,
  drivers TEXT, blocked TEXT, features TEXT
);
CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, slug TEXT, duration TEXT, side TEXT,
  opened_ts REAL, entry_price REAL, shares REAL, cost REAL, entry_fee REAL, p_model REAL,
  edge REAL, exit_ts REAL, exit_price REAL, exit_fee REAL, exit_proceeds REAL, exit_reason TEXT,
  status TEXT, won INTEGER, pnl_rule REAL, pnl_hold REAL, settled_ts REAL
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, duration TEXT, side TEXT,
  entry_price REAL, shares REAL, opened_ts REAL, closed_ts REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS news (
  link TEXT PRIMARY KEY, ts REAL, source TEXT, title TEXT, label TEXT, score REAL,
  confidence REAL, hits TEXT
);
CREATE TABLE IF NOT EXISTS feed_events (ts REAL, kind TEXT, detail TEXT);
"""


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def insert(self, table: str, row: dict) -> int:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))
        return int(cur.lastrowid)

    def upsert_window(self, w) -> None:
        self.execute(
            """INSERT INTO windows (slug,duration,start,end,condition_id,token_up,token_down,
               price_to_beat,ptb_source,gamma_ptb,final_price,outcome,rules_ok,rules_mismatches,
               rules_text,fee_schedule,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET condition_id=excluded.condition_id,
               token_up=excluded.token_up, token_down=excluded.token_down,
               price_to_beat=excluded.price_to_beat, ptb_source=excluded.ptb_source,
               gamma_ptb=excluded.gamma_ptb, final_price=excluded.final_price,
               outcome=excluded.outcome, rules_ok=excluded.rules_ok,
               rules_mismatches=excluded.rules_mismatches, rules_text=excluded.rules_text,
               fee_schedule=excluded.fee_schedule, updated=excluded.updated""",
            (w.slug, w.duration, w.start, w.end, w.condition_id, w.token_up, w.token_down,
             w.price_to_beat, w.ptb_source, w.gamma_ptb, w.final_price, w.outcome,
             None if w.rules is None else int(w.rules.ok),
             json.dumps(w.rules.mismatches if w.rules else []),
             w.rules.raw_text if w.rules else "", json.dumps(w.fee_schedule), time.time()))

    def log_event(self, kind: str, detail: str) -> None:
        self.execute("INSERT INTO feed_events VALUES (?,?,?)", (time.time(), kind, detail))

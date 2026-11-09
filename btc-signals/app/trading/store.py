"""Audit trail for the auto-trader: SQLite tables + a log file. Secrets are redacted."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, slug TEXT, duration TEXT, side TEXT,
  token TEXT, condition_id TEXT, status TEXT,            -- opening|open|closing|resolving|closed|void
  shares REAL DEFAULT 0, entry_price REAL, cost REAL DEFAULT 0, entry_fee REAL DEFAULT 0,
  p_model REAL, edge REAL, opened_ts REAL, filled_ts REAL,
  exit_shares REAL DEFAULT 0, exit_proceeds REAL DEFAULT 0, exit_fee REAL DEFAULT 0,
  exit_price REAL, exit_reason TEXT, exit_trigger TEXT, payout REAL DEFAULT 0,
  pnl REAL, won INTEGER, closed_ts REAL, entry_order TEXT, exit_order TEXT,
  tp_price REAL, sl_price REAL, last_value REAL, last_pnl REAL, last_note TEXT
);
CREATE INDEX IF NOT EXISTS bot_pos_status ON bot_positions(mode, status);
CREATE TABLE IF NOT EXISTS bot_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, position_id INTEGER,
  order_id TEXT, token TEXT, side TEXT, price REAL, size REAL, post_only INTEGER,
  status TEXT, filled REAL, avg_price REAL, fee REAL, purpose TEXT, error TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS bot_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mode TEXT, level TEXT, kind TEXT,
  message TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT, updated REAL);
"""

SECRET_ENV = ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_SECRET_KEY", "POLYMARKET_KEY_ID", "THERUNDOWN_API_KEY", "SHARPAPI_KEY",
              "THE_ODDS_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE", "POLY_API_KEY",
              "TELEGRAM_BOT_TOKEN", "NTFY_TOKEN")
_HEX_KEY = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")


def paper_count_from(conf: dict | None) -> float:
    """`paper_count_from` (ISO time) in a module's config: paper trades opened before it don't
    count toward the live requirement (set when the strategy changes). 0 = count everything."""
    v = (conf or {}).get("paper_count_from")
    if not v:
        return 0.0
    from datetime import datetime
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


class RedactSecrets(logging.Filter):
    """Masks private keys / API secrets in every log record, wherever they come from."""

    def __init__(self) -> None:
        super().__init__()
        self.values = [v for k in SECRET_ENV if len(v := os.environ.get(k, "")) >= 8]

    def _clean(self, s: str) -> str:
        for v in self.values:
            s = s.replace(v, "***REDACTED***")
        return _HEX_KEY.sub("***REDACTED-KEY***", s)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = self._clean(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def install_redaction() -> None:
    f = RedactSecrets()
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)


def setup_trade_log(path: Path) -> None:
    log = logging.getLogger("trader")
    if any(getattr(h, "_trade_log", False) for h in log.handlers):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    h._trade_log = True  # type: ignore[attr-defined]
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    h.addFilter(RedactSecrets())
    log.addHandler(h)
    log.setLevel(logging.INFO)


class TradeStore:
    NEW_COLUMNS = {   # added after v1; older rows get the defaults (crypto, intl)
        "venue": "TEXT DEFAULT 'polymarket_intl'", "module": "TEXT DEFAULT 'crypto_updown'",
        "league": "TEXT", "group_key": "TEXT", "bet_type": "TEXT", "confidence": "REAL",
        "starts_at": "REAL", "reasons": "TEXT", "closing_fair": "REAL", "clv": "REAL",
    }

    def __init__(self, db, mode: str, venue: str = "polymarket_intl", module: str = "crypto_updown"):
        self.db = db
        self.mode = mode
        self.venue = venue
        self.module = module
        db.conn.executescript(SCHEMA)
        cols = {r["name"] for r in db.query("PRAGMA table_info(bot_positions)")}
        for col, typ in self.NEW_COLUMNS.items():
            if col not in cols:
                db.conn.execute(f"ALTER TABLE bot_positions ADD COLUMN {col} {typ}")
        ecols = {r["name"] for r in db.query("PRAGMA table_info(bot_events)")}
        if "module" not in ecols:
            db.conn.execute("ALTER TABLE bot_events ADD COLUMN module TEXT")
        db.conn.commit()
        self.log = logging.getLogger("trader")

    # ---------------------------------------------------------------- events
    def event(self, kind: str, message: str, level: str = "INFO", **data) -> None:
        self.db.execute("INSERT INTO bot_events (ts,mode,level,kind,message,data,module) VALUES (?,?,?,?,?,?,?)",
                        (time.time(), self.mode, level, kind, message, json.dumps(data, default=str),
                         self.module))
        tag = self.mode.upper() if self.module == "crypto_updown" else f"{self.mode.upper()} {self.module}"
        self.log.log(getattr(logging, level, logging.INFO), "[%s] %s %s %s", tag, kind,
                     message, json.dumps(data, default=str) if data else "")

    def order(self, o, position_id: int | None, purpose: str) -> None:
        row = self.db.query("SELECT id FROM bot_orders WHERE order_id=? AND mode=?", (o.order_id, self.mode))
        vals = (o.status, o.filled, o.avg_price, o.fee, o.error, time.time())
        if row:
            self.db.execute("UPDATE bot_orders SET status=?, filled=?, avg_price=?, fee=?, error=?, "
                            "updated=? WHERE id=?", vals + (row[0]["id"],))
        else:
            self.db.execute("INSERT INTO bot_orders (ts,mode,position_id,order_id,token,side,price,size,"
                            "post_only,status,filled,avg_price,fee,purpose,error,updated) VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (o.created, self.mode, position_id, o.order_id, o.token, o.side, o.price,
                             o.size, int(o.post_only)) + vals[:4] + (purpose, o.error, time.time()))

    # ---------------------------------------------------------------- positions
    def new_position(self, **kw) -> int:
        kw.setdefault("mode", self.mode)
        kw.setdefault("venue", self.venue)
        kw.setdefault("module", self.module)
        return self.db.insert("bot_positions", kw)

    def update_position(self, pid: int, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE bot_positions SET {sets} WHERE id=?", tuple(kw.values()) + (pid,))

    def position(self, pid: int) -> dict:
        return self.db.query("SELECT * FROM bot_positions WHERE id=?", (pid,))[0]

    def active_positions(self) -> list[dict]:
        return self.db.query("SELECT * FROM bot_positions WHERE mode=? AND COALESCE(venue,'polymarket_intl')=? "
                             "AND COALESCE(module,'crypto_updown')=? "
                             "AND status IN ('opening','open','closing','resolving') ORDER BY id",
                             (self.mode, self.venue, self.module))

    def closed_positions(self, mode: str | None = None, since: float = 0, limit: int = 100000) -> list[dict]:
        return self.db.query("SELECT * FROM bot_positions WHERE mode=? AND COALESCE(venue,'polymarket_intl')=? "
                             "AND COALESCE(module,'crypto_updown')=? "
                             "AND status='closed' AND closed_ts>=? ORDER BY closed_ts LIMIT ?",
                             (mode or self.mode, self.venue, self.module, since, limit))

    def counted_paper_trades(self, since: float = 0.0) -> list[dict]:
        """Closed PAPER trades opened at/after `since` (the live-readiness count)."""
        return [p for p in self.closed_positions(mode="paper") if (p.get("opened_ts") or 0) >= since]

    # ---------------------------------------------------------------- state
    def get_state(self, key: str, default=None):
        r = self.db.query("SELECT value FROM bot_state WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set_state(self, key: str, value) -> None:
        self.db.execute("INSERT INTO bot_state (key,value,updated) VALUES (?,?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value, updated=excluded.updated",
                        (key, json.dumps(value), time.time()))


def bot_stats(rows: list[dict]) -> dict:
    """Win rate, average win vs loss, P&L, drawdown for closed bot positions."""
    pnls = [r["pnl"] for r in rows if r.get("pnl") is not None]
    if not pnls:
        return {"n": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak = cum = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    stake = sum((r["cost"] or 0) + (r["entry_fee"] or 0) for r in rows)
    triggers: dict[str, int] = {}
    for r in rows:
        k = r.get("exit_trigger") or "resolution"
        triggers[k] = triggers.get(k, 0) + 1
    return {
        "n": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(pnls), "total_pnl": sum(pnls),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "ev_per_trade": sum(pnls) / len(pnls), "return_on_stake": sum(pnls) / stake if stake else 0.0,
        "max_drawdown": dd, "fees_paid": sum((r["entry_fee"] or 0) + (r["exit_fee"] or 0) for r in rows),
        "exit_triggers": triggers,
    }

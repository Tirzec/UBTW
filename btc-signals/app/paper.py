"""Paper trading: every signal becomes a simulated trade at the live book, with fees.

Two PnL paths are tracked per trade:
  pnl_rule - following the exit engine's first SELL (or holding if none)
  pnl_hold - always holding to resolution
"""
from __future__ import annotations

import math
import time

from .db import DB
from .fees import walk_buy, walk_sell


class PaperTrader:
    def __init__(self, db: DB, stake_usd: float = 50, enabled: bool = True):
        self.db = db
        self.stake = stake_usd
        self.enabled = enabled

    def open(self, *, signal_id: int, slug: str, duration: str, side: str, asks: list,
             p_model: float, edge: float, fee_schedule: dict | None) -> int | None:
        if not self.enabled or not asks:
            return None
        f = walk_buy(asks, self.stake, fee_schedule)
        if f.shares <= 0:
            return None
        return self.db.insert("paper_trades", {
            "signal_id": signal_id, "slug": slug, "duration": duration, "side": side,
            "opened_ts": time.time(), "entry_price": f.avg_price, "shares": f.shares,
            "cost": f.cost, "entry_fee": f.fee, "p_model": p_model, "edge": edge,
            "status": "open"})

    def open_trades(self) -> list[dict]:
        return self.db.query("SELECT * FROM paper_trades WHERE status='open'")

    def exit(self, trade: dict, bids: list, reason: str, fee_schedule: dict | None) -> None:
        f = walk_sell(bids, trade["shares"], fee_schedule)
        if f.shares <= 0:
            return
        # anything the book could not absorb is treated as held to resolution at settlement
        self.db.execute(
            "UPDATE paper_trades SET exit_ts=?, exit_price=?, exit_fee=?, exit_proceeds=?, "
            "exit_reason=?, status='exited' WHERE id=?",
            (time.time(), f.avg_price, f.fee, f.cost, reason + ("" if f.complete else " (partial)"),
             trade["id"]))
        if not f.complete:
            self.db.execute("UPDATE paper_trades SET exit_reason=exit_reason||? WHERE id=?",
                            (f" filled {f.shares:.2f}/{trade['shares']:.2f}", trade["id"]))

    def settle(self, slug: str, outcome: str) -> int:
        rows = self.db.query("SELECT * FROM paper_trades WHERE slug=? AND status IN ('open','exited')",
                             (slug,))
        for t in rows:
            won = int(t["side"] == outcome)
            basis = t["cost"] + t["entry_fee"]
            pnl_hold = t["shares"] * won - basis
            if t["status"] == "exited":
                sold = (t["exit_proceeds"] or 0.0)
                sold_shares = sold / t["exit_price"] if t["exit_price"] else 0.0
                rest = max(0.0, t["shares"] - sold_shares)
                pnl_rule = sold - (t["exit_fee"] or 0.0) + rest * won - basis
            else:
                pnl_rule = pnl_hold
            self.db.execute("UPDATE paper_trades SET status='settled', won=?, pnl_rule=?, "
                            "pnl_hold=?, settled_ts=? WHERE id=?",
                            (won, pnl_rule, pnl_hold, time.time(), t["id"]))
        self.db.execute("UPDATE predictions SET outcome=? WHERE slug=?", (outcome, slug))
        return len(rows)


def max_drawdown(pnls: list[float]) -> float:
    peak = cum = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def calibration(pairs: list[tuple[float, int]], bins: int = 10) -> list[dict]:
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = [(p, y) for p, y in pairs if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if sel:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "predicted": sum(p for p, _ in sel) / len(sel),
                        "actual": sum(y for _, y in sel) / len(sel), "n": len(sel)})
    return out


def brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def trade_stats(trades: list[dict], pnl_key: str = "pnl_rule") -> dict:
    done = [t for t in trades if t.get(pnl_key) is not None]
    pnls = [t[pnl_key] for t in done]
    n = len(done)
    stake = sum(t["cost"] + t["entry_fee"] for t in done)
    if not n:
        return {"n": 0}
    mean = sum(pnls) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (n - 1)) if n > 1 else 0.0
    return {
        "n": n,
        "win_rate": sum(t["won"] for t in done) / n,
        "avg_edge": sum(t["edge"] for t in done) / n,
        "avg_entry": sum(t["entry_price"] for t in done) / n,
        "ev_per_trade": mean,
        "ev_per_dollar": sum(pnls) / stake if stake else 0.0,
        "total_pnl": sum(pnls),
        "max_drawdown": max_drawdown(pnls),
        "pnl_sd": sd,
        "t_stat": mean / (sd / math.sqrt(n)) if sd > 0 else None,
    }


def paper_stats(db: DB) -> dict:
    trades = db.query("SELECT * FROM paper_trades WHERE status='settled' ORDER BY settled_ts")
    preds = db.query("SELECT p_up, outcome FROM predictions WHERE outcome IS NOT NULL "
                     "AND degraded=0")
    pairs_model = [(r["p_up"], int(r["outcome"] == "UP")) for r in preds]
    pairs_trades = [(t["p_model"], t["won"]) for t in trades]
    by_dur = {d: {"rule": trade_stats([t for t in trades if t["duration"] == d]),
                  "hold": trade_stats([t for t in trades if t["duration"] == d], "pnl_hold")}
              for d in ("5m", "15m")}
    return {
        "rule": trade_stats(trades, "pnl_rule"),
        "hold": trade_stats(trades, "pnl_hold"),
        "by_duration": by_dur,
        "open": len(db.query("SELECT id FROM paper_trades WHERE status IN ('open','exited')")),
        "windows_observed": db.query("SELECT COUNT(DISTINCT slug) n FROM predictions "
                                     "WHERE outcome IS NOT NULL")[0]["n"],
        "calibration_trades": calibration(pairs_trades),
        "calibration_model": calibration(pairs_model),
        "brier_model": brier(pairs_model),
        "equity": [round(sum(t["pnl_rule"] for t in trades[: i + 1]), 4)
                   for i in range(len(trades))][-500:],
    }

"""Sports validation from OUR OWN saved data (no paid history):

  * sports_closing: closing consensus fair (last snapshot before start) for every market we
    tracked, plus Polymarket's mid at close and the official settlement.
  * bot_positions (module='sports'): every paper/live trade with entry price, P&L and CLV.

Reports per league and bet type: trades, win rate, ROI, average CLV (closing fair - entry price),
and calibration of the closing consensus (did "60%" happen 60% of the time?) and of Polymarket's
closing price for comparison. Run: python -m backtest sports [--mode paper]
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

from app.paper import brier, calibration
from app.trading.store import bot_stats


def load(db_path: str, mode: str = "paper") -> tuple[list[dict], list[dict]]:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    closing = [dict(r) for r in c.execute("SELECT * FROM sports_closing")] if "sports_closing" in tables else []
    trades = []
    if "bot_positions" in tables:
        cols = {r[1] for r in c.execute("PRAGMA table_info(bot_positions)")}
        if "module" in cols:
            trades = [dict(r) for r in c.execute("SELECT * FROM bot_positions WHERE module='sports' AND mode=? "
                                                 "AND status='closed' ORDER BY closed_ts", (mode,))]
    c.close()
    return closing, trades


def group_stats(trades: list[dict], key: str) -> dict:
    out: dict = {}
    for t in trades:
        out.setdefault(t.get(key) or "-", []).append(t)
    res = {}
    for k, rows in out.items():
        st = bot_stats(rows)
        clv = [r["clv"] for r in rows if r.get("clv") is not None]
        st["avg_clv"] = sum(clv) / len(clv) if clv else None
        st["positive_clv_rate"] = sum(1 for x in clv if x > 0) / len(clv) if clv else None
        res[k] = st
    return res


def summarize(closing: list[dict], trades: list[dict]) -> dict:
    settled = [r for r in closing if r.get("settled_long") is not None and r.get("closing_fair_long") is not None]
    # settlement can be 0.5 (ties) or a last-fair-market price (voids): only clean 0/1 outcomes calibrate
    clean = [r for r in settled if r["settled_long"] in (0.0, 1.0)]
    cons = [(r["closing_fair_long"], int(r["settled_long"])) for r in clean]
    pm = [(r["pm_mid_at_close"], int(r["settled_long"])) for r in clean if r.get("pm_mid_at_close") is not None]
    clv = [t["clv"] for t in trades if t.get("clv") is not None]
    return {
        "markets_with_closing_line": len(closing), "settled_markets": len(settled),
        "trades": bot_stats(trades),
        "avg_clv": sum(clv) / len(clv) if clv else None,
        "positive_clv_rate": sum(1 for x in clv if x > 0) / len(clv) if clv else None,
        "by_league": group_stats(trades, "league"), "by_bet_type": group_stats(trades, "bet_type"),
        "calibration_consensus": calibration(cons), "brier_consensus": brier(cons),
        "calibration_polymarket_close": calibration(pm), "brier_polymarket_close": brier(pm),
    }


def write_html(summary: dict, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "sports_summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")

    def cents(x) -> str:
        return "" if x is None else "{:+.1f}¢".format(x * 100)

    def table(groups: dict) -> str:
        rows = "".join(
            f"<tr><td>{html.escape(str(k))}</td><td>{v.get('n', 0)}</td><td>{(v.get('win_rate') or 0):.1%}</td>"
            f"<td>{(v.get('total_pnl') or 0):+.2f}</td><td>{(v.get('return_on_stake') or 0):+.1%}</td>"
            f"<td>{cents(v.get('avg_clv'))}</td></tr>"
            for k, v in groups.items())
        return f"<table><tr><th></th><th>Trades</th><th>Win</th><th>P&L</th><th>ROI</th><th>Avg CLV</th></tr>{rows}</table>"

    def cal(rows: list) -> str:
        return "<table><tr><th>Predicted</th><th>Actual</th><th>n</th></tr>" + "".join(
            f"<tr><td>{r['predicted']:.2f}</td><td>{r['actual']:.2f}</td><td>{r['n']}</td></tr>" for r in rows) + "</table>"

    t = summary["trades"]
    page = f"""<!doctype html><meta charset="utf-8"><title>Sports validation</title>
<style>body{{font:14px system-ui;max-width:1000px;margin:24px auto;padding:0 16px}}table{{border-collapse:collapse;margin:8px 0}}
td,th{{border-bottom:1px solid #ddd;padding:3px 10px;text-align:left}}</style>
<h1>Sports validation (our own closing lines)</h1>
<p>Markets with a recorded closing line: {summary['markets_with_closing_line']} · settled: {summary['settled_markets']} ·
trades: {t.get('n', 0)} · average CLV: {cents(summary['avg_clv'])} ·
positive CLV rate: {'' if summary['positive_clv_rate'] is None else '{:.0%}'.format(summary['positive_clv_rate'])}</p>
<h2>By league</h2>{table(summary['by_league'])}<h2>By bet type</h2>{table(summary['by_bet_type'])}
<h2>Calibration</h2><p>Brier: consensus {summary['brier_consensus']} vs Polymarket close {summary['brier_polymarket_close']}</p>
<div style="display:flex;gap:32px"><div><h3>Sportsbook consensus at close</h3>{cal(summary['calibration_consensus'])}</div>
<div><h3>Polymarket price at close</h3>{cal(summary['calibration_polymarket_close'])}</div></div>"""
    p = out / "sports_report.html"
    p.write_text(page, encoding="utf-8")
    return p

"""Daily summary: trades, P&L after fees, win rate, average win vs loss, CLV (sports).

Sent as a phone alert once a day (notifications.daily_summary_at, local time) and available any time:
    python -m app.trader summary            # today
    python -m app.trader summary --date 2026-11-09
"""
from __future__ import annotations

import time

from .store import bot_stats

MODULE_LABEL = {"crypto_updown": "Crypto", "sports": "Sports"}


def day_bounds(date: str | None = None, now: float | None = None) -> tuple[float, float, str]:
    if date:
        t = time.strptime(date, "%Y-%m-%d")
    else:
        t = time.localtime(now or time.time())
    start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return start, start + 86400, time.strftime("%Y-%m-%d", time.localtime(start))


def build_summary(db, *, mode: str, venue: str = "polymarket_us", date: str | None = None,
                  now: float | None = None) -> dict:
    start, end, day = day_bounds(date, now)
    rows = db.query("SELECT * FROM bot_positions WHERE mode=? AND status='closed' AND closed_ts>=? AND closed_ts<? "
                    "AND COALESCE(venue,'polymarket_intl')=? AND COALESCE(module,'crypto_updown')!='test_order' "
                    "ORDER BY closed_ts", (mode, start, end, venue))
    out = {"day": day, "mode": mode, "modules": {}}
    allrows = []
    for mod in sorted({r.get("module") or "crypto_updown" for r in rows} | {"crypto_updown"}):
        mr = [r for r in rows if (r.get("module") or "crypto_updown") == mod]
        allrows += mr
        s = bot_stats(mr) if mr else {}
        entry = {"trades": len(mr), "pnl": s.get("total_pnl", 0.0), "win_rate": s.get("win_rate"),
                 "avg_win": s.get("avg_win"), "avg_loss": s.get("avg_loss"), "fees": s.get("fees_paid", 0.0)}
        if mod == "sports":
            clvs = [r["clv"] for r in mr if r.get("clv") is not None]
            entry["clv_avg"] = sum(clvs) / len(clvs) if clvs else None
            entry["clv_n"] = len(clvs)
            entry["clv_beat"] = sum(1 for c in clvs if c > 0)
        out["modules"][mod] = entry
    tot = bot_stats(allrows) if allrows else {}
    out["total"] = {"trades": len(allrows), "pnl": tot.get("total_pnl", 0.0), "win_rate": tot.get("win_rate"),
                    "avg_win": tot.get("avg_win"), "avg_loss": tot.get("avg_loss"), "fees": tot.get("fees_paid", 0.0)}
    return out


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def _usd(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def format_summary(s: dict) -> tuple[str, str]:
    """(title, body) for a phone alert / the terminal."""
    t = s["total"]
    title = f"Daily summary {s['day']} ({s['mode'].upper()}): {t['trades']} trades, P&L {_usd(t['pnl'])} after fees"
    lines = []
    for mod, e in s["modules"].items():
        line = (f"{MODULE_LABEL.get(mod, mod)}: {e['trades']} trades, P&L {_usd(e['pnl'])}, win rate {_pct(e['win_rate'])}, "
                f"avg win {_usd(e['avg_win'])} vs avg loss {_usd(e['avg_loss'])}, fees ${e['fees'] or 0:.2f}")
        if mod == "sports":
            line += (f", CLV {e['clv_avg']:+.3f} avg ({e['clv_beat']}/{e['clv_n']} beat the close)"
                     if e.get("clv_avg") is not None else ", CLV - (no closing lines yet)")
        lines.append(line)
    lines.append(f"Total: win rate {_pct(t['win_rate'])}, avg win {_usd(t['avg_win'])} vs avg loss "
                 f"{_usd(t['avg_loss'])}, fees ${t['fees'] or 0:.2f}")
    return title, "\n".join(lines)

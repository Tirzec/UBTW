"""Daily summary: trades, P&L after fees, win rate, average win vs loss, CLV (sports).

Sent as a phone alert once a day (notifications.daily_summary_at, local time) and available any time:
    python -m app.trader summary            # today
    python -m app.trader summary --date 2026-11-09
"""
from __future__ import annotations

import time

from .store import bot_stats

MODULE_LABEL = {"crypto_updown": "Crypto", "sports": "Sports", "fair_value": "Crypto fair-value"}
CAL_BUCKETS = [(i / 10, (i + 1) / 10) for i in range(10)]


def break_even_win_rate(avg_win, avg_loss):
    """Win rate needed for zero P&L given the average win and average loss."""
    if not avg_win or avg_loss is None or avg_win <= 0:
        return None
    lo = abs(avg_loss)
    return lo / (avg_win + lo) if (avg_win + lo) > 0 else None


def calibration(rows: list[dict]) -> dict:
    """Predicted probability at entry (p_model) vs. whether that side actually won (side_won)."""
    pts = [(r["p_model"], r["side_won"]) for r in rows
           if r.get("p_model") is not None and r.get("side_won") is not None]
    out, ece = [], 0.0
    for lo, hi in CAL_BUCKETS:
        b = [(p, y) for p, y in pts if lo <= p < hi or (hi == 1 and p == 1)]
        if b:
            mp, wr = sum(p for p, _ in b) / len(b), sum(y for _, y in b) / len(b)
            out.append({"bucket": f"{int(lo * 100)}-{int(hi * 100)}%", "n": len(b), "predicted": round(mp, 4),
                        "actual": round(wr, 4)})
            ece += len(b) * abs(mp - wr)
    return {"n": len(pts), "buckets": out, "error": (ece / len(pts)) if pts else None}


def strategy_stats(rows: list[dict]) -> dict:
    s = bot_stats(rows) if rows else {}
    e = {"trades": len(rows), "pnl": s.get("total_pnl", 0.0), "win_rate": s.get("win_rate"),
         "avg_win": s.get("avg_win"), "avg_loss": s.get("avg_loss"), "fees": s.get("fees_paid", 0.0)}
    e["break_even_win_rate"] = break_even_win_rate(e["avg_win"], e["avg_loss"])
    e["calibration"] = calibration(rows)
    return e


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
    def key(r):
        m = r.get("module") or "crypto_updown"
        return r.get("strategy") or m if m == "crypto_updown" else m
    for mod in sorted({key(r) for r in rows} | {"fair_value"}):
        mr = [r for r in rows if key(r) == mod]
        allrows += mr
        entry = strategy_stats(mr)
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


def self_check(rows: list[dict], *, after: int = 100, every: int = 25, max_cal_error: float = 0.10) -> dict | None:
    """After `after` fair-value trades (then every `every`): fail if calibration is poor or P&L < 0."""
    n = len(rows)
    if n < after or (n - after) % every:
        return None
    st = strategy_stats(rows)
    cal = st["calibration"]
    problems = []
    if st["pnl"] < 0:
        problems.append(f"P&L after fees is {st['pnl']:+.2f}")
    if cal["error"] is not None and cal["n"] >= 30 and cal["error"] > max_cal_error:
        problems.append(f"calibration error {cal['error']:.1%} > {max_cal_error:.0%}")
    return {"n": n, "ok": not problems, "problems": problems, "stats": st}


def format_summary(s: dict) -> tuple[str, str]:
    """(title, body) for a phone alert / the terminal."""
    t = s["total"]
    title = f"Daily summary {s['day']} ({s['mode'].upper()}): {t['trades']} trades, P&L {_usd(t['pnl'])} after fees"
    lines = []
    for mod, e in s["modules"].items():
        line = (f"{MODULE_LABEL.get(mod, mod)}: {e['trades']} trades, P&L {_usd(e['pnl'])} after fees, win rate "
                f"{_pct(e['win_rate'])} (break-even {_pct(e.get('break_even_win_rate'))}), avg win {_usd(e['avg_win'])} "
                f"vs avg loss {_usd(e['avg_loss'])}, fees ${e['fees'] or 0:.2f}")
        cal = e.get("calibration") or {}
        if cal.get("n"):
            line += "\n  calibration: " + ", ".join(f"{b['bucket']} {b['actual']:.0%} won (n={b['n']})"
                                                for b in cal["buckets"])
        if mod == "sports":
            line += (f", CLV {e['clv_avg']:+.3f} avg ({e['clv_beat']}/{e['clv_n']} beat the close)"
                     if e.get("clv_avg") is not None else ", CLV - (no closing lines yet)")
        lines.append(line)
    lines.append(f"Total: win rate {_pct(t['win_rate'])}, avg win {_usd(t['avg_win'])} vs avg loss "
                 f"{_usd(t['avg_loss'])}, fees ${t['fees'] or 0:.2f}")
    return title, "\n".join(lines)

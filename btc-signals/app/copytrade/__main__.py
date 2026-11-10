"""Copy trading - pick who to copy.

    python -m app.copytrade candidates [--days 90] [--pool 150] [--backtest 25] [--top 10]
    python -m app.copytrade approve WALLET [WALLET ...]     # writes data/copy_traders.json
    python -m app.copytrade approved                        # show the approved list

`candidates` pulls the international monthly P&L leaderboards (overall + category boards), scores every trader on the last
--days days, drops market makers / high-frequency / one-lucky-bet / fast-crypto / 97c+ sweepers / < 50 trades, backtests copying each survivor
10 s and 30 s late (at the then-current traded price, +0.5c slippage, Polymarket US taker fee 0.095 p(1-p)),
and lists those still profitable at 30 s. Results are saved to data/copy_candidates.json.
Read-only: nothing here places orders.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from .intl import IntlData, resolution_payout
from .selection import backtest_copy, group_fills, score_trader

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ROOT / "data" / "copy_candidates.json"
APPROVED = ROOT / "data" / "copy_traders.json"
# categories with markets that Polymarket US also lists (crypto / weather / mentions are mostly fast or US-less)
CATEGORIES = ["sports", "esports", "politics", "economics", "finance", "tech", "culture"]


def _parse_ts(s):
    if not s:
        return None
    import calendar
    s = str(s).replace("Z", "").replace("T", " ").split(".")[0].split("+")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(s, fmt)))
        except ValueError:
            continue
    return None


class Pricer:
    """price_after / market_info for the backtest (cached)."""

    def __init__(self, api: IntlData):
        self.api = api
        self._px: dict[tuple, Optional[float]] = {}

    def price_after(self, cond, tok, t, side="BUY", wallet=None):
        key = (cond, str(tok), int(t), side)
        if key not in self._px:
            try:
                res = self.api.price_after(cond, tok, t, side=side, exclude_wallet=wallet)
            except Exception:  # noqa: BLE001
                res = None
            self._px[key] = None if res is None else res[1]
        return self._px[key]

    def market_info(self, cond, tok):
        try:
            m = self.api.market(cond)
        except Exception:  # noqa: BLE001
            m = None
        if not m:
            return {}
        return market_info_from(m, tok)


def market_info_from(m: dict, tok) -> dict:
    """Resolution time, payout and current price of a gamma market (pure). For sports, gamma's endDate is
    often the game START, so resolution is taken as kickoff + 3 h; closed markets use the real close time."""
    mark = None
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        if str(tok) in toks and len(prices) == len(toks):
            mark = prices[toks.index(str(tok))]
    except (ValueError, TypeError):
        pass
    end = _parse_ts(m.get("endDate"))
    if m.get("closed") and m.get("closedTime"):
        end = _parse_ts(m["closedTime"]) or end
    elif m.get("gameStartTime"):
        gs = _parse_ts(m["gameStartTime"])
        if gs is not None:
            end = max(end or 0.0, gs + 3 * 3600)
    return {"end_ts": end, "payout": resolution_payout(m, tok), "mark": mark}


def on_us(api: IntlData, cache: dict, event_slug: str) -> bool:
    """Is this international event also listed on Polymarket US (gamma events[].usId)? Sports/esports only:
    non-sports markets are matched market by market by the live copier."""
    if event_slug not in cache:
        try:
            ev = api.event(event_slug) if event_slug else None
        except Exception:  # noqa: BLE001
            ev = None
        cache[event_slug] = bool(ev and ev.get("usId"))
    return cache[event_slug]


def find_candidates(*, days: int = 90, recent_days: int = 30, pool: int = 150, n_backtest: int = 25,
                    top: int = 10, max_entries: int = 300, log=print) -> dict:
    """Out-of-sample by design: traders are drawn from the CURRENT monthly leaderboards, so eligibility and the
    keep/drop backtest use the EARLIER period [now-days, now-recent_days]; the recent month is reported only."""
    api = IntlData()
    now = time.time()
    since = now - days * 86400
    split = now - recent_days * 86400
    seen, people = set(), []
    boards = [("month", None, "PNL", pool)] + [("month", c, "PNL", max(20, pool // 3)) for c in CATEGORIES] + \
             [("month", None, "VOLUME", pool // 2)]
    for period, cat, sort_by, n in boards:
        try:
            rows = api.leaderboard(period=period, sort_by=sort_by, category=cat, n=n)
        except Exception as e:  # noqa: BLE001 - one board failing must not stop the search
            log(f"leaderboard {cat or 'overall'}/{sort_by}: {e}")
            rows = []
        for row in rows:
            w = (row.get("user_id") or "").lower()
            if w and w not in seen:
                seen.add(w)
                people.append({"wallet": w, "name": row.get("user_name") or w[:10], "lb_pnl": row.get("pnl"),
                               "lb_board": f"{cat or 'overall'}/{sort_by.lower()}"})
    log(f"{len(people)} traders from the monthly leaderboards (overall P&L + volume, {', '.join(CATEGORIES)})")
    scored = []
    for i, p in enumerate(people, 1):
        w = p["wallet"]
        try:
            fills, trunc = api.user_trades(w, start=int(since), max_rows=20000)
            finished = api.closed_positions(w, start=int(since)) + api.resolved_open_positions(w, start=int(since))
            pts = api.user_pnl(w, interval="all", fidelity="1d")
        except Exception as e:  # noqa: BLE001
            log(f"  [{i}/{len(people)}] {p['name']}: data error {e}")
            continue
        s = score_trader(wallet=w, name=p["name"], fills=fills, truncated=trunc, closed=finished, pnl_points=pts,
                         since=since, now=split)
        r = score_trader(wallet=w, name=p["name"], fills=fills, truncated=trunc, closed=finished, pnl_points=pts,
                         since=split, now=now, min_trades=0)
        s["recent"] = {k: r[k] for k in ("realized_pnl", "roi", "win_rate", "entries", "max_drawdown", "curve_pnl")}
        s["_all_entries"] = group_fills([f for f in fills if (f.get("timestamp") or 0) >= since])
        s["lb_board"] = p["lb_board"]
        scored.append(s)
        log(f"  [{i}/{len(people)}] {p['name'][:20]:20s} " + ("OK " if not s["excluded"] else "-- " + s["excluded"][0]))
    eligible = [s for s in scored if not s["excluded"]]
    eligible.sort(key=lambda s: (s["curve_pnl"] or s["realized_pnl"]) / max(s["cost"], 1.0) * min(1.0, s["entries"] / 200),
                  reverse=True)
    log(f"{len(eligible)} eligible after exclusions (scored on days {days}..{recent_days} ago); "
        f"backtesting the top {min(n_backtest, len(eligible))}")
    pricer, us_cache = Pricer(api), {}
    final = []
    for s in eligible[:n_backtest]:
        ents = s["_all_entries"]
        old_buys = [e for e in ents if e["side"] == "BUY" and e["t"] < split]
        start = old_buys[-max_entries]["t"] if len(old_buys) > max_entries else since
        kw = dict(price_after=pricer.price_after, market_info=pricer.market_info, wallet=s["wallet"], now=now)
        bt = backtest_copy(ents, copy_from=start, copy_to=split, **kw)
        new_buys = [e for e in ents if e["side"] == "BUY" and e["t"] >= split]
        start_r = new_buys[-max_entries]["t"] if len(new_buys) > max_entries else split
        bt_recent = backtest_copy(ents, copy_from=start_r, copy_to=now + 1, **kw)
        us_ents = [e for e in ents if on_us(api, us_cache, e.get("event_slug"))]
        bt_us = backtest_copy(us_ents, copy_from=start, copy_to=now + 1, **kw) if us_ents else None
        s["backtest"] = {str(k): v for k, v in bt.items()}
        s["backtest_recent"] = {str(k): v for k, v in bt_recent.items()}
        s["backtest_us_only"] = None if bt_us is None else {str(k): v for k, v in bt_us.items()}
        n_buys = len([e for e in ents if e["side"] == "BUY"])
        s["us_match"] = {"buys": n_buys, "on_us": len([e for e in us_ents if e["side"] == "BUY"])}
        b10, b30 = bt[10], bt[30]
        ok = (b10["pnl"] or 0) > 0 and (b30["pnl"] or 0) > 0 and b30["markets"] >= 20
        s["copy_ok"] = ok
        log(f"  backtest {s['name'][:20]:20s} 10s {b10['pnl']:+.2f} / 30s {b30['pnl']:+.2f} on {b30['copied']} copies "
            f"in {b30['markets']} markets (lower bound {b30['pnl_lb']}) | recent month 30s "
            f"{bt_recent[30]['pnl']:+.2f} -> {'KEEP' if ok else 'drop'}")
        final.append(s)
    keep = sorted([s for s in final if s["copy_ok"]], key=lambda s: s["backtest"]["30"]["pnl_lb"] or -1e9, reverse=True)
    for s in scored:
        s.pop("_entries", None)
        s.pop("_all_entries", None)
    res = {"generated": now, "days": days, "recent_days": recent_days, "api_calls": api.calls, "pool": len(people),
           "eligible": len(eligible), "backtested": len(final), "shortlist": keep[:top],
           "dropped_after_backtest": [s for s in final if not s["copy_ok"]],
           "excluded": [{"wallet": s["wallet"], "name": s["name"], "why": s["excluded"]} for s in scored if s["excluded"]]}
    CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text(json.dumps(res, indent=1, default=str))
    return res


def _pct(x):
    return "-" if x is None else f"{x:+.0%}" if abs(x) < 10 else f"{x:+.1f}x"


def format_shortlist(res: dict) -> str:
    rd = res.get("recent_days", 30)
    L = [f"Pool {res['pool']} traders -> {res['eligible']} eligible -> {res['backtested']} backtested -> "
         f"{len(res['shortlist'])} still profitable copied 10 s AND 30 s late", "",
         f"Scored and backtested on days {res['days']}..{rd} ago (they were picked from the current month's "
         f"leaderboard, so the recent month is shown separately as a check).", ""]
    hdr = (f"{'#':>2} {'trader':16s} {'wallet':10s} {'P&L':>10s} {'ROI':>6s} {'win':>4s} {'bets':>5s} {'maxDD':>9s} "
           f"{'wks+':>4s} | {'copy10s':>7s} {'copy30s':>7s} {'lowbnd':>7s} {'mkts':>4s} {'gap':>5s} | "
           f"{'recent':>7s} | {'onUS':>8s} {'US-only':>7s}")
    L.append(hdr)
    for i, s in enumerate(res["shortlist"], 1):
        b10, b30, rc = s["backtest"]["10"], s["backtest"]["30"], s["backtest_recent"]["30"]
        us = s.get("us_match") or {}
        bu = (s.get("backtest_us_only") or {}).get("30")
        L.append(f"{i:>2} {s['name'][:16]:16s} {s['wallet'][:10]:10s} {s['realized_pnl']:>10,.0f} {_pct(s['roi']):>6s} "
                 f"{(s['win_rate'] or 0):>4.0%} {s['entries']:>5d} {(s['max_drawdown'] or 0):>9,.0f} "
                 f"{(s['profitable_weeks'] or 0):>4.0%} | {b10['pnl']:>+7.2f} {b30['pnl']:>+7.2f} "
                 f"{(b30['pnl_lb'] if b30['pnl_lb'] is not None else 0):>+7.2f} {b30['markets']:>4d} "
                 f"{(b30['avg_copy_cost'] or 0) * 100:>4.1f}c | {rc['pnl']:>+7.2f} | "
                 f"{us.get('on_us', 0):>3d}/{us.get('buys', 0):<4d} {('-' if not bu else f'{bu['pnl']:+.2f}'):>7s}")
    L += ["",
          "P&L, ROI, win, bets, maxDD, wks+ = the trader's own record in the earlier period (finished positions, incl.",
          "  unredeemed losers; daily P&L curve). copy10s / copy30s = $ P&L of copying every trade with $1, 10 / 30 s late,",
          "  at the price a copier could get (+1c, US taker fee). lowbnd = copy30s minus one standard error across",
          "  markets (> 0 = less likely to be luck). gap = average price paid above theirs. recent = copy30s on the",
          f"  last {rd} days. onUS = their trades in events Polymarket US also lists (sports/esports via usId; other",
          "  markets get matched one by one when copying); US-only = copy30s on just those.",
          "Approve with: python -m app.copytrade approve WALLET [WALLET ...]"]
    return "\n".join(L)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.copytrade")
    ap.add_argument("command", choices=["candidates", "approve", "approved", "show"])
    ap.add_argument("wallets", nargs="*")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pool", type=int, default=150)
    ap.add_argument("--backtest", type=int, default=25)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max-entries", type=int, default=300)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    from ..console import safe_console
    safe_console()
    if a.command == "candidates":
        res = find_candidates(days=a.days, pool=a.pool, n_backtest=a.backtest, top=a.top, max_entries=a.max_entries)
        print("\n" + format_shortlist(res))
    elif a.command == "show":
        print(format_shortlist(json.loads(CANDIDATES.read_text())))
    elif a.command == "approve":
        if not a.wallets:
            sys.exit("give one or more wallets from the candidates list")
        cands = {s["wallet"]: s for s in json.loads(CANDIDATES.read_text())["shortlist"]} if CANDIDATES.exists() else {}
        cur = json.loads(APPROVED.read_text()) if APPROVED.exists() else {"traders": []}
        have = {t["wallet"] for t in cur["traders"]}
        for w in a.wallets:
            w = w.lower()
            match = [k for k in cands if k.startswith(w)]
            if len(match) != 1:
                sys.exit(f"{w}: not (uniquely) in the current candidate list - run candidates first")
            if match[0] not in have:
                c = cands[match[0]]
                cur["traders"].append({"wallet": match[0], "name": c["name"], "approved_ts": time.time(),
                                       "score": {k: c.get(k) for k in ("roi", "win_rate", "entries", "realized_pnl")},
                                       "backtest_30s": c["backtest"]["30"]})
        APPROVED.write_text(json.dumps(cur, indent=1, default=str))
        print(f"Approved: {', '.join(t['name'] for t in cur['traders'])}")
    else:
        cur = json.loads(APPROVED.read_text()) if APPROVED.exists() else {"traders": []}
        for t in cur["traders"]:
            print(f"{t['name']:20s} {t['wallet']}")
        if not cur["traders"]:
            print("No approved traders yet.")


if __name__ == "__main__":
    main()

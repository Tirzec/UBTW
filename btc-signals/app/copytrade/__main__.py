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

from .intl import IntlData, resolution_payout
from .selection import backtest_copy, score_trader

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
    """price_after / market_info for the backtest, with one market-trades request per (market, entry)."""

    def __init__(self, api: IntlData):
        self.api = api
        self._win: dict[tuple, list] = {}

    def price_after(self, cond, tok, t):
        key = (cond, str(tok), int(t))
        if key not in self._win:
            try:
                res = self.api.price_after(cond, tok, t)
            except Exception:  # noqa: BLE001
                res = None
            self._win[key] = None if res is None else res[1]
        return self._win[key]

    def market_info(self, cond, tok):
        try:
            m = self.api.market(cond)
        except Exception:  # noqa: BLE001
            m = None
        if not m:
            return {}
        mark = None
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            if str(tok) in toks and len(prices) == len(toks):
                mark = prices[toks.index(str(tok))]
        except (ValueError, TypeError):
            pass
        return {"end_ts": _parse_ts(m.get("endDate")), "payout": resolution_payout(m, tok), "mark": mark}


def us_match_share(api: IntlData, entries: list[dict], sample: int = 60) -> dict:
    """Share of a trader's recent entries in events Polymarket lists on Polymarket US too (gamma
    events[].usId - sports/esports; non-sports markets are matched later, market by market)."""
    slugs = []
    for e in reversed(entries):
        if e["side"] == "BUY" and e["event_slug"] and e["event_slug"] not in slugs:
            slugs.append(e["event_slug"])
        if len(slugs) >= sample:
            break
    hit = 0
    for s in slugs:
        try:
            ev = api.event(s)
        except Exception:  # noqa: BLE001
            ev = None
        if ev and ev.get("usId"):
            hit += 1
    return {"events_checked": len(slugs), "on_us": hit, "share": (hit / len(slugs)) if slugs else None}


def find_candidates(*, days: int = 90, pool: int = 150, n_backtest: int = 25, top: int = 10,
                    max_entries: int = 300, log=print) -> dict:
    api = IntlData()
    now = time.time()
    since = now - days * 86400
    seen, people = set(), []
    boards = [("month", None, pool)] + [("month", c, max(20, pool // 3)) for c in CATEGORIES]
    for period, cat, n in boards:
        for row in api.leaderboard(period=period, sort_by="PNL", category=cat, n=n):
            w = (row.get("user_id") or "").lower()
            if w and w not in seen:
                seen.add(w)
                people.append({"wallet": w, "name": row.get("user_name") or w[:10], "lb_pnl": row.get("pnl"),
                               "lb_board": cat or "overall"})
    log(f"{len(people)} traders from the monthly profit leaderboards (overall + {', '.join(CATEGORIES)})")
    scored = []
    for i, p in enumerate(people, 1):
        w = p["wallet"]
        try:
            fills, trunc = api.user_trades(w, start=int(since), max_rows=20000)
            closed = api.closed_positions(w, start=int(since))
            pts = api.user_pnl(w, interval="all", fidelity="1d")
        except Exception as e:  # noqa: BLE001
            log(f"  [{i}/{len(people)}] {p['name']}: data error {e}")
            continue
        s = score_trader(wallet=w, name=p["name"], fills=fills, truncated=trunc, closed=closed, pnl_points=pts,
                         since=since, now=now)
        scored.append(s)
        log(f"  [{i}/{len(people)}] {p['name'][:20]:20s} " + ("OK " if not s["excluded"] else "-- " + s["excluded"][0]))
    eligible = [s for s in scored if not s["excluded"]]
    eligible.sort(key=lambda s: ((s["roi"] or 0) * min(1.0, s["entries"] / 200)), reverse=True)
    log(f"{len(eligible)} eligible after exclusions; backtesting the top {min(n_backtest, len(eligible))}")
    pricer = Pricer(api)
    final = []
    for s in eligible[:n_backtest]:
        ents = s["_entries"][-max_entries * 2:]
        bt = backtest_copy(ents, price_after=pricer.price_after, market_info=pricer.market_info, now=now)
        s["backtest"] = {str(k): v for k, v in bt.items()}
        s["us_match"] = us_match_share(api, s["_entries"])
        b30 = bt[30]
        ok = (b30["pnl"] or 0) > 0 and b30["copied"] >= 20
        s["copy_ok"] = ok
        log(f"  backtest {s['name'][:20]:20s} 10s P&L {bt[10]['pnl']:+.2f} / 30s P&L {b30['pnl']:+.2f} "
            f"on {b30['copied']} copies -> {'KEEP' if ok else 'drop'}")
        final.append(s)
    keep = sorted([s for s in final if s["copy_ok"]], key=lambda s: s["backtest"]["30"]["roi"] or 0, reverse=True)
    for s in scored:
        s.pop("_entries", None)
    res = {"generated": now, "days": days, "api_calls": api.calls, "pool": len(people),
           "eligible": len(eligible), "backtested": len(final), "shortlist": keep[:top],
           "dropped_after_backtest": [s for s in final if not s["copy_ok"]],
           "excluded": [{"wallet": s["wallet"], "name": s["name"], "why": s["excluded"]} for s in scored if s["excluded"]]}
    CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text(json.dumps(res, indent=1, default=str))
    return res


def _pct(x):
    return "-" if x is None else f"{x:+.0%}" if abs(x) < 10 else f"{x:+.1f}x"


def format_shortlist(res: dict) -> str:
    L = [f"Pool {res['pool']} traders -> {res['eligible']} eligible -> {res['backtested']} backtested -> "
         f"{len(res['shortlist'])} still profitable copied 30 s late (last {res['days']} days)", ""]
    hdr = (f"{'#':>2} {'trader':18s} {'wallet':12s} {'P&L':>11s} {'ROI':>6s} {'win':>4s} {'bets':>5s} "
           f"{'maxDD':>9s} {'wks+':>5s} {'copy10s':>8s} {'copy30s':>8s} {'gap':>6s} {'onUS':>5s}")
    L.append(hdr)
    for i, s in enumerate(res["shortlist"], 1):
        b10, b30 = s["backtest"]["10"], s["backtest"]["30"]
        us = s.get("us_match") or {}
        L.append(f"{i:>2} {s['name'][:18]:18s} {s['wallet'][:12]:12s} {s['realized_pnl']:>11,.0f} {_pct(s['roi']):>6s} "
                 f"{(s['win_rate'] or 0):>4.0%} {s['entries']:>5d} {(s['max_drawdown'] or 0):>9,.0f} "
                 f"{(s['profitable_weeks'] or 0):>5.0%} {_pct(b10['roi']):>8s} {_pct(b30['roi']):>8s} "
                 f"{(b30['avg_copy_cost'] or 0) * 100:>5.1f}c {('-' if us.get('share') is None else f'{us['share']:.0%}'):>5s}")
    L += ["", "P&L / ROI / win / bets / maxDD / wks+ = their own closed positions and daily P&L curve in the window",
          "copy10s / copy30s = ROI of copying every trade $1 each, 10 s / 30 s late, after fees",
          "gap = average price I'd pay above theirs; onUS = share of their recent events also listed on Polymarket US",
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

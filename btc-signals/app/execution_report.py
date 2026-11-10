"""Execution diagnosis: how fast entries happen and fill, and how much profit early exits left on the table.

    python -m app.trader execution [--live] [--days 7]

ENTRIES (every fair-value entry attempt):
  signal -> order placed -> fill times; how many entry orders were cancelled unfilled (price moved away);
  signals that were blocked or missed (from the signal log, bot_signals, recorded since 2026-11-10).
  'signal' = when the bot decided (position created). The book-update time that triggered it is recorded
  from 2026-11-10 on (signal_book_ts), so 'data -> order' latency exists only for newer trades.
EXITS (every trade sold before the window ended at a profit):
  sale price vs. what it was worth at the end: the settlement payout ($1 if the side won, $0 if not), and the
  last bid before the close (bot trace, or Polymarket's ~1-minute price history shifted +60 s).
  Profit left on the table = (end value - sale price) x shares when positive.
GROUPS: trades are split by rules version (positions.rules; older trades = 'old rules').
"""
from __future__ import annotations

import time
from statistics import mean, median


def _pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[k]


OLD = "old rules"
NEW_LABEL = {"v2-2026-11-10": "new rules (v2, 2026-11-10)"}


def _rules(r) -> str:
    v = r.get("rules")
    return NEW_LABEL.get(v, v) if v else OLD


def entries(db, rows: list[dict]) -> dict:
    out = {}
    for key in sorted({_rules(r) for r in rows}):
        g = [r for r in rows if _rules(r) == key]
        lat_order, lat_fill, lat_data, slip = [], [], [], []
        filled = unfilled = partial = rejected = other_void = 0
        for r in g:
            o = db.query("SELECT * FROM bot_orders WHERE order_id=? ORDER BY id LIMIT 1", (r["entry_order"],))
            o = o[0] if o else None
            if o and r.get("opened_ts"):
                lat_order.append((o["ts"] - r["opened_ts"]) * 1000)
            if r.get("signal_book_ts") and (r.get("order_sent_ts") or o):
                lat_data.append(((r.get("order_sent_ts") or o["ts"]) - r["signal_book_ts"]) * 1000)
            if r["status"] == "void":
                why = r.get("exit_reason") or ""
                if why == "entry not filled in time":
                    unfilled += 1
                elif why.startswith("entry rejected"):
                    rejected += 1
                else:
                    other_void += 1
                continue
            filled += 1
            if o and o["filled"] is not None and o["size"] and o["filled"] < o["size"] - 1e-9:
                partial += 1
            t_fill = r.get("first_fill_ts") or r.get("filled_ts")
            t_ord = r.get("order_sent_ts") or (o["ts"] if o else None)
            if t_fill and t_ord:
                lat_fill.append((t_fill - t_ord) * 1000)
            if r.get("signal_ask") and r.get("entry_price"):
                slip.append(r["entry_price"] - r["signal_ask"])
        out[key] = {"attempts": len(g), "filled": filled, "unfilled_cancelled": unfilled, "partial": partial,
                    "rejected": rejected, "void_other": other_void,
                    "signal_to_order_ms": _summ(lat_order), "data_to_order_ms": _summ(lat_data),
                    "order_to_fill_ms": _summ(lat_fill),
                    "fill_vs_signal_ask": (mean(slip) if slip else None),
                    "signals": _signals(db, g, key)}
    return out


def _signals(db, g: list[dict], key: str) -> dict | None:
    """Windows where the model saw an entry edge but no position was filled, and why (signal log)."""
    if key == OLD or not g or not _has_table(db, "bot_signals"):
        return None
    ver = g[0].get("rules")
    since = min(r["opened_ts"] for r in g) - 3600
    rows = db.query("SELECT slug, decision, reason FROM bot_signals WHERE mode=? AND rules=? AND ts>=?",
                    (g[0]["mode"], ver, since))
    if not rows:
        return None
    filled = {r["slug"] for r in g if r["status"] != "void"}
    windows = {r["slug"] for r in rows}
    missed = windows - filled
    why: dict[str, int] = {}
    for r in rows:
        if r["slug"] in missed and r["decision"] == "skipped":
            k = (r["reason"] or "")[:60]
            why[k] = why.get(k, 0) + 1
    return {"windows_with_signal": len(windows), "windows_filled": len(windows & filled),
            "windows_missed": len(missed), "top_reasons": sorted(why.items(), key=lambda x: -x[1])[:5]}


def _has_table(db, name: str) -> bool:
    return bool(db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _summ(xs):
    if not xs:
        return None
    return {"n": len(xs), "median": median(xs), "p90": _pct(xs, 0.9), "max": max(xs)}


def _end_value(db, r, client=None, history_cache=None):
    """(payout per share at settlement or None, last bid before the close or None)."""
    payout = None if r.get("side_won") is None else float(r["side_won"])
    from .exit_report import _window
    _, start, end = _window(r["slug"])
    last_bid = None
    if end:
        t = db.query("SELECT bid FROM bot_ticks WHERE position_id=? AND ts<=? AND bid IS NOT NULL "
                     "ORDER BY ts DESC LIMIT 1", (r["id"], end)) if _has_table(db, "bot_ticks") else []
        if t and r.get("closed_ts") and r["closed_ts"] >= end - 5:
            last_bid = t[0]["bid"]
        elif client is not None and start:
            key = r["slug"]
            if history_cache is not None and key in history_cache:
                hist = history_cache[key]
            else:
                from .exit_report import history_bids
                try:
                    hist = history_bids(client, r["slug"], r["side"], start, end)
                except Exception:  # noqa: BLE001
                    hist = []
                if history_cache is not None:
                    history_cache[key] = hist
            before = [b for ts, b in hist if ts <= end - 1]
            last_bid = before[-1] if before else None
    return payout, last_bid


def exits(db, rows: list[dict], client=None) -> dict:
    out, cache = {}, {}
    for key in sorted({_rules(r) for r in rows}):
        g = [r for r in rows if _rules(r) == key and r["status"] == "closed" and (r["pnl"] or 0) > 0
             and r.get("exit_shares") and r.get("exit_trigger") not in (None, "resolution")]
        items = []
        for r in g:
            payout, last_bid = _end_value(db, r, client, cache)
            sold = (r["exit_proceeds"] or 0) / r["exit_shares"] if r["exit_shares"] else None
            end_val = payout if payout is not None else last_bid
            left = None if end_val is None or sold is None else (end_val - sold) * r["exit_shares"]
            items.append({"id": r["id"], "slug": r["slug"], "side": r["side"], "entry": r["entry_price"],
                          "sold": sold, "shares": r["exit_shares"], "trigger": r["exit_trigger"],
                          "payout": payout, "last_bid": last_bid, "left": left, "pnl": r["pnl"]})
        lefts = [i["left"] for i in items if i["left"] is not None]
        out[key] = {"profitable_early_sales": len(items), "items": items,
                    "avg_left_on_table": mean([max(0.0, x) for x in lefts]) if lefts else None,
                    "avg_net_vs_holding": mean(lefts) if lefts else None,
                    "would_have_lost_by_holding": sum(1 for x in lefts if x < 0)}
    return out


def results(rows: list[dict]) -> dict:
    out = {}
    for key in sorted({_rules(r) for r in rows}):
        g = [r for r in rows if _rules(r) == key and r["status"] == "closed" and r["pnl"] is not None]
        wins = [r["pnl"] for r in g if r["pnl"] > 0]
        losses = [r["pnl"] for r in g if r["pnl"] <= 0]
        out[key] = {"trades": len(g), "win_rate": len(wins) / len(g) if g else None,
                    "avg_win": mean(wins) if wins else None, "avg_loss": mean(losses) if losses else None,
                    "pnl": sum(r["pnl"] for r in g),
                    "cost": sum((r["cost"] or 0) + (r["entry_fee"] or 0) for r in g)}
    return out


def analyse(db, *, mode: str, days: float = 7, client=None) -> dict:
    since = time.time() - days * 86400
    rows = [dict(r) for r in db.query(
        "SELECT * FROM bot_positions WHERE mode=? AND opened_ts>=? AND COALESCE(strategy,'')='fair_value' "
        "AND COALESCE(venue,'polymarket_intl')='polymarket_us' ORDER BY id", (mode, since))]
    return {"n": len(rows), "entries": entries(db, rows), "exits": exits(db, rows, client), "results": results(rows)}


def _f(x, fmt="{:+.2f}"):
    return "-" if x is None else fmt.format(x)


def _ms(s):
    return "-" if not s else f"median {s['median']:.0f} ms, 90% under {s['p90']:.0f} ms, max {s['max']:.0f} ms (n={s['n']})"


def format_report(a: dict) -> str:
    L = [f"{a['n']} fair-value entry attempts", "", "=== OLD vs NEW RULES ===", compare_text(a), "",
         "=== ENTRIES ==="]
    for key, e in a["entries"].items():
        rested = e["attempts"] - e["rejected"] - e["void_other"]
        L.append(f"[{key}] attempts {e['attempts']}: filled {e['filled']}, cancelled unfilled {e['unfilled_cancelled']}"
                 + (f" ({e['unfilled_cancelled'] / rested:.0%} of orders placed - price moved away before filling)"
                    if rested > 0 else "")
                 + (f", rejected by the exchange {e['rejected']}" if e["rejected"] else "")
                 + (f", lost on restart {e['void_other']}" if e["void_other"] else "")
                 if e["attempts"] else f"[{key}] no attempts")
        L.append(f"   signal -> order placed: {_ms(e['signal_to_order_ms'])}")
        L.append(f"   book update -> order placed: {_ms(e['data_to_order_ms'])}"
                 + ("" if e["data_to_order_ms"] else "  (recorded from 2026-11-10 on)"))
        L.append(f"   order -> first fill seen: {_ms(e['order_to_fill_ms'])} (resting orders: checked every 250 ms; "
                 f"older trades: when the fill was booked); partial fills {e['partial']}")
        if e["fill_vs_signal_ask"] is not None:
            L.append(f"   fill price vs ask at the signal: {e['fill_vs_signal_ask']:+.3f} on average")
        sg = e["signals"]
        if sg:
            L.append(f"   windows with an entry signal {sg['windows_with_signal']}: filled {sg['windows_filled']}, "
                     f"missed {sg['windows_missed']}")
            for reason, n in sg["top_reasons"]:
                L.append(f"      skipped {n}x: {reason}")
        else:
            L.append("   skipped/missed signals: not logged for these trades (signal log starts with the new rules)")
    L += ["", "=== EXITS: trades sold early at a profit ==="]
    for key, x in a["exits"].items():
        L.append(f"[{key}] {x['profitable_early_sales']} profitable early sales")
        for i in x["items"]:
            end = (f"settled {'WON ($1)' if i['payout'] == 1 else 'LOST ($0)'}" if i["payout"] is not None
                   else f"last bid {_f(i['last_bid'], '{:.2f}')}")
            L.append(f"   #{i['id']} {i['side']} entry {_f(i['entry'], '{:.2f}')} sold {_f(i['sold'], '{:.2f}')} "
                     f"({i['trigger']}) -> {end}; left on table {_f(i['left'])}")
        L.append(f"   average profit left on the table: {_f(x['avg_left_on_table'])} per trade; net vs holding "
                 f"{_f(x['avg_net_vs_holding'])} per trade; {x['would_have_lost_by_holding']} would have LOST by holding")
    L += ["", "=== RESULTS ==="]
    for key, r in a["results"].items():
        roi = (r["pnl"] / r["cost"]) if r["cost"] else None
        L.append(f"[{key}] {r['trades']} closed trades, win rate {_f(r['win_rate'], '{:.0%}')}, avg win {_f(r['avg_win'])}, "
                 f"avg loss {_f(r['avg_loss'])}, P&L after fees {_f(r['pnl'])} ({_f(roi, '{:+.0%}')} of money in)")
    return "\n".join(L)


def compare_text(a: dict) -> str:
    """Old rules vs new rules, one block per measure (phone alert / report header)."""
    keys = list(a["results"])
    L = []
    for key in keys:
        r = a["results"][key]
        x = a["exits"].get(key, {})
        e = a["entries"].get(key, {})
        missed = e.get("unfilled_cancelled", 0)
        sg = e.get("signals")
        miss_txt = f"{missed} entry orders cancelled unfilled" + (
            f", {sg['windows_missed']} of {sg['windows_with_signal']} signal windows not traded" if sg else "")
        L.append(f"{key}: {r['trades']} trades | avg win {_f(r['avg_win'])} | avg loss {_f(r['avg_loss'])} | "
                 f"left on table {_f(x.get('avg_left_on_table'))}/early sale | missed: {miss_txt} | "
                 f"P&L after fees {_f(r['pnl'])}")
    return "\n".join(L) if L else "no trades yet"

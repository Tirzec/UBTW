"""Exit diagnosis: positions whose bid reached 90c+ - when, what the bot decided, how fast it sold.

    python -m app.trader exits [--days 3] [--live]

For each position:
  * first time its bid crossed the threshold, the max bid, and the sell order time (delay between them)
  * what the hold/sell rule said at that moment (from the per-position trace, bot_ticks), or the exit
    reason; latency events (price -> order -> acknowledgement); order errors
  * the bid over the final seconds of the window
Positions from before the trace existed use Polymarket's ~1-minute price history (/v1/price-history:
UP bid = 1 - shortPrice, DOWN bid = 1 - longPrice), so their timing is only accurate to about a minute.
"""
from __future__ import annotations

import json
import time

import httpx

GATEWAY = "https://gateway.polymarket.us"
DUR = {"5m": 300, "15m": 900}


def _hms(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"


def _window(slug: str):
    import calendar
    try:
        dur = slug.split("-")[3]
        stamp = slug.rsplit("-", 4)[-4:]
        t = time.strptime("-".join(stamp).rstrip("z"), "%Y-%m-%d-%H%M")
        start = calendar.timegm(t)
        return dur, start, start + DUR[dur]
    except (IndexError, KeyError, ValueError):
        return None, None, None


def history_bids(client, slug: str, side: str, start: int, end: int) -> list[tuple[float, float]]:
    r = client.get(f"{GATEWAY}/v1/price-history", params={"symbol": slug, "timestamp.startTimestamp": start,
                                                          "timestamp.endTimestamp": end + 60, "fidelity": 1})
    if r.status_code != 200:
        return []
    out = []
    for h in r.json().get("history") or []:
        lp, sp = h.get("longPrice"), h.get("shortPrice")
        if lp is None or sp is None:
            continue
        out.append((float(h["timestamp"]), round(1 - float(sp) if side == "UP" else 1 - float(lp), 4)))
    return out


def analyse(db, *, mode: str, days: float = 3, threshold: float = 0.90, client=None) -> list[dict]:
    since = time.time() - days * 86400
    rows = db.query("SELECT * FROM bot_positions WHERE mode=? AND opened_ts>=? AND status!='void' "
                    "AND COALESCE(venue,'polymarket_intl')='polymarket_us' "
                    "AND COALESCE(module,'crypto_updown')='crypto_updown' ORDER BY id", (mode, since))
    client = client or httpx.Client(timeout=15)
    out = []
    for p in rows:
        dur, start, end = _window(p["slug"])
        ticks = db.query("SELECT * FROM bot_ticks WHERE position_id=? ORDER BY ts", (p["id"],))
        events = db.query("SELECT ts, kind, message, data FROM bot_events WHERE data LIKE ? ORDER BY ts",
                          (f'%"position": {p["id"]}%',))
        sells = [e for e in events if e["kind"] == "order" and e["message"].startswith("SELL")]
        lock_orders = [e for e in events if e["kind"] == "order" and "profit lock" in e["message"]]
        lat = [json.loads(e["data"] or "{}") for e in events if e["kind"] == "exit_latency"]
        errors = [e["message"] for e in db.query(
            "SELECT message FROM bot_events WHERE kind='api_error' AND ts BETWEEN ? AND ?",
            (p["opened_ts"] or 0, (p["closed_ts"] or time.time()) + 5))]
        if ticks:
            src = "bot trace"
            path = [(t["ts"], t["bid"]) for t in ticks if t["bid"] is not None]
        elif start:
            src = "price history (~1 min)"
            path = [(ts, b) for ts, b in history_bids(client, p["slug"], p["side"], start, end)
                    if (p["filled_ts"] or p["opened_ts"] or 0) - 60 <= ts <= (p["closed_ts"] or end) + 5]
        else:
            src, path = "none", []
        cross = next(((ts, b) for ts, b in path if b >= threshold), None)
        max_bid = max((b for _, b in path), default=None)
        decision_at_cross = None
        if cross and ticks:
            t = next((t for t in ticks if t["ts"] >= cross[0] - 1e-6), None)
            if t:
                decision_at_cross = f"{t['decision']} ({t['trigger'] or ''}, fair {t['fair'] or 0:.3f})"
        sell_ts = sells[0]["ts"] if sells else None
        final = [(round(end - ts), b) for ts, b in path if end and end - 15 <= ts <= end] if end else []
        out.append({
            "id": p["id"], "slug": p["slug"], "side": p["side"], "entry": p["entry_price"],
            "exit_price": p["exit_price"], "pnl": p["pnl"], "won": p["side_won"], "trigger": p["exit_trigger"],
            "reason": p["exit_reason"], "status": p["status"], "source": src, "max_bid": max_bid,
            "crossed": cross, "sell_ts": sell_ts,
            "delay_s": (sell_ts - cross[0]) if (cross and sell_ts) else None,
            "decision_at_cross": decision_at_cross, "latency": lat[:3], "lock_orders": len(lock_orders),
            "errors": errors[:3], "final_seconds": final[-15:], "window_end": end})
    return out


def format_report(items: list[dict], threshold: float = 0.90) -> str:
    hit = [i for i in items if i["max_bid"] is not None and i["max_bid"] >= threshold]
    lines = [f"{len(items)} positions checked, {len(hit)} reached a bid of {threshold:.0%}+", ""]
    for i in hit:
        c = i["crossed"]
        lines.append(f"#{i['id']} {i['slug']} {i['side']}  entry {i['entry'] or 0:.2f}  max bid {i['max_bid']:.2f}  "
                     f"[{i['source']}]")
        lines.append(f"   bid first >= {threshold:.2f} at {_hms(c[0])} ({(i['window_end'] or c[0]) - c[0]:.0f}s before close)")
        if i["sell_ts"]:
            lines.append(f"   sell order at {_hms(i['sell_ts'])}: {i['delay_s']:+.1f}s after crossing  "
                         f"-> {i['trigger']}: {i['reason'] or ''}")
        else:
            lines.append(f"   NO sell order: {i['trigger'] or 'held'} ({i['reason'] or i['status']})")
        if i["decision_at_cross"]:
            lines.append(f"   rule at that moment: {i['decision_at_cross']}")
        for lt in i["latency"]:
            lines.append(f"   latency: price->order {lt.get('price_to_order_ms')} ms, ack {lt.get('ack_ms')} ms")
        if i["errors"]:
            lines.append("   order/API errors meanwhile: " + "; ".join(e[:80] for e in i["errors"]))
        if i["final_seconds"]:
            lines.append("   final seconds (s before close: bid): " +
                         ", ".join(f"{s}s:{b:.2f}" for s, b in i["final_seconds"]))
        res = "won" if i["won"] == 1 else "lost" if i["won"] == 0 else "?"
        lines.append(f"   outcome: side {res} at settlement; P&L {i['pnl'] if i['pnl'] is not None else 0:+.2f}")
        lines.append("")
    if not hit:
        lines.append("No position reached that bid in this period.")
    return "\n".join(lines)

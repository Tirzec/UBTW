"""Entry diagnosis: why the bot bought what it bought, and how cheap vs. expensive entries did.

    python -m app.trader entries [--live] [--days 7] [--below 0.30] [--above 0.50]

For every position entered below `--below` (default 30c): the model's true probability at entry (p_model),
the edge it saw, time left, the reasons it logged, and which entry rules it passed with the settings that
were in force (price band, edge threshold, first-third rule). Then win rate / P&L for entries below
`--below` vs. above `--above`, plus how often the bought side actually won at settlement (calibration).
"""
from __future__ import annotations

import json
import time

DUR = {"5m": 300, "15m": 900}


def _dur(slug: str):
    for d in DUR:
        if f"-{d}-" in slug:
            return d
    return None


def _group(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["status"] == "closed" and r["pnl"] is not None]
    wins = [r["pnl"] for r in closed if r["pnl"] > 0]
    losses = [r["pnl"] for r in closed if r["pnl"] <= 0]
    settled = [r for r in rows if r.get("side_won") is not None]
    ps = [r["p_model"] for r in settled if r.get("p_model") is not None]
    aw = sum(wins) / len(wins) if wins else None
    al = sum(losses) / len(losses) if losses else None
    return {"n": len(rows), "closed": len(closed), "win_rate": len(wins) / len(closed) if closed else None,
            "avg_win": aw, "avg_loss": al,
            "break_even": (abs(al) / (aw + abs(al))) if (aw and al is not None and aw + abs(al) > 0) else None,
            "pnl": sum(r["pnl"] for r in closed), "cost": sum((r["cost"] or 0) + (r["entry_fee"] or 0) for r in closed),
            "side_won_rate": (sum(r["side_won"] for r in settled) / len(settled)) if settled else None,
            "avg_p_model": (sum(ps) / len(ps)) if ps else None, "settled": len(settled),
            "avg_entry": (sum(r["entry_price"] or 0 for r in rows) / len(rows)) if rows else None}


def backfill_outcomes(db, rows: list[dict], client=None) -> int:
    """Positions from before outcomes were recorded: ask the exchange who won (and store it)."""
    from .exit_report import _window
    from .ingest.us_markets import nocache
    todo = [r for r in rows if r.get("side_won") is None]
    if not todo:
        return 0
    import httpx
    client = client or httpx.Client(timeout=15)
    n, seen = 0, {}
    for r in todo:
        _, _, end = _window(r["slug"])
        if not end or end > time.time() - 30:
            continue
        if r["slug"] not in seen:
            try:
                m = client.get(f"https://gateway.polymarket.us/v1/market/slug/{r['slug']}", params=nocache())
                t = (m.json().get("market") or {}).get("assetPriceTerms") or {} if m.status_code == 200 else {}
                ptb, fin = t.get("priceToBeat"), t.get("settlementPrice")
                seen[r["slug"]] = (None if not (ptb and fin) else
                                   ("UP" if float(fin["value"]) >= float(ptb["value"]) else "DOWN"))
            except Exception:  # noqa: BLE001 - leave it unknown
                seen[r["slug"]] = None
        outcome = seen[r["slug"]]
        if outcome:
            r["side_won"] = int(r["side"] == outcome)
            db.execute("UPDATE bot_positions SET side_won=? WHERE id=?", (r["side_won"], r["id"]))
            n += 1
    return n


def analyse(db, *, mode: str, days: float = 7, below: float = 0.30, above: float = 0.50,
            cfg: dict | None = None, fetch_outcomes: bool = False, client=None) -> dict:
    since = time.time() - days * 86400
    rows = [dict(r) for r in db.query(
        "SELECT * FROM bot_positions WHERE mode=? AND opened_ts>=? AND status!='void' "
        "AND COALESCE(venue,'polymarket_intl')='polymarket_us' "
        "AND COALESCE(module,'crypto_updown')='crypto_updown' ORDER BY id", (mode, since))]
    if fetch_outcomes:
        backfill_outcomes(db, rows, client)
    fv = (cfg or {}).get("fair_value", {}) or {}
    thr = float(fv.get("min_edge", 0.05))
    mult = float(fv.get("five_min_multiplier", 2.0))
    cheap = []
    for r in rows:
        if r["entry_price"] is None or r["entry_price"] >= below:
            continue
        dur = _dur(r["slug"])
        L = DUR.get(dur or "", None)
        try:
            reasons = [x.get("text", "") for x in json.loads(r.get("reasons") or "[]")]
        except ValueError:
            reasons = []
        need = thr * (mult if dur == "5m" else 1.0)
        checks = []
        if r.get("strategy") == "fair_value":
            checks.append(f"price {r['entry_price']:.2f} inside the band then in force (5c-95c)")
            if r.get("edge") is not None:
                checks.append(f"edge {r['edge']:+.3f} >= needed {need:.2f} ({dur})" if r["edge"] >= need - 1e-9
                              else f"edge {r['edge']:+.3f} BELOW needed {need:.2f} (should not have passed)")
            if L and r.get("time_left_s") is not None:
                elapsed = L - r["time_left_s"]
                checks.append(f"{elapsed:.0f}s into the window (entries allowed after {L / 3:.0f}s)")
        else:
            checks.append("entered by the OLD strategy (indicator model, no price band)")
        cheap.append({**{k: r.get(k) for k in ("id", "slug", "side", "entry_price", "shares", "cost", "p_model", "edge",
                                                "time_left_s", "status", "pnl", "exit_trigger", "exit_reason",
                                                "side_won", "strategy", "opened_ts", "closed_ts")},
                      "duration": dur, "reasons": reasons[:3], "checks": checks})
    lo = [r for r in rows if r["entry_price"] is not None and r["entry_price"] < below]
    mid = [r for r in rows if r["entry_price"] is not None and below <= r["entry_price"] <= above]
    hi = [r for r in rows if r["entry_price"] is not None and r["entry_price"] > above]
    from .exit_report import _window
    by_settle: dict = {}
    for r in rows:
        _, _, end = _window(r["slug"])
        if end:
            by_settle.setdefault(end, []).append(r)
    doubled = {k: v for k, v in by_settle.items() if len(v) > 1}
    return {"rows": len(rows), "cheap": cheap, "below": _group(lo), "middle": _group(mid), "above": _group(hi),
            "thresholds": (below, above), "doubled": doubled}


def _f(x, fmt="{:+.2f}"):
    return "-" if x is None else fmt.format(x)


def format_report(a: dict) -> str:
    below, above = a["thresholds"]
    L = [f"{a['rows']} positions checked; {len(a['cheap'])} entered below {below * 100:.0f}c", ""]
    for c in a["cheap"]:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["opened_ts"])) if c["opened_ts"] else "?"
        won = {1: "WON", 0: "lost", None: "not settled"}[c["side_won"]]
        L.append(f"#{c['id']} {when}  {c['slug']}  {c['side']} @ {c['entry_price']:.2f}  "
                 f"({c['strategy'] or 'old strategy'})")
        L.append(f"   true probability at entry {_f(c['p_model'], '{:.1%}')}, edge {_f(c['edge'], '{:+.3f}')}, "
                 f"time left {_f(c['time_left_s'], '{:.0f}s')}")
        for ch in c["checks"]:
            L.append(f"   passed: {ch}")
        for rs in c["reasons"]:
            L.append(f"   logged reason: {rs}")
        L.append(f"   result: side {won}; exit {c['exit_trigger'] or '-'} ({(c['exit_reason'] or '')[:70]}); "
                 f"P&L {_f(c['pnl'])}")
        L.append("")
    L.append(f"{'entry price':<16}{'trades':>7}{'win rate':>10}{'break-even':>12}{'avg win':>9}{'avg loss':>9}"
             f"{'P&L':>8}{'P&L/$':>8}{'side won':>10}{'model said':>12}")
    for label, g in ((f"below {below * 100:.0f}c", a["below"]), (f"{below * 100:.0f}-{above * 100:.0f}c", a["middle"]),
                     (f"above {above * 100:.0f}c", a["above"])):
        roi = (g["pnl"] / g["cost"]) if g["cost"] else None
        L.append(f"{label:<16}{g['n']:>7}{_f(g['win_rate'], '{:.0%}'):>10}{_f(g['break_even'], '{:.0%}'):>12}"
                 f"{_f(g['avg_win']):>9}{_f(g['avg_loss']):>9}{_f(g['pnl']):>8}{_f(roi, '{:+.0%}'):>8}"
                 f"{_f(g['side_won_rate'], '{:.0%}'):>10}{_f(g['avg_p_model'], '{:.0%}'):>12}")
    L.append("('side won' = how often the bought side won at settlement; 'model said' = average true probability"
             " the model gave those sides at entry. A big gap means the model overestimated them.)")
    if a["doubled"]:
        L.append("")
        L.append("Positions settling at the same time (doubled-up bets):")
        for end, rs in sorted(a["doubled"].items()):
            L.append(f"   {time.strftime('%Y-%m-%d %H:%M', time.localtime(end))}: " + "; ".join(
                f"#{r['id']} {r['slug'].split('-')[3]} {r['side']} @ {r['entry_price']:.2f} -> {_f(r['pnl'])}" for r in rs))
    return "\n".join(L)

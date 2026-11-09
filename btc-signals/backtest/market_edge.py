"""Does the settlement model beat Polymarket's own prices? (test day, ~1-minute market snapshots)

    python -m backtest.market_edge --data DIR

At each market snapshot after the first third of a window: model fair P(UP) (only data up to that
second) vs the market mid; plus the PART 2 entry rule (fair - ask - taker fee >= threshold; 5m needs
2x; ask within [0.05, 0.95]; first signal per window; buy at the ask; hold to resolution).
Display prices are ~1-minute book snapshots, so this is an approximation of what was executable.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from app.fees import fee_per_share, fee_usdc
from app.settlement import FairEstimator
from backtest.calibrate_fair import load

SCHED = {"rate": 0.095, "exponent": 1, "takerOnly": True, "round_cents": True}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--min-edge", type=float, default=0.05)
    a = ap.parse_args(argv)
    d = Path(a.data)
    X, S = load(d)
    rep = json.load(open(d / "report/calibration.json"))
    hist = json.load(open(d / "market_hist.json"))
    snaps = {}
    for slug, m in hist.items():
        for h in m["h"]:
            t = int(h["timestamp"])
            if t < m["start"] + (m["end"] - m["start"]) / 3 or t >= m["end"] - 5:
                continue
            if h.get("longPrice") is None or h.get("shortPrice") is None:
                continue
            snaps.setdefault(t, []).append((slug, m, float(h["longPrice"]), float(h["shortPrice"])))
    est = FairEstimator(**rep["best_settings"])
    lo, hi = min(snaps) - 2000, max(snaps) + 1
    q, ssum = [], 0.0
    pairs, trades, taken = [], [], set()
    for sec in range(lo, hi):
        s = S.get(sec)
        q.append(s)
        ssum += s or 0
        if len(q) > 60:
            ssum -= q.pop(0) or 0
        if len(q) == 60 and None not in q:
            est.update_basis(sec, X.get(sec), ssum / 60)
        for slug, m, ask_up, ask_dn in snaps.get(sec, []):
            end, start = m["end"], m["start"]
            ptb, fin = X.get(start - 1), X.get(end - 1)
            if ptb is None or fin is None:
                continue
            fv = est.compute(now=sec, end=end, price_to_beat=ptb, x_at=X.get, spot_at=S.get, x_latest_sec=sec - 1)
            if fv is None:
                continue
            y = int(fin >= ptb)
            bid_up = 1 - ask_dn
            mid = min(max((ask_up + bid_up) / 2, 0.001), 0.999)
            pairs.append((m["dur"], fv.p_up, mid, y))
            if slug in taken:
                continue
            need = a.min_edge * (2 if m["dur"] == "5m" else 1)
            for side, ask, p in (("UP", ask_up, fv.p_up), ("DOWN", ask_dn, 1 - fv.p_up)):
                if not 0.05 <= ask <= 0.95:
                    continue
                edge = p - ask - fee_per_share(ask, SCHED)
                if edge >= need:
                    won = (y == 1) == (side == "UP")
                    pnl = (1.0 if won else 0.0) - ask - fee_usdc(1, ask, SCHED)
                    trades.append({"dur": m["dur"], "side": side, "ask": ask, "fair": p, "edge": edge,
                                   "won": won, "pnl": pnl, "left": end - sec})
                    taken.add(slug)
                    break

    def brier(i):
        return sum((r[i] - r[3]) ** 2 for r in pairs) / len(pairs)
    out = {"snapshots": len(pairs), "brier_model": brier(1), "brier_market_mid": brier(2)}
    for dur in ("15m", "5m"):
        sub = [r for r in pairs if r[0] == dur]
        if sub:
            out[f"brier_{dur}"] = {"model": sum((r[1] - r[3]) ** 2 for r in sub) / len(sub),
                                   "market": sum((r[2] - r[3]) ** 2 for r in sub) / len(sub), "n": len(sub)}
    for dur in ("15m", "5m", "all"):
        t = [x for x in trades if dur == "all" or x["dur"] == dur]
        if not t:
            out[f"trades_{dur}"] = {"n": 0}
            continue
        wins = [x["pnl"] for x in t if x["pnl"] > 0]
        losses = [x["pnl"] for x in t if x["pnl"] <= 0]
        aw = sum(wins) / len(wins) if wins else 0.0
        al = sum(losses) / len(losses) if losses else 0.0
        out[f"trades_{dur}"] = {"n": len(t), "win_rate": len(wins) / len(t), "avg_win": aw, "avg_loss": al,
                                "break_even_win_rate": abs(al) / (aw + abs(al)) if (aw + abs(al)) else None,
                                "pnl_per_contract_total": sum(x["pnl"] for x in t),
                                "pnl_per_dollar": sum(x["pnl"] for x in t) / sum(x["ask"] for x in t),
                                "avg_edge_claimed": sum(x["edge"] for x in t) / len(t)}
    (d / "report/market_edge.json").write_text(json.dumps({"summary": out, "trades": trades}, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

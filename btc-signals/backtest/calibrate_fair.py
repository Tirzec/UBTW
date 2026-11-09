"""Calibration of the settlement-math model (app/settlement.FairEstimator) on recorded data.

    python -m backtest.calibrate_fair --data DIR [--out reports/calibration]

DIR holds brti.json ({"prices": {sec: X}} - the public BRTI series, which IS the 60 s average)
and spot.json ({sec: price} 1-second spot, raw-price proxy). Outcome of each window is exact:
UP iff X(end-1) >= X(start-1) (verified equal to the official values).

Model settings are chosen on the first part of the data (train) and reported on the rest (test).
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from app.settlement import FairEstimator

BUCKETS = [(i / 10, (i + 1) / 10) for i in range(10)]


def load(data: Path):
    X = {int(k): float(v) for k, v in json.load(open(data / "brti.json"))["prices"].items()}
    S = {int(k): float(v) for k, v in json.load(open(data / "spot.json")).items()}
    return X, S


def run(X, S, *, t_from, t_to, est_kw, every=10, naive=False):
    """Replay seconds t_from..t_to; evaluate open windows every `every` s after their first third."""
    est = FairEstimator(**est_kw)
    spot_sum, q = 0.0, []
    out = []
    for sec in range(t_from - 120, t_to):
        s = S.get(sec)
        q.append(s)
        spot_sum += s or 0.0
        if len(q) > 60:
            old = q.pop(0)
            spot_sum -= old or 0.0
        if len(q) == 60 and None not in q:
            est.update_basis(sec, X.get(sec), spot_sum / 60)
        if sec < t_from or sec % every:
            continue
        for dur, L in (("5m", 300), ("15m", 900)):
            start = sec - sec % L
            end = start + L
            if sec < start + L / 3 or sec >= end:
                continue
            ptb, fin = X.get(start - 1), X.get(end - 1)
            if ptb is None or fin is None:
                continue
            if naive:   # old approach: treat the lagging average as the current price
                class _N:
                    pass
                fv = est.compute(now=sec, end=end, price_to_beat=ptb, x_at=X.get,
                                 spot_at=lambda s_, b=est.basis or 0: (X.get(s_) - b) if X.get(s_) else None,
                                 x_latest_sec=sec)
            else:
                fv = est.compute(now=sec, end=end, price_to_beat=ptb, x_at=X.get, spot_at=S.get,
                                 x_latest_sec=sec - 1)
            if fv is None:
                continue
            out.append((dur, end - sec, fv.p_up, int(fin >= ptb)))
    return out


def scores(rows):
    if not rows:
        return {}
    eps = 1e-6
    brier = sum((p - y) ** 2 for _, _, p, y in rows) / len(rows)
    ll = -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for _, _, p, y in rows) / len(rows)
    base = sum(y for *_, y in rows) / len(rows)
    brier0 = sum((base - y) ** 2 for *_, y in rows) / len(rows)
    cal, ece = [], 0.0
    for lo, hi in BUCKETS:
        b = [(p, y) for _, _, p, y in rows if lo <= p < hi or (hi == 1 and p == 1)]
        if b:
            mp, wr = sum(p for p, _ in b) / len(b), sum(y for _, y in b) / len(b)
            cal.append({"bucket": f"{int(lo * 100)}-{int(hi * 100)}%", "n": len(b), "predicted": mp, "actual": wr})
            ece += len(b) / len(rows) * abs(mp - wr)
    return {"n": len(rows), "brier": brier, "brier_coinflip": brier0, "logloss": ll, "ece": ece, "calibration": cal}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="reports/calibration")
    ap.add_argument("--test-days", type=float, default=1.0)
    a = ap.parse_args(argv)
    X, S = load(Path(a.data))
    lo = max(min(S), min(X)) + 2000
    hi = min(max(S), max(X)) - 1
    split = int(hi - a.test_days * 86400)
    grid = [dict(sigma_step=st, vol_mult=vm, dist=di) for st in (1, 10, 30) for vm in (1.0, 1.15)
            for di in ("normal", "t")]
    train = []
    for kw in grid:
        sc = scores(run(X, S, t_from=lo, t_to=split, est_kw=kw, every=30))
        train.append((sc["logloss"], kw, sc))
        print(f"train {kw}: logloss {sc['logloss']:.4f} brier {sc['brier']:.4f} ece {sc['ece']:.4f} n={sc['n']}",
              flush=True)
    train.sort(key=lambda r: r[0])
    best = train[0][1]
    test_rows = run(X, S, t_from=split, t_to=hi, est_kw=best, every=10)
    naive_rows = run(X, S, t_from=split, t_to=hi, est_kw=best, every=10, naive=True)
    rep = {"best_settings": best, "train_from": lo, "split": split, "test_to": hi,
           "test": scores(test_rows), "test_15m": scores([r for r in test_rows if r[0] == "15m"]),
           "test_5m": scores([r for r in test_rows if r[0] == "5m"]),
           "test_last_60s": scores([r for r in test_rows if r[1] <= 60]),
           "naive_lagging_index": scores(naive_rows)}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "calibration.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps({k: (v if k in ("best_settings",) else {kk: vv for kk, vv in v.items() if kk != "calibration"}
                          if isinstance(v, dict) else v) for k, v in rep.items()}, indent=1))
    for c in rep["test"]["calibration"]:
        print(f"  {c['bucket']:>8s}  n={c['n']:6d}  predicted {c['predicted']:.3f}  actual {c['actual']:.3f}")
    return rep


if __name__ == "__main__":
    main()

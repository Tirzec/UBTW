"""Backtest CLI.

  python -m backtest collect --days 3            # download Gamma outcomes, trade tapes, 1s bars
  python -m backtest train   --train-frac 0.7    # fit logistic models on the first 70% of windows
  python -m backtest run     --from-frac 0.7     # out-of-sample replay + HTML report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3

from app.config import load_config, resolve_path
from app.model import LogisticModel

from .data import collect
from .replay import DEFAULTS, brier, dataset, load_bars, load_windows, log_loss, simulate
from .report import summarize, write_report

log = logging.getLogger("backtest")


def _bt_cfg(cfg: dict, args) -> dict:
    s, x = cfg.get("signal", {}), cfg.get("exits", {})
    out = dict(DEFAULTS)
    out.update({k: s[k] for k in ("buffer", "stake_usd", "max_spread", "min_seconds_left",
                                  "max_seconds_left_5m", "max_seconds_left_15m") if k in s})
    out.update({k: x[k] for k in ("hold_buffer", "stop_threshold", "take_profit",
                                  "lock_remaining_ev") if k in x})
    out.update(cfg.get("backtest", {}))
    for k in ("half_spread", "latency_s", "buffer", "stake_usd", "step_s"):
        v = getattr(args, k, None)
        if v is not None:
            out[k] = v
    if getattr(args, "no_exits", False):
        out["use_exits"] = False
    return out


def _split(windows, frac_from: float | None, frac_to: float | None):
    n = len(windows)
    a = int(n * (frac_from or 0.0))
    b = int(n * (frac_to if frac_to is not None else 1.0))
    return windows[a:b]


def _load(conn, durations):
    ws = load_windows(conn, durations)
    if not ws:
        raise SystemExit("no windows in backtest db - run `python -m backtest collect` first")
    bars = load_bars(conn, ws[0].start - 1200, ws[-1].end + 10)
    return ws, bars


def cmd_train(cfg, args):
    conn = sqlite3.connect(args.db)
    ws, bars = _load(conn, args.durations)
    mc = cfg.get("model", {})
    bt = _bt_cfg(cfg, args)
    for d in args.durations:
        wd = [w for w in ws if w.duration == d]
        train = _split(wd, 0.0, args.train_frac)
        test = _split(wd, args.train_frac, 1.0)
        if len(train) < 20:
            log.warning("%s: only %d training windows - skipping", d, len(train))
            continue
        prior = {d: LogisticModel.prior_only()}
        rtr = simulate(train, bars, prior, bt, sample_every_s=args.sample_every)
        X, y = dataset(rtr.predictions)
        m = LogisticModel.fit(X, y, l2=args.l2)
        m.meta.update({"duration": d, "train_windows": len(train),
                       "train_start": train[0].start, "train_end": train[-1].end})
        rte = simulate(test, bars, prior, bt, sample_every_s=args.sample_every)
        Xt, yt = dataset(rte.predictions)
        p_prior = [p["p_model"] for p in rte.predictions]
        p_fit = m.predict_matrix(Xt) if len(Xt) else []
        m.meta["test"] = {"windows": len(test), "n": int(len(yt)),
                          "brier_prior": brier(p_prior, yt), "brier_model": brier(p_fit, yt),
                          "logloss_prior": log_loss(p_prior, yt), "logloss_model": log_loss(p_fit, yt)}
        path = resolve_path(cfg, mc.get(f"path_{d}", f"models/model_{d}.json"))
        t = m.meta["test"]
        if t["n"] and not (t["brier_model"] <= t["brier_prior"]) and not args.force:
            log.warning("%s: trained model is NOT better than the prior out of sample "
                        "(Brier %.4f vs %.4f) - not saving; collect more data or pass --force",
                        d, t["brier_model"], t["brier_prior"])
        else:
            m.save(path)
            log.info("%s model saved to %s", d, path)
        print(json.dumps({"duration": d, "weights": {k: round(v, 4) for k, v in m.weights.items()},
                          "intercept": round(m.intercept, 4), "test": m.meta["test"]}, indent=2))


def cmd_run(cfg, args):
    conn = sqlite3.connect(args.db)
    ws, bars = _load(conn, args.durations)
    mc = cfg.get("model", {})
    models = {d: (LogisticModel.prior_only() if args.prior_only else
                  LogisticModel.load(resolve_path(cfg, mc.get(f"path_{d}", f"models/model_{d}.json"))))
              for d in ("5m", "15m")}
    sel = []
    for d in args.durations:
        sel += _split([w for w in ws if w.duration == d], args.from_frac, args.to_frac)
    sel.sort(key=lambda w: w.start)
    bt = _bt_cfg(cfg, args)
    res = simulate(sel, bars, models, bt, sample_every_s=args.sample_every)
    label = f"{','.join(args.durations)} windows {len(sel)} ({'prior only' if args.prior_only else 'trained'})"
    summary = summarize(res, label)
    out = write_report(summary, res, resolve_path(cfg, args.out), bt)
    r = summary["trades_rule"]
    print(json.dumps({k: summary[k] for k in ("label", "windows_sampled", "skipped", "model_brier",
                                               "market_brier")}, indent=2, default=float))
    print(json.dumps({"rule": r, "hold": summary["trades_hold"]}, indent=2, default=float))
    print(f"report: {out}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="python -m backtest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "train", "run"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=None)
        p.add_argument("--durations", nargs="+", default=["5m", "15m"])
        if name == "collect":
            p.add_argument("--days", type=float, default=3)
            p.add_argument("--min-trade-usd", type=float, default=0.0)
        else:
            p.add_argument("--sample-every", type=int, default=10)
            p.add_argument("--half-spread", dest="half_spread", type=float)
            p.add_argument("--latency", dest="latency_s", type=float)
            p.add_argument("--buffer", type=float)
            p.add_argument("--stake", dest="stake_usd", type=float)
            p.add_argument("--step", dest="step_s", type=int)
        if name == "train":
            p.add_argument("--train-frac", type=float, default=0.7)
            p.add_argument("--l2", type=float, default=5.0)
            p.add_argument("--force", action="store_true", help="save even if it loses to the prior")
        if name == "run":
            p.add_argument("--from-frac", type=float, default=0.0)
            p.add_argument("--to-frac", type=float, default=1.0)
            p.add_argument("--prior-only", action="store_true")
            p.add_argument("--no-exits", action="store_true")
            p.add_argument("--out", default="reports/backtest")
    args = ap.parse_args()
    cfg = load_config()
    args.db = args.db or str(resolve_path(cfg, "data/backtest.db"))
    if args.cmd == "collect":
        asyncio.run(collect(cfg, args.db, args.days, args.durations, args.min_trade_usd))
    elif args.cmd == "train":
        cmd_train(cfg, args)
    else:
        cmd_run(cfg, args)


if __name__ == "__main__":
    main()

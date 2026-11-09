import math

import numpy as np
import pytest

from app.model import LogisticModel
from backtest.replay import DEFAULTS, DenseBars, Tape, WindowData, quote, simulate, window_scale
from backtest.report import summarize, write_report
from app.paper import calibration, max_drawdown, trade_stats

SCHED = {"rate": 0.07, "exponent": 1}


def make_bars(t0, n, start_px=70000.0, drift=0.0, sigma=5e-5, seed=0):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, sigma, n)
    c = start_px * np.exp(np.cumsum(r))
    return [(t0 + i, c[i], c[i], c[i], c[i], 1.0, 0.5, 0.5) for i in range(n)]


def test_dense_bars_forward_fill_and_window():
    rows = [(10, 1, 1, 1, 1.0, 1, 1, 0), (13, 2, 2, 2, 2.0, 1, 0, 1)]
    b = DenseBars(rows)
    assert list(b.c) == [1.0, 1.0, 1.0, 2.0]
    w = b.window(12, 2)
    assert list(w.close) == [1.0, 1.0] and w.end_sec == 12
    assert b.mean(10, 13) == pytest.approx(1.25)


def test_tape_last_and_volume():
    t = Tape([(10, 0.5, 5), (20, 0.6, 3), (30, 0.7, 2)])
    assert t.last(25, 30) == 0.6 and t.last(5, 30) is None and t.last(100, 30) is None
    assert t.volume(15, 30) == 5 and t.volume(0, 100) == 10


def _window(start, outcome, up_px, ptb=70000.0):
    up = Tape([(start + s, up_px, 1000) for s in range(0, 300, 3)])
    dn = Tape([(start + s, round(1 - up_px, 2), 1000) for s in range(1, 300, 3)])
    return WindowData(f"btc-updown-5m-{start}", "5m", start, start + 300, ptb, ptb, outcome,
                      SCHED, up, dn)


def test_quote_uses_other_side_when_needed():
    w = _window(1000, "UP", 0.5)
    w.up = Tape([])
    bid, ask = quote(w, "UP", 1100, DEFAULTS)
    assert (bid, ask) == (0.49, 0.51)


def test_window_scale_reanchors_to_price_to_beat():
    bars = DenseBars(make_bars(0, 2000, sigma=0))
    w = _window(1500, "UP", 0.5, ptb=70700.0)
    assert window_scale(w, bars) == pytest.approx(1.01)


def test_simulate_takes_mispriced_trades_and_settles():
    # BTC drifts up strongly; the market keeps UP at 0.50 -> model should buy UP and win
    t0 = 0
    bars = DenseBars(make_bars(t0, 4000, drift=2e-5, sigma=2e-5, seed=1))
    start = 1500
    tw = bars.mean(start - 59, start)
    w = _window(start, "UP", 0.50, ptb=tw)
    res = simulate([w], bars, {"5m": LogisticModel.prior_only(), "15m": LogisticModel.prior_only()},
                   {"step_s": 5}, sample_every_s=30)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["side"] == "UP" and t["won"] == 1
    # entry pays ask (0.51) + latency (same) + impact, plus the fee
    assert t["entry_price"] == pytest.approx(0.51 + DEFAULTS["impact_per_100usd"] * 0.5)
    assert t["pnl_hold"] == pytest.approx(t["shares"] - t["cost"] - t["entry_fee"])
    assert res.predictions and all(0 <= p["p_model"] <= 1 for p in res.predictions)


def test_simulate_no_lookahead():
    # identical history up to t, different future -> identical decisions at t
    base = make_bars(0, 2000, sigma=3e-5, seed=4)
    alt = list(base)
    for i in range(1700, 2000):
        s = alt[i]
        alt[i] = (s[0], s[1] * 0.99, s[2] * 0.99, s[3] * 0.99, s[4] * 0.99, s[5], s[6], s[7])
    w1 = _window(1500, "UP", 0.5, ptb=DenseBars(base).mean(1441, 1500))
    w2 = _window(1500, "UP", 0.5, ptb=DenseBars(base).mean(1441, 1500))
    m = {"5m": LogisticModel.prior_only(), "15m": LogisticModel.prior_only()}
    r1 = simulate([w1], DenseBars(base), m, {"step_s": 5}, sample_every_s=5)
    r2 = simulate([w2], DenseBars(alt), m, {"step_s": 5}, sample_every_s=5)
    assert not r1.skipped and not r2.skipped
    early1 = [p["p_model"] for p in r1.predictions if p["t"] <= 1700]
    early2 = [p["p_model"] for p in r2.predictions if p["t"] <= 1700]
    assert early1 and early1 == early2
    late1 = [p["p_model"] for p in r1.predictions if p["t"] > 1705]
    late2 = [p["p_model"] for p in r2.predictions if p["t"] > 1705]
    assert late1 != late2


def test_simulate_skips_rules_mismatch_and_missing_bars():
    bars = DenseBars(make_bars(0, 2000))
    w = _window(1500, "UP", 0.5)
    w.rules_ok = False
    w2 = _window(5000, "UP", 0.5)
    res = simulate([w, w2], bars, {"5m": LogisticModel.prior_only()})
    assert res.skipped == {"rules": 1, "no_bars": 1} and not res.trades


def test_stats_helpers():
    assert max_drawdown([1, -2, -1, 3, -4]) == pytest.approx(4)
    trades = [{"pnl_rule": 1.0, "won": 1, "edge": 0.05, "entry_price": 0.5, "cost": 10, "entry_fee": 0.1},
              {"pnl_rule": -1.0, "won": 0, "edge": 0.03, "entry_price": 0.5, "cost": 10, "entry_fee": 0.1}]
    s = trade_stats(trades)
    assert s["n"] == 2 and s["win_rate"] == 0.5 and s["ev_per_trade"] == 0
    cal = calibration([(0.05, 0), (0.07, 1), (0.95, 1)], bins=10)
    assert cal[0]["n"] == 2 and cal[0]["actual"] == 0.5 and cal[-1]["predicted"] == 0.95


def test_report_written(tmp_path):
    bars = DenseBars(make_bars(0, 4000, drift=2e-5, sigma=2e-5, seed=1))
    w = _window(1500, "UP", 0.50, ptb=bars.mean(1441, 1500))
    res = simulate([w], bars, {"5m": LogisticModel.prior_only()})
    s = summarize(res, "test")
    p = write_report(s, res, tmp_path, DEFAULTS)
    assert p.exists() and "<svg" in p.read_text()
    assert s["trades_rule"]["n"] == 1 and math.isfinite(s["model_brier"])

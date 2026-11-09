import math

import numpy as np
import pytest

from app.features import (FEATURES, compute_features, ewma_vol, momentum_sigma, norm_cdf,
                          order_flow_imbalance, prob_up, resample_closes, rsi, twap_terminal,
                          vwap_deviation)
from app.series import BarArrays, SecondBars


def bars_from(close, vol=None, buy=None, sell=None):
    c = np.asarray(close, float)
    n = len(c)
    v = np.ones(n) if vol is None else np.asarray(vol, float)
    b = v / 2 if buy is None else np.asarray(buy, float)
    s = v / 2 if sell is None else np.asarray(sell, float)
    return BarArrays(n, c, c, c, v, b, s, c * v)


def test_norm_cdf():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)


def test_ewma_vol_recovers_sigma():
    rng = np.random.default_rng(0)
    sigma = 1e-4
    c = 70000 * np.exp(np.cumsum(rng.normal(0, sigma, 5000)))
    est = ewma_vol(c, halflife_s=5000, floor=1e-9)
    assert est == pytest.approx(sigma, rel=0.08)


def test_ewma_vol_floor():
    assert ewma_vol(np.full(100, 70000.0), floor=2e-5) == 2e-5


def test_twap_terminal_before_lookback():
    # tau > L: variance = (tau - L) + L/3 (in sigma^2 S^2 units)
    m, sd = twap_terminal(100.0, tau=300, lookback=60, sigma=0.01)
    assert m == 100.0
    assert sd == pytest.approx(100 * 0.01 * math.sqrt(240 + 20))


def test_twap_terminal_inside_lookback_uses_locked_mean():
    # 20s left of a 60s TWAP; first 40s averaged 101, spot now 100
    m, sd = twap_terminal(100.0, tau=20, lookback=60, sigma=0.01, locked_mean=101.0)
    assert m == pytest.approx((40 * 101 + 20 * 100) / 60)
    assert sd == pytest.approx(100 * 0.01 * math.sqrt(20 ** 3 / (3 * 60 ** 2)))


def test_twap_terminal_continuous_at_boundary():
    a = twap_terminal(100.0, tau=60.0001, lookback=60, sigma=0.01)
    b = twap_terminal(100.0, tau=60, lookback=60, sigma=0.01, locked_mean=100.0)
    assert a[1] == pytest.approx(b[1], rel=1e-4)


def test_twap_sd_smaller_than_spot_sd():
    _, sd_twap = twap_terminal(100, 300, 60, 0.01)
    _, sd_spot = twap_terminal(100, 300, 0, 0.01)
    assert sd_twap < sd_spot


def test_twap_variance_monte_carlo():
    rng = np.random.default_rng(1)
    S, sigma, tau, L = 100.0, 0.001, 120, 60
    paths = S * np.exp(np.cumsum(rng.normal(0, sigma, (20000, tau)), axis=1))
    F = paths[:, -L:].mean(axis=1)
    m, sd = twap_terminal(S, tau, L, sigma)
    assert F.mean() == pytest.approx(m, rel=1e-3)
    assert F.std() == pytest.approx(sd, rel=0.05)


def test_prob_up_symmetry_and_ties():
    assert prob_up(100, 1, 100) == pytest.approx(0.5)
    assert prob_up(101, 1, 100) == pytest.approx(norm_cdf(1))
    assert prob_up(100, 0, 100) == 1.0      # ">=" resolves UP on a tie
    assert prob_up(99.9, 0, 100) == 0.0


def test_momentum_sigma():
    c = np.array([100.0] * 61)
    c[-1] = 100 * math.exp(0.001)
    assert momentum_sigma(c, 60, 1e-4) == pytest.approx(0.001 / (1e-4 * math.sqrt(60)))
    assert momentum_sigma(c[:10], 60, 1e-4) == 0.0


def test_vwap_deviation_sign():
    c = np.linspace(100, 101, 300)
    assert vwap_deviation(bars_from(c), 300, 1e-4) > 0


def test_rsi_extremes():
    assert rsi(np.arange(1, 30, dtype=float)) == 100.0
    assert rsi(np.arange(30, 1, -1, dtype=float)) == pytest.approx(0.0)
    assert rsi(np.array([1.0, 2.0])) == 50.0


def test_rsi_known_value():
    # alternating +1 / -1 -> RSI 50
    c = np.cumsum([1, -1] * 20).astype(float) + 100
    assert rsi(c) == pytest.approx(50.0, abs=5)


def test_order_flow_imbalance():
    b = bars_from(np.full(60, 100.0), vol=np.ones(60), buy=np.full(60, 0.75), sell=np.full(60, 0.25))
    assert order_flow_imbalance(b, 60) == pytest.approx(0.5)


def test_resample_closes():
    c = np.arange(180, dtype=float)
    assert list(resample_closes(c, 60)) == [59.0, 119.0, 179.0]


def test_compute_features_shapes_and_prior():
    rng = np.random.default_rng(2)
    c = 70000 * np.exp(np.cumsum(rng.normal(0, 5e-5, 960)))
    fs = compute_features(bars=bars_from(c), ref_spot=float(c[-1]), price_to_beat=float(c[-1]) - 50,
                          seconds_left=120, lookback_s=60)
    assert set(fs.values) == set(FEATURES)
    assert 0.5 < fs.p_prior < 1
    assert len(fs.vector()) == len(FEATURES)
    assert all(math.isfinite(v) for v in fs.values.values())


def test_second_bars_window_forward_fill():
    sb = SecondBars()
    sb.add_trade(1000.2, 10.0, 1, 1)
    sb.add_trade(1000.7, 12.0, 2, -1)
    sb.add_trade(1003.1, 11.0, 1, 1)
    w = sb.window(1004, 5)
    assert list(w.close) == [12.0, 12.0, 12.0, 11.0, 11.0]
    assert w.buy_volume[0] == 1 and w.sell_volume[0] == 2
    assert w.high[0] == 12.0 and w.low[0] == 10.0

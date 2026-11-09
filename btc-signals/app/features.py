"""Feature calculations. Pure functions over numpy arrays, shared by live + backtest.

Key idea: the market settles on a 60s TWAP, not a spot print. With S the
current Chainlink price, tau seconds left, L the TWAP lookback, and sigma
the 1s log-return vol (driftless Brownian approximation):

  tau > L : F - S  ~ N(0, S^2 sigma^2 [(tau - L) + L/3])
  tau <= L: part of the average is already fixed. With A = mean price over
            [end-L, now] (L - tau seconds) and the rest still to come,
            E[F] = ((L - tau) A + tau S) / L,
            Var[F] = S^2 sigma^2 tau^3 / (3 L^2)

P(UP) prior = Phi((E[F] - price_to_beat) / sd).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .series import BarArrays

FEATURES = [
    "logit_prior",   # logit of the TWAP-aware Brownian probability
    "mom_60",        # 1m log return in sigma units
    "mom_180",       # 3m
    "mom_300",       # 5m
    "vwap_dev",      # (price - 5m VWAP) in sigma units
    "rsi",           # (RSI14 on 1m closes - 50) / 50
    "ofi_60",        # taker buy-sell imbalance over 60s, [-1, 1]
    "book_imb",      # spot/perp top-of-book imbalance, [-1, 1]
    "vol_ratio",     # log(short vol / long vol)
    "funding",       # perp funding rate (per hour, in bps)
    "oi_change",     # 5m change in open interest, %
    "liq_imb",       # net liquidations (shorts - longs) / total, [-1, 1]
    "news",          # decayed news sentiment x confidence, [-1, 1]
]


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def logit(p: float, eps: float = 1e-4) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def log_returns(close: np.ndarray) -> np.ndarray:
    c = np.asarray(close, dtype=float)
    if len(c) < 2:
        return np.zeros(0)
    return np.diff(np.log(c))


def ewma_vol(close: np.ndarray, halflife_s: float = 900, floor: float = 2e-5) -> float:
    """EWMA std of 1s log returns (per sqrt(second))."""
    r = log_returns(close)
    if len(r) == 0:
        return floor
    lam = 0.5 ** (1.0 / max(halflife_s, 1.0))
    w = lam ** np.arange(len(r) - 1, -1, -1)
    var = float(np.sum(w * r * r) / np.sum(w))
    return max(math.sqrt(var), floor)


def realized_vol(close: np.ndarray, n: int) -> float:
    r = log_returns(close[-(n + 1):])
    return float(np.sqrt(np.mean(r * r))) if len(r) else 0.0


def twap_terminal(spot: float, tau: float, lookback: float, sigma: float,
                  locked_mean: float | None = None) -> tuple[float, float]:
    """Mean and std (in price units) of the TWAP the market will settle on."""
    tau = max(float(tau), 0.0)
    L = float(lookback)
    if L <= 0:  # plain spot settlement
        return spot, spot * sigma * math.sqrt(tau)
    if tau > L:
        var_t = (tau - L) + L / 3.0
        return spot, spot * sigma * math.sqrt(var_t)
    locked = spot if locked_mean is None else locked_mean
    mean = ((L - tau) * locked + tau * spot) / L
    sd = spot * sigma * math.sqrt(tau ** 3 / (3.0 * L * L))
    return mean, sd


def prob_up(mean: float, sd: float, price_to_beat: float) -> float:
    """P(F >= price_to_beat)."""
    if sd <= 1e-12:
        return 1.0 if mean >= price_to_beat else 0.0
    return norm_cdf((mean - price_to_beat) / sd)


def momentum_sigma(close: np.ndarray, n: int, sigma: float) -> float:
    if len(close) <= n or sigma <= 0:
        return 0.0
    r = math.log(close[-1] / close[-1 - n])
    return r / (sigma * math.sqrt(n))


def vwap_deviation(b: BarArrays, n: int, sigma: float) -> float:
    pv = float(b.pv[-n:].sum())
    v = float(b.volume[-n:].sum())
    if v <= 0 or sigma <= 0:
        return 0.0
    vwap = pv / v
    return math.log(b.close[-1] / vwap) / (sigma * math.sqrt(n))


def resample_closes(close: np.ndarray, step: int) -> np.ndarray:
    """1s closes -> step-second closes (last value of each bucket), aligned to the end."""
    n = (len(close) // step) * step
    if n == 0:
        return close[-1:].copy()
    return close[-n:].reshape(-1, step)[:, -1]


def rsi(close: np.ndarray, period: int = 14) -> float:
    """Wilder RSI. Returns 50 when there is not enough data."""
    if len(close) < period + 1:
        return 50.0
    d = np.diff(np.asarray(close, dtype=float))
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    avg_u = up[:period].mean()
    avg_d = dn[:period].mean()
    for i in range(period, len(d)):
        avg_u = (avg_u * (period - 1) + up[i]) / period
        avg_d = (avg_d * (period - 1) + dn[i]) / period
    if avg_d == 0:
        return 100.0 if avg_u > 0 else 50.0
    rs = avg_u / avg_d
    return 100.0 - 100.0 / (1.0 + rs)


def order_flow_imbalance(b: BarArrays, n: int) -> float:
    bv = float(b.buy_volume[-n:].sum())
    sv = float(b.sell_volume[-n:].sum())
    tot = bv + sv
    return (bv - sv) / tot if tot > 0 else 0.0


def book_imbalance(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                   depth: int = 10) -> float:
    b = sum(s for _, s in bids[:depth])
    a = sum(s for _, s in asks[:depth])
    return (b - a) / (b + a) if (a + b) > 0 else 0.0


@dataclass
class FeatureSet:
    values: dict[str, float]
    p_prior: float
    sigma: float
    d_sigma: float               # (E[F] - price_to_beat) / sd
    expected_final: float
    sd_final: float
    context: dict = field(default_factory=dict)

    def vector(self) -> np.ndarray:
        return np.array([self.values[k] for k in FEATURES], dtype=float)


def compute_features(*, bars: BarArrays, ref_spot: float, price_to_beat: float,
                     seconds_left: float, lookback_s: float, locked_mean: float | None = None,
                     vol_halflife_s: float = 900, min_vol: float = 2e-5,
                     book_imb: float = 0.0, derivs: dict | None = None,
                     news_score: float = 0.0) -> FeatureSet:
    """All model inputs at one instant. `bars` should cover >= 15 minutes of 1s data."""
    c = bars.close
    sigma = ewma_vol(c, vol_halflife_s, min_vol)
    mean, sd = twap_terminal(ref_spot, seconds_left, lookback_s, sigma, locked_mean)
    p0 = prob_up(mean, sd, price_to_beat)
    d_sig = (mean - price_to_beat) / sd if sd > 1e-12 else math.copysign(10.0, mean - price_to_beat)
    closes_1m = resample_closes(c, 60)
    short_v = realized_vol(c, 60)
    long_v = realized_vol(c, min(len(c) - 1, 900))
    d = derivs or {}
    vals = {
        "logit_prior": logit(p0),
        "mom_60": momentum_sigma(c, 60, sigma),
        "mom_180": momentum_sigma(c, 180, sigma),
        "mom_300": momentum_sigma(c, 300, sigma),
        "vwap_dev": vwap_deviation(bars, min(300, len(c)), sigma),
        "rsi": (rsi(closes_1m, 14) - 50.0) / 50.0,
        "ofi_60": order_flow_imbalance(bars, 60),
        "book_imb": float(book_imb),
        "vol_ratio": math.log(short_v / long_v) if short_v > 0 and long_v > 0 else 0.0,
        "funding": float(d.get("funding_bps_hr", 0.0)),
        "oi_change": float(d.get("oi_change_pct_5m", 0.0)),
        "liq_imb": float(d.get("liq_imbalance", 0.0)),
        "news": float(news_score),
    }
    return FeatureSet(vals, p0, sigma, d_sig, mean, sd, context={
        "ref_spot": ref_spot, "price_to_beat": price_to_beat, "seconds_left": seconds_left,
        "lookback_s": lookback_s, "locked_mean": locked_mean, "rsi_raw": rsi(closes_1m, 14),
    })

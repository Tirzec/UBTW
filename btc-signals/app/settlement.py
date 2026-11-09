"""Fair probability of UP from the exact settlement rule of Polymarket US BTC Up/Down markets.

Rule (market description, verified 2026-11-09 for 5m and 15m):
  "in the last minute before each of those two times, 60 BRTI prices are collected. The official
   value ... is the simple average of these prices, rounded to the nearest 2 decimal places."
  UP wins if final value >= price to beat (the start value, published by the exchange).

So the final value is A = round2( mean of the 60 one-second BRTI prices at end-60s .. end-1s ).

Model: over seconds-to-minutes the index follows an arithmetic random walk S(t+u) = S(t) + sigma*W(u)
(no drift). With m prices already locked in (sum L) and n = 60 - m still to come at offsets
u_1 < ... < u_n seconds from now:
    mean(A)  = (L + n*S_now) / 60
    var(A)   = sigma^2 * sum_i sum_j min(u_i, u_j) / 60^2
which is exact for a Gaussian walk. Then P(UP) = P(A >= K - 0.005) (rounding + >= tie rule).
Fat tails: optional Student-t with the same variance (dist="t", nu degrees of freedom).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

N_PRICES = 60          # prices in the averaging window (both 5m and 15m)
ROUND_HALF = 0.005     # rounding to cents: A_rounded >= K  <=>  A >= K - 0.005


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def student_t_cdf(x: float, nu: float) -> float:
    """CDF of Student-t (regularized incomplete beta via continued fraction)."""
    if nu <= 0:
        return norm_cdf(x)
    t = nu / (nu + x * x)
    ib = _betainc(nu / 2.0, 0.5, t)
    return 1.0 - 0.5 * ib if x > 0 else 0.5 * ib


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def _betacf(a: float, b: float, x: float, iters: int = 200, eps: float = 1e-12) -> float:
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, iters + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        delta = d * c
        h *= delta
        if abs(delta - 1) < eps:
            break
    return h


def sum_min_offsets(offsets: Sequence[float]) -> float:
    """sum_i sum_j min(u_i, u_j) for sorted positive offsets (O(n))."""
    u = sorted(offsets)
    n = len(u)
    # sum over pairs = sum_k u_k * (2*(n-k) - 1)  for 0-based k in sorted order
    return sum(uk * (2 * (n - k) - 1) for k, uk in enumerate(u))


@dataclass
class FairValue:
    p_up: float
    mean: float            # expected final average
    sd: float              # sd of the final average
    locked: int            # prices already collected
    seconds_left: float
    sigma: float           # $ per sqrt(second)

    def p_side(self, side: str) -> float:
        return self.p_up if side == "UP" else 1.0 - self.p_up


def fair_up(*, now: float, end: float, price_to_beat: float, index_now: float, sigma: float,
            locked_prices: Optional[Sequence[float]] = None, dist: str = "normal", nu: float = 4.0,
            n_prices: int = N_PRICES, locked_sum: Optional[float] = None) -> FairValue:
    """P(final 60-price average, rounded, >= price_to_beat).

    now, end            unix seconds; sample times are end-60, ..., end-1 (one per second)
    index_now           current best estimate of the settlement index (BRTI)
    sigma               index volatility in $ per sqrt(second)
    locked_prices       index prices already collected in the window (oldest first)
    """
    sample_times = [end - n_prices + i for i in range(n_prices)]
    future = [t for t in sample_times if t > now + 1e-9]
    m = n_prices - len(future)
    if locked_sum is not None:
        L = locked_sum if m else 0.0
    else:
        locked = list(locked_prices or [])
        if len(locked) < m:                     # missing locked samples: fill with the current estimate
            locked = locked + [index_now] * (m - len(locked))
        locked = locked[-m:] if m else []
        L = sum(locked)
    mean = (L + len(future) * index_now) / n_prices
    var = sigma * sigma * sum_min_offsets([t - now for t in future]) / n_prices ** 2 if future else 0.0
    sd = math.sqrt(max(var, 0.0))
    k = price_to_beat - ROUND_HALF
    if sd <= 1e-12:
        p = 1.0 if mean >= k else 0.0
    else:
        z = (mean - k) / sd
        if dist == "t" and nu > 2:
            p = student_t_cdf(z * math.sqrt(nu / (nu - 2)), nu)
        else:
            p = norm_cdf(z)
    return FairValue(min(1.0, max(0.0, p)), mean, sd, m, max(0.0, end - now), sigma)


def sigma_from_prices(prices: Sequence[float], step: int = 30, max_points: int = 1800,
                      floor: float = 0.05) -> Optional[float]:
    """$/sqrt(s) volatility from 1-second index prices, using overlapping `step`-second changes
    (1-second changes of a smoothed index understate the real variance)."""
    p = list(prices)[-max_points:]
    if len(p) < step + 30:
        return None
    diffs = [p[i] - p[i - step] for i in range(step, len(p))]
    var = sum(d * d for d in diffs) / len(diffs) / step
    return max(math.sqrt(var), floor)


def mc_fair_up(*, now, end, price_to_beat, index_now, sigma, locked_prices=None, n_paths=20000,
               n_prices=N_PRICES, seed=1):
    """Monte Carlo check of fair_up (tests only)."""
    import random
    rnd = random.Random(seed)
    times = [end - n_prices + i for i in range(n_prices)]
    future = [t for t in times if t > now + 1e-9]
    m = n_prices - len(future)
    locked = list(locked_prices or [])[-m:] if m else []
    locked += [index_now] * (m - len(locked))
    wins = 0
    for _ in range(n_paths):
        s, t_prev, tot = index_now, now, sum(locked)
        for t in future:
            s += sigma * math.sqrt(t - t_prev) * rnd.gauss(0, 1)
            t_prev = t
            tot += s
        if round(tot / n_prices, 2) >= price_to_beat:
            wins += 1
    return wins / n_paths


# --------------------------------------------------------------------------------- live estimator
class FairEstimator:
    """Fair P(UP) every second from:
      X(t)  the published BRTI series. It IS the 60-second average: X(T-1) equals the official
            price to beat / settlement value exactly (120/120 checked 2026-11-09). It lags the
            real price by ~30 s.
      s(t)  fast spot (median of Coinbase/Kraken): an early, raw-price signal.
      b     basis, EWMA of X(t) - mean(s over the same 60 s): raw BRTI ~= s + b.
    The final value is the mean of raw BRTI over end-60 .. end-1.
    """

    def __init__(self, sigma_step: int = 10, sigma_lookback: int = 1800, basis_halflife: float = 600.0,
                 dist: str = "normal", nu: float = 4.0, vol_mult: float = 1.0):
        self.sigma_step, self.sigma_lookback = sigma_step, sigma_lookback
        self.alpha = 1 - 0.5 ** (1 / basis_halflife)
        self.dist, self.nu, self.vol_mult = dist, nu, vol_mult
        self.basis: Optional[float] = None
        self._basis_sec = -1

    def update_basis(self, sec: int, x: Optional[float], spot_mean60: Optional[float]) -> None:
        if x is None or spot_mean60 is None or sec <= self._basis_sec:
            return
        d = x - spot_mean60
        self.basis = d if self.basis is None else self.basis + self.alpha * (d - self.basis)
        self._basis_sec = sec

    def compute(self, *, now: int, end: int, price_to_beat: float, x_at, spot_at,
                x_latest_sec: Optional[int]) -> Optional[FairValue]:
        """x_at(sec)/spot_at(sec) -> value or None. Returns None if data is insufficient."""
        if self.basis is None:
            return None
        b = self.basis

        def raw(sec):
            v = spot_at(sec)
            return None if v is None else v + b
        r_now = raw(now)
        if r_now is None:
            return None
        hist = [raw(s) for s in range(now - self.sigma_lookback, now + 1)]
        hist = [h for h in hist if h is not None]
        sig = sigma_from_prices(hist, step=self.sigma_step, max_points=self.sigma_lookback)
        if sig is None:
            return None
        w0 = end - N_PRICES
        locked_sum = None
        if now >= w0:
            tx = x_latest_sec if (x_latest_sec is not None and x_latest_sec <= now) else None
            pre = None
            if tx is not None and tx >= w0 and x_at(tx) is not None:
                pre_vals = [raw(s) for s in range(tx - N_PRICES + 1, w0)]
                post_vals = [raw(s) for s in range(tx + 1, now + 1)]
                if None not in pre_vals and None not in post_vals:
                    pre = N_PRICES * x_at(tx) - sum(pre_vals) + sum(post_vals)
            if pre is None:
                vals = [raw(s) for s in range(w0, now + 1)]
                if None in vals:
                    return None
                pre = sum(vals)
            locked_sum = pre
        return fair_up(now=now, end=end, price_to_beat=price_to_beat, index_now=r_now,
                       sigma=sig * self.vol_mult, dist=self.dist, nu=self.nu, locked_sum=locked_sum)

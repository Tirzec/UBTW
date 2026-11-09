"""Explainable P(UP) model: logistic regression on top of the TWAP-aware prior.

z = b0 + sum_i w_i * (x_i - mu_i) / sd_i ; P(UP) = sigmoid(z)
The untrained default is w(logit_prior) = 1 and all other weights 0, so it
returns exactly the analytic prior until you train it on backtest data.
Saved as human-readable JSON.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .features import FEATURES, FeatureSet, sigmoid


@dataclass
class LogisticModel:
    weights: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in FEATURES})
    intercept: float = 0.0
    means: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in FEATURES})
    stds: dict[str, float] = field(default_factory=lambda: {k: 1.0 for k in FEATURES})
    meta: dict = field(default_factory=dict)

    @classmethod
    def prior_only(cls) -> "LogisticModel":
        m = cls()
        m.weights["logit_prior"] = 1.0
        m.meta = {"kind": "prior_only", "note": "untrained: P(UP) = TWAP-aware Brownian prior"}
        return m

    # ---------------------------------------------------------------- predict
    def _z_parts(self, values: dict[str, float]) -> dict[str, float]:
        return {k: self.weights.get(k, 0.0) * (values.get(k, 0.0) - self.means.get(k, 0.0))
                / (self.stds.get(k, 1.0) or 1.0) for k in FEATURES}

    def predict(self, fs: FeatureSet | dict) -> tuple[float, dict[str, float]]:
        """Returns P(UP) and each feature's contribution to the log-odds."""
        values = fs.values if isinstance(fs, FeatureSet) else fs
        parts = self._z_parts(values)
        z = self.intercept + sum(parts.values())
        return sigmoid(z), parts

    def predict_matrix(self, X: np.ndarray) -> np.ndarray:
        w, mu, sd = self._arrays()
        z = self.intercept + ((X - mu) / sd) @ w
        return 1.0 / (1.0 + np.exp(-z))

    def _arrays(self):
        w = np.array([self.weights.get(k, 0.0) for k in FEATURES])
        mu = np.array([self.means.get(k, 0.0) for k in FEATURES])
        sd = np.array([self.stds.get(k, 1.0) or 1.0 for k in FEATURES])
        return w, mu, sd

    # ---------------------------------------------------------------- train
    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50,
            sample_weight: np.ndarray | None = None) -> "LogisticModel":
        """L2-regularised logistic regression by Newton/IRLS (intercept unpenalised)."""
        X = np.asarray(X, float)
        y = np.asarray(y, float)
        n, k = X.shape
        sw = np.ones(n) if sample_weight is None else np.asarray(sample_weight, float)
        mu = X.mean(axis=0)
        sd = X.std(axis=0)
        sd[sd < 1e-9] = 1.0
        # don't rescale the prior: keep its weight interpretable (1.0 == trust the prior)
        i0 = FEATURES.index("logit_prior")
        mu[i0], sd[i0] = 0.0, 1.0
        Z = np.hstack([np.ones((n, 1)), (X - mu) / sd])
        beta = np.zeros(k + 1)
        beta[1 + i0] = 1.0
        pen = np.full(k + 1, l2)
        pen[0] = 0.0
        pen[1 + i0] = l2 * 0.01  # weak shrinkage toward 0 on the prior; it is the backbone
        for _ in range(iters):
            p = 1.0 / (1.0 + np.exp(-(Z @ beta)))
            W = sw * p * (1 - p) + 1e-9
            g = Z.T @ (sw * (p - y)) + pen * beta
            H = (Z * W[:, None]).T @ Z + np.diag(pen)
            step = np.linalg.solve(H, g)
            beta -= step
            if np.max(np.abs(step)) < 1e-8:
                break
        m = cls()
        m.intercept = float(beta[0])
        m.weights = {f: float(beta[1 + i]) for i, f in enumerate(FEATURES)}
        m.means = {f: float(mu[i]) for i, f in enumerate(FEATURES)}
        m.stds = {f: float(sd[i]) for i, f in enumerate(FEATURES)}
        m.meta = {"kind": "logistic", "n": int(n), "l2": l2, "pos_rate": float(y.mean())}
        return m

    # ---------------------------------------------------------------- io
    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({
            "features": FEATURES, "intercept": self.intercept, "weights": self.weights,
            "means": self.means, "stds": self.stds, "meta": self.meta}, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "LogisticModel":
        p = Path(path)
        if not p.exists():
            return cls.prior_only()
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        m = cls()
        m.intercept = float(d.get("intercept", 0.0))
        m.weights.update(d.get("weights", {}))
        m.means.update(d.get("means", {}))
        m.stds.update(d.get("stds", {}))
        m.meta = d.get("meta", {})
        return m


# ------------------------------------------------------------------ explain
def _fmt_secs(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def describe_driver(name: str, fs: FeatureSet) -> str:
    v = fs.values[name]
    c = fs.context
    if name == "logit_prior":
        diff = c["ref_spot"] - c["price_to_beat"]
        return (f"BTC {'+' if diff >= 0 else '-'}${abs(diff):,.0f} vs price to beat "
                f"({fs.d_sigma:+.2f}σ of the settlement TWAP) with {_fmt_secs(c['seconds_left'])} left")
    if name.startswith("mom_"):
        return f"{int(name[4:]) // 60}m momentum {v:+.2f}σ"
    if name == "vwap_dev":
        return f"price {v:+.2f}σ vs 5m VWAP"
    if name == "rsi":
        return f"RSI(14, 1m) {c.get('rsi_raw', 50):.0f}"
    if name == "ofi_60":
        return f"60s order flow {(v + 1) / 2:.0%} taker buys"
    if name == "book_imb":
        return f"order book imbalance {v:+.2f}"
    if name == "vol_ratio":
        return f"short-term vol {math.exp(v):.2f}x the 15m average"
    if name == "funding":
        return f"perp funding {v:+.2f} bps/hr"
    if name == "oi_change":
        return f"open interest {v:+.2f}% in 5m"
    if name == "liq_imb":
        return f"liquidations skew {v:+.2f} (+ = shorts liquidated)"
    if name == "news":
        return f"news sentiment {v:+.2f}"
    return f"{name} {v:+.3f}"


def top_drivers(parts: dict[str, float], fs: FeatureSet, side: str, n: int = 3) -> list[dict]:
    """Top-n contributions, signed toward the recommended side."""
    sign = 1.0 if side != "DOWN" else -1.0
    ranked = sorted(parts.items(), key=lambda kv: abs(kv[1]), reverse=True)
    out = []
    for name, contrib in ranked[:n]:
        if abs(contrib) < 1e-6:
            continue
        out.append({"feature": name, "contribution": round(contrib, 3),
                    "supports": "for" if contrib * sign > 0 else "against",
                    "text": describe_driver(name, fs)})
    return out

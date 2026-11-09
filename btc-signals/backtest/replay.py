"""Window-by-window replay using the same features/model/signal/exit code as live.

No look-ahead: at decision time t only bars with sec <= t-1 and trades with ts <= t
are used. Fill model (no historical order books exist):
  * ask = last traded price of that outcome (<= max_quote_age_s old) + half_spread
  * bid = last traded price - half_spread
  * entry fills at the worse of ask(t) and ask(t + latency_s), plus impact_per_100usd
  * the trade only fills if that outcome traded >= stake/price shares within
    +/- depth_window_s (a liquidity proxy); otherwise it is skipped
  * taker fees use the exact Polymarket curve, on entry and on any early exit
"""
from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field

import numpy as np

from app.exits import evaluate_exit
from app.features import FEATURES, compute_features
from app.fees import walk_buy, walk_sell
from app.model import LogisticModel
from app.series import BarArrays
from app.signal import decide

DEFAULTS = {
    "step_s": 5, "half_spread": 0.01, "latency_s": 2, "max_quote_age_s": 30,
    "depth_window_s": 30, "impact_per_100usd": 0.005, "stake_usd": 50, "buffer": 0.04,
    "max_spread": 0.04, "min_seconds_left": 10, "max_seconds_left_5m": 290,
    "max_seconds_left_15m": 880, "vol_halflife_s": 900, "min_vol": 2e-5,
    "hold_buffer": 0.01, "stop_threshold": 0.15, "take_profit": 0.20, "lock_remaining_ev": 0.03,
    "use_exits": True, "lookback_s": 60, "vol_step_s": 60,
}


class DenseBars:
    """Contiguous forward-filled 1s arrays; window() is an O(1) slice."""

    def __init__(self, rows: list[tuple]):
        if not rows:
            raise ValueError("no bars")
        rows = sorted(rows)
        self.t0 = int(rows[0][0])
        n = int(rows[-1][0]) - self.t0 + 1
        c = np.full(n, np.nan)
        h = np.full(n, np.nan)
        lo = np.full(n, np.nan)
        v = np.zeros(n)
        bv = np.zeros(n)
        sv = np.zeros(n)
        for sec, o, hh, ll, cc, vv, bb, ss in rows:
            i = int(sec) - self.t0
            c[i], h[i], lo[i], v[i], bv[i], sv[i] = cc, hh, ll, vv, bb, ss
        # forward fill
        mask = np.isnan(c)
        idx = np.where(~mask, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        c, h, lo = c[idx], h[idx], lo[idx]
        h[mask], lo[mask] = c[mask], c[mask]
        self.c, self.h, self.l, self.v, self.bv, self.sv = c, h, lo, v, bv, sv
        self.pv = c * v
        self.t1 = self.t0 + n - 1

    def covers(self, a: int, b: int) -> bool:
        return a >= self.t0 and b <= self.t1

    def window(self, end_sec: int, n: int, scale: float = 1.0) -> BarArrays:
        j = end_sec - self.t0 + 1
        i = max(0, j - n)
        return BarArrays(end_sec, self.c[i:j] * scale, self.h[i:j] * scale, self.l[i:j] * scale,
                         self.v[i:j], self.bv[i:j], self.sv[i:j], self.pv[i:j] * scale)

    def close_at(self, sec: int) -> float:
        return float(self.c[sec - self.t0])

    def mean(self, a: int, b: int) -> float:
        return float(self.c[a - self.t0: b - self.t0 + 1].mean())


class Tape:
    """Trades of one outcome, sorted by time."""

    def __init__(self, trades: list[tuple[int, float, float]]):
        trades = sorted(trades)
        self.ts = [t for t, _, _ in trades]
        self.px = [p for _, p, _ in trades]
        self.sz = [s for _, _, s in trades]
        self.cum = list(np.cumsum(self.sz)) if trades else []

    def last(self, t: float, max_age: float) -> float | None:
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0 or t - self.ts[i] > max_age:
            return None
        return self.px[i]

    def volume(self, a: float, b: float) -> float:
        i = bisect.bisect_left(self.ts, a)
        j = bisect.bisect_right(self.ts, b) - 1
        if j < i:
            return 0.0
        return self.cum[j] - (self.cum[i - 1] if i > 0 else 0.0)


@dataclass
class WindowData:
    slug: str
    duration: str
    start: int
    end: int
    price_to_beat: float
    final_price: float
    outcome: str
    fee_schedule: dict
    up: Tape
    down: Tape
    rules_ok: bool = True


@dataclass
class BTResult:
    trades: list[dict] = field(default_factory=list)
    predictions: list[dict] = field(default_factory=list)   # (p_model, p_market, y) samples
    skipped: dict = field(default_factory=dict)


def quote(w: WindowData, side: str, t: float, cfg: dict) -> tuple[float | None, float | None]:
    """(bid, ask) estimate for `side` at time t from the tape."""
    age = cfg["max_quote_age_s"]
    hs = cfg["half_spread"]
    own, other = (w.up, w.down) if side == "UP" else (w.down, w.up)
    last = own.last(t, age)
    if last is None:
        o = other.last(t, age)
        last = None if o is None else 1.0 - o
    if last is None:
        return None, None
    tick = 0.01
    bid = max(tick, round(last - hs, 2))
    ask = min(1 - tick, round(last + hs, 2))
    return bid, ask


def synthetic_book(bid: float | None, ask: float | None, depth_shares: float) -> dict:
    return {"bids": [(bid, depth_shares)] if bid else [],
            "asks": [(ask, depth_shares)] if ask else []}


def features_at(w: WindowData, bars: DenseBars, t: int, scale: float, cfg: dict):
    sec = t - 1
    b = bars.window(sec, 960, scale)
    left = w.end - t
    L = cfg["lookback_s"]
    ref = bars.close_at(sec) * scale
    locked = bars.mean(w.end - L, sec) * scale if left <= L and sec >= w.end - L else None
    return compute_features(bars=b, ref_spot=ref, price_to_beat=w.price_to_beat,
                            seconds_left=left, lookback_s=L, locked_mean=locked,
                            vol_halflife_s=cfg["vol_halflife_s"], min_vol=cfg["min_vol"],
                            vol_step_s=cfg["vol_step_s"])


def window_scale(w: WindowData, bars: DenseBars) -> float:
    """Re-anchor Binance USDT to Chainlink USD: TWAP60(binance) at start -> priceToBeat."""
    tw = bars.mean(w.start - 59, w.start)
    return w.price_to_beat / tw if tw > 0 else 1.0


def simulate(windows: list[WindowData], bars: DenseBars, models: dict[str, LogisticModel],
             cfg: dict | None = None, sample_every_s: int = 30) -> BTResult:
    cfg = {**DEFAULTS, **(cfg or {})}
    res = BTResult()
    sk = res.skipped
    for w in windows:
        if not w.rules_ok:
            sk["rules"] = sk.get("rules", 0) + 1
            continue
        if not bars.covers(w.start - 1000, w.end):
            sk["no_bars"] = sk.get("no_bars", 0) + 1
            continue
        model = models[w.duration]
        scale = window_scale(w, bars)
        y = int(w.outcome == "UP")
        max_left = cfg[f"max_seconds_left_{w.duration}"]
        entered = None
        last_sample = -10**9
        for t in range(w.start + cfg["step_s"], w.end - cfg["min_seconds_left"] + 1, cfg["step_s"]):
            fs = features_at(w, bars, t, scale, cfg)
            p_up, parts = model.predict(fs)
            up_bid, up_ask = quote(w, "UP", t, cfg)
            if t - last_sample >= sample_every_s:
                last_sample = t
                mkt = None if up_bid is None else (up_bid + up_ask) / 2
                res.predictions.append({"slug": w.slug, "duration": w.duration, "t": t,
                                        "seconds_left": w.end - t, "p_model": p_up,
                                        "p_prior": fs.p_prior, "p_market": mkt, "y": y,
                                        "x": fs.vector().tolist()})
            if entered is not None:
                if cfg["use_exits"] and entered.get("exit_ts") is None:
                    side = entered["side"]
                    bid, _ = quote(w, side, t, cfg)
                    if bid is not None:
                        fair = p_up if side == "UP" else 1 - p_up
                        depth = getattr(w, side.lower()).volume(t - cfg["depth_window_s"], t + cfg["depth_window_s"])
                        adv = evaluate_exit(entry_price=entered["entry_price"], shares=entered["shares"],
                                            cost_basis=entered["cost"] + entered["entry_fee"], fair=fair,
                                            bids=[(bid, max(depth, entered["shares"]))],
                                            fee_schedule=w.fee_schedule, hold_buffer=cfg["hold_buffer"],
                                            stop_threshold=cfg["stop_threshold"], take_profit=cfg["take_profit"],
                                            lock_remaining_ev=cfg["lock_remaining_ev"])
                        if adv.action == "SELL":
                            f = walk_sell([(bid, entered["shares"])], entered["shares"], w.fee_schedule)
                            entered.update(exit_ts=t, exit_price=bid, exit_fee=f.fee,
                                           exit_proceeds=f.cost, exit_reason=adv.reason.split(":")[0])
                continue
            left = w.end - t
            if left > max_left:
                continue
            down_bid, down_ask = quote(w, "DOWN", t, cfg)
            depth_up = w.up.volume(t - cfg["depth_window_s"], t + cfg["depth_window_s"])
            depth_dn = w.down.volume(t - cfg["depth_window_s"], t + cfg["depth_window_s"])
            books = {"UP": synthetic_book(up_bid, up_ask, depth_up),
                     "DOWN": synthetic_book(down_bid, down_ask, depth_dn)}
            d = decide(p_up, books, buffer=cfg["buffer"], stake_usd=cfg["stake_usd"],
                       max_spread=cfg["max_spread"], fee_schedule=w.fee_schedule, gates=[])
            if d.action == "NO TRADE":
                continue
            side = d.action
            # latency: you click a few seconds after the alert
            _, ask_late = quote(w, side, t + cfg["latency_s"], cfg)
            ask = max(books[side]["asks"][0][0], ask_late or 0)
            ask = min(0.99, ask + cfg["impact_per_100usd"] * cfg["stake_usd"] / 100)
            f = walk_buy([(ask, 1e12)], cfg["stake_usd"], w.fee_schedule)
            p_side = p_up if side == "UP" else 1 - p_up
            entered = {"slug": w.slug, "duration": w.duration, "side": side, "opened_ts": t,
                       "seconds_left": w.end - t, "entry_price": ask, "shares": f.shares,
                       "cost": f.cost, "entry_fee": f.fee, "p_model": p_side,
                       "edge": p_side - ask - f.fee / f.shares - cfg["buffer"],
                       "raw_edge": p_side - ask, "exit_ts": None, "outcome": w.outcome}
        if entered is not None:
            won = int(entered["side"] == w.outcome)
            basis = entered["cost"] + entered["entry_fee"]
            entered["won"] = won
            entered["pnl_hold"] = entered["shares"] * won - basis
            if entered.get("exit_ts") is not None:
                entered["pnl_rule"] = entered["exit_proceeds"] - entered["exit_fee"] - basis
            else:
                entered["pnl_rule"] = entered["pnl_hold"]
            res.trades.append(entered)
    return res


def load_windows(conn, durations: list[str] | None = None, t0: int | None = None,
                 t1: int | None = None) -> list[WindowData]:
    q = "SELECT * FROM bt_windows WHERE outcome IS NOT NULL AND price_to_beat IS NOT NULL AND trades_done=1"
    rows = conn.execute(q).fetchall()
    cols = [d[0] for d in conn.execute(q).description]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        if durations and d["duration"] not in durations:
            continue
        if (t0 and d["start"] < t0) or (t1 and d["start"] >= t1):
            continue
        tr = conn.execute("SELECT ts, outcome, price, size FROM bt_trades WHERE slug=?",
                          (d["slug"],)).fetchall()
        up = Tape([(ts, p, s) for ts, o, p, s in tr if (o or "").lower() == "up"])
        dn = Tape([(ts, p, s) for ts, o, p, s in tr if (o or "").lower() == "down"])
        out.append(WindowData(d["slug"], d["duration"], d["start"], d["end"], d["price_to_beat"],
                              d["final_price"], d["outcome"], json.loads(d["fee_schedule"] or "{}"),
                              up, dn, bool(d["rules_ok"])))
    out.sort(key=lambda w: w.start)
    return out


def load_bars(conn, t0: int, t1: int) -> DenseBars:
    rows = conn.execute("SELECT sec,o,h,l,c,v,bv,sv FROM bt_bars WHERE sec BETWEEN ? AND ?",
                        (t0, t1)).fetchall()
    return DenseBars(rows)


def dataset(preds: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array([p["x"] for p in preds], dtype=float).reshape(-1, len(FEATURES))
    y = np.array([p["y"] for p in preds], dtype=float)
    return X, y


def brier(p, y) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2)) if len(p) else math.nan


def log_loss(p, y) -> float:
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))) if len(p) else math.nan

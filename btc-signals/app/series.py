"""1-second bar and value series shared by the live engine and the backtester."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class BarArrays:
    """Aligned, forward-filled 1s arrays ending at `end_sec` (inclusive)."""
    end_sec: int
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    volume: np.ndarray
    buy_volume: np.ndarray
    sell_volume: np.ndarray
    pv: np.ndarray            # price * volume, for VWAP

    def __len__(self) -> int:
        return len(self.close)


class SecondBars:
    """Trades -> 1s OHLCV bars with taker buy/sell volume. Bounded memory."""

    def __init__(self, max_seconds: int = 4 * 3600):
        self.max_seconds = max_seconds
        self._bars: dict[int, list[float]] = {}
        self.last_sec: int | None = None

    def add_trade(self, ts: float, price: float, size: float, side: int) -> None:
        s = int(ts)
        b = self._bars.get(s)
        if b is None:
            self._bars[s] = b = [price, price, price, price, 0.0, 0.0, 0.0, 0.0]
            if self.last_sec is None or s > self.last_sec:
                self.last_sec = s
                self._trim()
        else:
            b[1] = max(b[1], price)
            b[2] = min(b[2], price)
            b[3] = price
        b[4] += size
        if side > 0:
            b[5] += size
        elif side < 0:
            b[6] += size
        b[7] += price * size

    def add_bar(self, sec: int, o: float, h: float, l: float, c: float, v: float = 0.0,
                bv: float = 0.0, sv: float = 0.0) -> None:
        self._bars[int(sec)] = [o, h, l, c, v, bv, sv, c * v]
        if self.last_sec is None or sec > self.last_sec:
            self.last_sec = int(sec)

    def _trim(self) -> None:
        if len(self._bars) > self.max_seconds * 1.2 and self.last_sec is not None:
            cutoff = self.last_sec - self.max_seconds
            for k in [k for k in self._bars if k < cutoff]:
                del self._bars[k]

    def __len__(self) -> int:
        return len(self._bars)

    def window(self, end_sec: int, n: int) -> BarArrays | None:
        """Last n seconds ending at end_sec, forward-filling empty seconds."""
        if not self._bars:
            return None
        start = end_sec - n + 1
        # seed forward fill from the latest bar at or before start
        prev = None
        for k in range(start - 1, start - 600, -1):
            if k in self._bars:
                prev = self._bars[k][3]
                break
        c = np.empty(n)
        h = np.empty(n)
        lo = np.empty(n)
        v = np.zeros(n)
        bv = np.zeros(n)
        sv = np.zeros(n)
        pv = np.zeros(n)
        have_any = False
        for i in range(n):
            b = self._bars.get(start + i)
            if b is not None:
                have_any = True
                prev = b[3]
                c[i], h[i], lo[i] = b[3], b[1], b[2]
                v[i], bv[i], sv[i], pv[i] = b[4], b[5], b[6], b[7]
            else:
                c[i] = h[i] = lo[i] = np.nan if prev is None else prev
        if not have_any and prev is None:
            return None
        # back-fill a leading NaN run with the first real value
        if np.isnan(c[0]):
            first = c[~np.isnan(c)][0]
            mask = np.isnan(c)
            c[mask] = h[mask] = lo[mask] = first
        return BarArrays(end_sec, c, h, lo, v, bv, sv, pv)


class SecondValues:
    """Sparse value-per-second series (e.g. Chainlink spot or TWAP)."""

    def __init__(self, max_seconds: int = 4 * 3600):
        self.max_seconds = max_seconds
        self._v: dict[int, float] = {}
        self.last_sec: int | None = None

    def add(self, ts: float, value: float) -> None:
        s = int(round(ts)) if abs(ts - round(ts)) < 0.05 else int(ts)
        self._v[s] = value
        if self.last_sec is None or s > self.last_sec:
            self.last_sec = s
            if len(self._v) > self.max_seconds * 1.2:
                cutoff = s - self.max_seconds
                for k in [k for k in self._v if k < cutoff]:
                    del self._v[k]

    def get(self, sec: int) -> float | None:
        return self._v.get(int(sec))

    def at_or_before(self, sec: int, max_back: int = 5) -> tuple[int, float] | None:
        for k in range(int(sec), int(sec) - max_back - 1, -1):
            if k in self._v:
                return k, self._v[k]
        return None

    def mean_between(self, start_sec: int, end_sec: int) -> float | None:
        """Time-weighted mean over [start, end] using forward fill at 1s resolution."""
        if end_sec < start_sec:
            return None
        seed = self.at_or_before(start_sec, max_back=120)
        cur = seed[1] if seed else None
        total, n = 0.0, 0
        for k in range(start_sec, end_sec + 1):
            v = self._v.get(k)
            if v is not None:
                cur = v
            if cur is not None:
                total += cur
                n += 1
        return total / n if n else None

    def latest(self) -> tuple[int, float] | None:
        if self.last_sec is None:
            return None
        return self.last_sec, self._v[self.last_sec]

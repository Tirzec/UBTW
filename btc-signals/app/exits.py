"""SELL / HOLD guidance for an open position (manual or paper)."""
from __future__ import annotations

from dataclasses import dataclass

from .fees import walk_sell


@dataclass
class ExitAdvice:
    action: str                 # "SELL" | "HOLD"
    reason: str
    fair: float                 # model probability the held side wins
    exit_price: float | None    # avg bid after walking the book, before fee
    exit_net: float | None      # per-share proceeds after the taker fee
    value_now: float | None     # shares * exit_net
    pnl_now: float | None       # value_now - cost basis
    hold_ev: float              # shares * fair - cost basis (expected PnL if held)

    def as_dict(self) -> dict:
        r = lambda x: None if x is None else round(x, 4)  # noqa: E731
        return {"action": self.action, "reason": self.reason, "fair": r(self.fair),
                "exit_price": r(self.exit_price), "exit_net": r(self.exit_net),
                "value_now": r(self.value_now), "pnl_now": r(self.pnl_now),
                "hold_ev": r(self.hold_ev)}


def evaluate_exit(*, entry_price: float, shares: float, cost_basis: float, fair: float,
                  bids: list[tuple[float, float]], fee_schedule: dict | None,
                  hold_buffer: float, stop_threshold: float, take_profit: float,
                  lock_remaining_ev: float, blocked_reason: str | None = None) -> ExitAdvice:
    """Decide SELL vs HOLD.

    `fair` = model P(held side wins). Selling now yields `exit_net` per share;
    holding is worth `fair` per share in expectation. Rules, in order:
      1. stop:       fair < entry - stop_threshold          (the move reversed)
      2. edge gone:  exit_net >= fair - hold_buffer         (market pays fair value)
      3. lock:       exit_net >= entry + take_profit and fair - exit_net < lock_remaining_ev
      else HOLD while the edge persists.
    """
    hold_ev = shares * fair - cost_basis
    if not bids or shares <= 0:
        return ExitAdvice("HOLD", "no bids to sell into - hold to resolution", fair, None, None,
                          None, None, hold_ev)
    fill = walk_sell(bids, shares, fee_schedule)
    if fill.shares <= 0:
        return ExitAdvice("HOLD", "no bids to sell into", fair, None, None, None, None, hold_ev)
    exit_net = (fill.cost - fill.fee) / fill.shares
    value_now = exit_net * shares if fill.complete else (fill.cost - fill.fee)
    pnl_now = value_now - cost_basis
    thin = "" if fill.complete else " (book too thin to exit fully)"

    def adv(action: str, reason: str) -> ExitAdvice:
        return ExitAdvice(action, reason + thin, fair, fill.avg_price, exit_net, value_now,
                          pnl_now, hold_ev)

    if blocked_reason:
        return adv("SELL", f"data unreliable ({blocked_reason}) - consider exiting")
    if fair < entry_price - stop_threshold:
        return adv("SELL", f"stop: fair value {fair:.2f} fell more than {stop_threshold:.2f} "
                           f"below entry {entry_price:.2f}")
    if exit_net >= fair - hold_buffer:
        return adv("SELL", f"edge gone: bid nets {exit_net:.2f} vs fair {fair:.2f}")
    if exit_net >= entry_price + take_profit and fair - exit_net < lock_remaining_ev:
        return adv("SELL", f"lock profit: +{exit_net - entry_price:.2f}/share, only "
                           f"{fair - exit_net:.2f} edge left to hold for")
    return adv("HOLD", f"edge persists: fair {fair:.2f} vs bid {exit_net:.2f} "
                       f"(+{fair - exit_net:.2f}/share by holding)")

"""Pure decision functions for the auto-trader (no I/O, fully unit-tested).

Fees: takers pay shares * rate * p * (1 - p); makers pay 0.
  Polymarket US (docs.polymarket.us/fees, checked 2026-11-09): rate = the market's
  feeCoefficient (0.095 at Tier 1), rounded to the cent per fill, half-to-even.
  International: rate 0.07 on crypto, unrounded.
Entries are post-only limit orders (maker, fee 0). Exits are immediate-or-cancel
limit sells (taker, fee charged). All P&L below includes both.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..fees import fee_per_share, fee_usdc, walk_sell


def round_down(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


def round_price(p: float, tick: float) -> float:
    return round(round(p / tick) * tick, 6)


@dataclass
class EntryPlan:
    ok: bool
    reason: str
    price: float = 0.0
    shares: float = 0.0
    cost: float = 0.0
    post_only: bool = True
    expected_fee: float = 0.0


def plan_entry(*, best_bid: float | None, best_ask: float | None, stake_usd: float, tick: float,
               min_size: float, size_step: float = 0.01, style: str = "maker", fee_schedule: dict | None = None,
               max_price: float = 0.97) -> EntryPlan:
    """Choose a limit price and size for a BUY.

    maker: post-only at one tick above the best bid if that still rests below the
           ask (gets queue priority), else at the best bid. Fee 0, may not fill.
    taker: limit at the best ask (crosses now, pays the taker fee, never worse than ask).
    """
    if best_ask is None:
        return EntryPlan(False, "no asks in book")
    if style == "taker":
        price, post_only = best_ask, False
    else:
        if best_bid is None:
            price = best_ask - tick
        else:
            price = best_bid + tick if best_bid + tick < best_ask - 1e-9 else best_bid
        post_only = True
    price = round_price(price, tick)
    if price <= 0 or price > max_price:
        return EntryPlan(False, f"entry price {price:.2f} outside (0, {max_price}]")
    shares = round(round_down(stake_usd / price, size_step), 2)
    if shares < min_size:
        return EntryPlan(False, f"${stake_usd:g} buys {shares:.2f} shares < market minimum {min_size:g}")
    fee = 0.0 if post_only else fee_usdc(shares, price, fee_schedule)
    return EntryPlan(True, "", price, shares, round(shares * price, 6), post_only, fee)


@dataclass
class Valuation:
    shares_sellable: float
    proceeds: float           # gross USDC from selling into current bids
    fee: float                # taker fee on that sale
    net: float                # proceeds - fee
    net_per_share: float
    pnl: float                # net - cost_basis (only the sellable part counts)
    pnl_pct: float
    complete: bool            # whole position sellable at current depth


def value_position(*, shares: float, cost_basis: float, bids: list[tuple[float, float]],
                   fee_schedule: dict | None, floor_price: float = 0.0) -> Valuation:
    """What the position is worth if sold NOW into the visible bids (fees included).
    Shares the book can't absorb are valued at 0: only actually sellable profit counts."""
    usable = [(p, s) for p, s in bids if p >= floor_price]
    f = walk_sell(usable, shares, fee_schedule)
    net = f.cost - f.fee
    pnl = net - cost_basis
    return Valuation(f.shares, f.cost, f.fee, net, net / f.shares if f.shares else 0.0, pnl,
                     pnl / cost_basis if cost_basis else 0.0, f.complete)


@dataclass
class ExitDecision:
    action: str               # "SELL" | "HOLD"
    reason: str
    trigger: str = ""         # take_profit | edge_gone | stop_loss | prob_flip | time_exit | hold_to_resolution


def evaluate_bot_exit(*, val: Valuation, fair: float | None, seconds_left: float, entry_price: float,
                      take_profit_pct: float, stop_loss_pct: float, flip_threshold: float,
                      time_exit_s: float, hold_min_ev: float, model_ok: bool = True,
                      seconds_since_fill: float = 1e9, stop_loss_grace_s: float = 0.0,
                      hard_stop_pct: float = 0.25) -> ExitDecision:
    """Small wins, fast losses.

    Order of checks:
      1. stop loss      pnl% (sellable, after fees) <= -stop_loss_pct
      2. prob flip      model P(held side) < flip_threshold            (needs model_ok)
      3. take profit    pnl% >= take_profit_pct
      4. edge gone      net sell price per share >= model fair value   (needs model_ok)
      5. time exit      in the final `time_exit_s`: hold to resolution only if
                        expected value of holding beats selling now by > hold_min_ev
      else HOLD.
    When data is degraded (model_ok False) only the price-based rules (1, 3) apply,
    and the time exit sells.
    """
    if val.shares_sellable <= 0:
        return ExitDecision("HOLD", "no bids to sell into")
    if seconds_since_fill < stop_loss_grace_s:
        # optional grace period: right after a fill the spread + exit fee alone can be ~-10%;
        # only a hard stop applies until the grace period ends
        if val.pnl_pct <= -hard_stop_pct:
            return ExitDecision("SELL", f"hard stop in grace period: {val.pnl_pct:+.1%} after fees",
                                "stop_loss")
        if val.pnl_pct >= take_profit_pct:
            return ExitDecision("SELL", f"take profit: {val.pnl_pct:+.1%} after fees "
                                        f"(target +{take_profit_pct:.0%})", "take_profit")
        return ExitDecision("HOLD", f"grace period ({stop_loss_grace_s - seconds_since_fill:.0f}s left): "
                                    f"P&L {val.pnl_pct:+.1%}")
    if val.pnl_pct <= -stop_loss_pct:
        return ExitDecision("SELL", f"stop loss: {val.pnl_pct:+.1%} after fees "
                                    f"(limit -{stop_loss_pct:.0%})", "stop_loss")
    if model_ok and fair is not None and fair < flip_threshold:
        return ExitDecision("SELL", f"model flipped: P(win) {fair:.0%} < {flip_threshold:.0%}",
                            "prob_flip")
    if val.pnl_pct >= take_profit_pct:
        return ExitDecision("SELL", f"take profit: {val.pnl_pct:+.1%} after fees "
                                    f"(target +{take_profit_pct:.0%})", "take_profit")
    if model_ok and fair is not None and val.net_per_share >= fair:
        return ExitDecision("SELL", f"edge gone: bid nets {val.net_per_share:.3f} >= fair {fair:.3f}",
                            "edge_gone")
    if seconds_left <= time_exit_s:
        if not model_ok or fair is None:
            return ExitDecision("SELL", "time exit: model unavailable near close", "time_exit")
        hold_ev = fair - val.net_per_share       # per share: E[payout] - sell-now value
        if hold_ev > hold_min_ev and val.complete:
            return ExitDecision("HOLD", f"holding to resolution: E[win] {fair:.2f} vs sell "
                                        f"{val.net_per_share:.2f} (+{hold_ev:.3f}/share)",
                                "hold_to_resolution")
        return ExitDecision("SELL", f"time exit: holding EV {hold_ev:+.3f}/share not worth it",
                            "time_exit")
    return ExitDecision("HOLD", f"P&L {val.pnl_pct:+.1%}; TP +{take_profit_pct:.0%} / "
                                f"SL -{stop_loss_pct:.0%}")


def trigger_levels(*, shares: float, cost_basis: float, take_profit_pct: float, stop_loss_pct: float,
                   fee_schedule: dict | None, tick: float = 0.01) -> tuple[float, float]:
    """Approximate bid prices at which TP and SL fire (for display), fees included."""
    def bid_for(target_net: float) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(40):
            mid = (lo + hi) / 2
            net = shares * (mid - fee_per_share(mid, fee_schedule))
            lo, hi = (mid, hi) if net < target_net else (lo, mid)
        return hi
    tp = bid_for(cost_basis * (1 + take_profit_pct))
    sl = bid_for(cost_basis * (1 - stop_loss_pct))
    return round(math.ceil(tp / tick - 1e-9) * tick, 4), round(math.floor(sl / tick + 1e-9) * tick, 4)

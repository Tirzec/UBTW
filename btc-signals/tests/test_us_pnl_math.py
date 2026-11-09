"""Polymarket US P&L math (docs.polymarket.us, checked 2026-11-09): per-fill banker's
rounding of taker fees, tick-exact order prices (NFL 0.0025 / 0.005), contract steps, IOC exits."""
import asyncio

import pytest

from app.fees import bankers_cents, fee_usdc, walk_buy, walk_sell
from app.trading.broker import PaperBroker
from app.trading.rules import plan_entry
from app.trading.us_broker import format_price, order_body

US = {"rate": 0.095, "exponent": 1, "takerOnly": True, "round_cents": True}


def test_bankers_rounding_half_to_even():
    assert bankers_cents(0.025) == 0.02
    assert bankers_cents(0.035) == 0.04
    assert bankers_cents(0.0049) == 0.0
    assert bankers_cents(0.005) == 0.0          # half -> even (0)
    assert bankers_cents(0.015) == 0.02


def test_us_fee_rounded_per_fill_intl_exact():
    # 10 contracts @ 0.50: 0.095*10*0.25 = 0.2375 -> $0.24
    assert fee_usdc(10, 0.50, US) == 0.24
    # 1 contract @ 0.50: 0.02375 -> $0.02 ; tiny fills round to $0.00
    assert fee_usdc(1, 0.50, US) == 0.02
    assert fee_usdc(0.1, 0.50, US) == 0.0
    intl = {"rate": 0.07, "exponent": 1}
    assert fee_usdc(10, 0.5, intl) == pytest.approx(0.175)     # international: unrounded


def test_walk_rounds_each_level_separately():
    f = walk_buy([(0.50, 1), (0.51, 1)], 2, US)
    assert f.fee == pytest.approx(0.02 + 0.02)
    s = walk_sell([(0.50, 10)], 10, US)
    assert s.fee == 0.24


def test_nfl_ticks_format_exactly():
    assert format_price(0.4925, 0.0025) == "0.4925"
    assert format_price(0.4930, 0.0025) == "0.4925"            # snapped to the tick
    assert format_price(0.515, 0.005) == "0.515"
    assert format_price(0.50, 0.01) == "0.50"
    assert format_price(0.999, 0.01) == "0.99"                 # exchange limits 0.01-0.99


def test_short_side_converted_on_tick():
    b = order_body("nfl-x:short", "BUY", 0.4925, 3, True, tick=0.0025)
    assert b["price"]["value"] == "0.5075" and b["intent"] == "ORDER_INTENT_BUY_SHORT"
    b = order_body("nfl-x:short", "SELL", 0.485, 3, False, tick=0.005)
    assert b["price"]["value"] == "0.515" and b["intent"] == "ORDER_INTENT_SELL_SHORT"


def test_entries_rest_exits_are_ioc():
    assert order_body("m:long", "BUY", 0.5, 1, True)["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    exit_ = order_body("m:long", "SELL", 0.5, 1, False)
    assert exit_["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" and exit_["participateDontInitiate"] is False


def test_contract_step():
    p = plan_entry(best_bid=0.50, best_ask=0.52, stake_usd=1.0, tick=0.01, min_size=0.01)
    assert p.ok and p.shares == 1.96
    p = plan_entry(best_bid=0.50, best_ask=0.52, stake_usd=1.0, tick=0.01, min_size=1, size_step=1.0)
    assert p.ok and p.shares == 1.0
    p = plan_entry(best_bid=None, best_ask=0.52, stake_usd=1.0, tick=0.01, min_size=0.01,
                   style="taker", fee_schedule=US)
    assert p.expected_fee == fee_usdc(p.shares, 0.52, US)


def test_paper_ioc_never_rests():
    book = {"bids": [(0.48, 5)], "asks": [(0.50, 2)]}
    b = PaperBroker(lambda t: book, lambda t, s: [], lambda t: US, ioc_non_post_only=True)
    o = asyncio.run(b.place_limit(token="m:long", side="BUY", price=0.50, size=5, post_only=False))
    assert o.status == "canceled" and o.filled == 2 and o.fee == 0.05      # 0.095*2*.25=0.0475
    o = asyncio.run(b.place_limit(token="m:long", side="BUY", price=0.49, size=5, post_only=True))
    assert o.status == "live"                                             # maker entries still rest

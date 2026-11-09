import pytest

from app.exits import evaluate_exit
from app.fees import fee_per_share, fee_usdc, walk_buy, walk_sell
from app.signal import decide

SCHED = {"rate": 0.07, "exponent": 1, "takerOnly": True}


def test_fee_matches_docs_table():
    # docs.polymarket.com/trading/fees, crypto, 100 shares
    assert fee_usdc(100, 0.50, SCHED) == pytest.approx(1.75)
    assert fee_usdc(100, 0.10, SCHED) == pytest.approx(0.63)
    assert fee_usdc(100, 0.90, SCHED) == pytest.approx(0.63)
    assert fee_usdc(100, 0.01, SCHED) == pytest.approx(0.0693)
    assert fee_per_share(0.3, SCHED) == pytest.approx(fee_per_share(0.7, SCHED))


def test_walk_buy_multiple_levels():
    f = walk_buy([(0.50, 20), (0.52, 100)], 20.0, SCHED)
    assert f.shares == pytest.approx(20 + (20 - 10) / 0.52)
    assert f.cost == pytest.approx(20.0)
    assert f.complete and f.worst_price == 0.52


def test_walk_buy_thin_book():
    f = walk_buy([(0.5, 10)], 50.0, SCHED)
    assert not f.complete and f.shares == 10


def test_walk_sell():
    f = walk_sell([(0.6, 5), (0.55, 100)], 10, SCHED)
    assert f.cost == pytest.approx(5 * 0.6 + 5 * 0.55)
    assert f.complete


def book(bid, ask, size=1000):
    return {"bids": [(bid, size)], "asks": [(ask, size)]}


def test_decide_trades_only_with_edge_after_costs():
    books = {"UP": book(0.54, 0.55), "DOWN": book(0.45, 0.46)}
    # p=0.62: edge = 0.62 - 0.55 - fee(0.0173) - 0.04 = +0.0127
    d = decide(0.62, books, buffer=0.04, stake_usd=50, max_spread=0.04, fee_schedule=SCHED, gates=[])
    assert d.action == "UP" and d.edge == pytest.approx(0.62 - 0.55 - 0.07 * 0.55 * 0.45 - 0.04)
    # p=0.60: edge negative -> NO TRADE
    d = decide(0.60, books, buffer=0.04, stake_usd=50, max_spread=0.04, fee_schedule=SCHED, gates=[])
    assert d.action == "NO TRADE" and d.best_side == "UP"


def test_decide_down_side():
    books = {"UP": book(0.69, 0.70), "DOWN": book(0.30, 0.31)}
    d = decide(0.55, books, buffer=0.04, stake_usd=50, max_spread=0.04, fee_schedule=SCHED, gates=[])
    assert d.action == "DOWN"


def test_decide_gates_block():
    books = {"UP": book(0.54, 0.55), "DOWN": book(0.45, 0.46)}
    d = decide(0.9, books, buffer=0.04, stake_usd=50, max_spread=0.04, fee_schedule=SCHED,
               gates=["DEGRADED DATA: gap"])
    assert d.action == "NO TRADE" and "DEGRADED DATA: gap" in d.blocked_by


def test_decide_wide_spread_and_thin_book():
    d = decide(0.9, {"UP": book(0.40, 0.55), "DOWN": book(0.45, 0.60)}, buffer=0.04, stake_usd=50,
               max_spread=0.04, fee_schedule=SCHED, gates=[])
    assert d.action == "NO TRADE" and any("spread" in b for b in d.blocked_by)
    d = decide(0.9, {"UP": book(0.54, 0.55, size=5), "DOWN": book(0.45, 0.46, size=5)}, buffer=0.04,
               stake_usd=50, max_spread=0.04, fee_schedule=SCHED, gates=[])
    assert d.action == "NO TRADE" and any("thin" in b for b in d.blocked_by)


def test_decide_no_book():
    d = decide(0.9, {"UP": None, "DOWN": None}, buffer=0.04, stake_usd=50, max_spread=0.04,
               fee_schedule=SCHED, gates=[])
    assert d.action == "NO TRADE" and "no order book" in d.blocked_by


EX = dict(fee_schedule=SCHED, hold_buffer=0.01, stop_threshold=0.15, take_profit=0.20,
          lock_remaining_ev=0.03)


def test_exit_hold_when_edge_persists():
    a = evaluate_exit(entry_price=0.55, shares=100, cost_basis=55, fair=0.80,
                      bids=[(0.65, 1000)], **EX)
    assert a.action == "HOLD" and "edge persists" in a.reason


def test_exit_sell_when_edge_gone():
    a = evaluate_exit(entry_price=0.55, shares=100, cost_basis=55, fair=0.70,
                      bids=[(0.72, 1000)], **EX)
    assert a.action == "SELL" and a.reason.startswith("edge gone")


def test_exit_stop():
    a = evaluate_exit(entry_price=0.60, shares=100, cost_basis=60, fair=0.40,
                      bids=[(0.30, 1000)], **EX)
    assert a.action == "SELL" and a.reason.startswith("stop")


def test_exit_lock_profit():
    a = evaluate_exit(entry_price=0.50, shares=100, cost_basis=50, fair=0.93,
                      bids=[(0.92, 1000)], **{**EX, "hold_buffer": 0.0})
    # exit_net = 0.92 - fee(0.92) = 0.9148 ; fair - exit_net = 0.015 < 0.03
    assert a.action == "SELL" and a.reason.startswith(("lock", "edge gone"))


def test_exit_degraded_data_advises_sell():
    a = evaluate_exit(entry_price=0.5, shares=10, cost_basis=5, fair=0.8, bids=[(0.6, 100)],
                      blocked_reason="gap 30 bps", **EX)
    assert a.action == "SELL" and "data unreliable" in a.reason


def test_exit_no_bids():
    a = evaluate_exit(entry_price=0.5, shares=10, cost_basis=5, fair=0.8, bids=[], **EX)
    assert a.action == "HOLD"

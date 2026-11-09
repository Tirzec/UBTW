"""Entry cost guard (skip trades that lose money the moment they fill) and paper-count restart."""
from app.trading.store import TradeStore, paper_count_from
from app.trading.trader import entry_cost_guard
from app.db import DB

US = {"rate": 0.095, "exponent": 1, "takerOnly": True, "round_cents": True}
EC = {"max_spread": 0.02, "min_price": 0.10, "max_price": 0.90, "min_edge_after_exit_cost": 0.01}


def g(**kw):
    base = dict(plan_price=0.50, fair=0.60, best_bid=0.49, best_ask=0.51, fee_schedule=US)
    base.update(kw)
    return entry_cost_guard(EC, **base)


def test_good_trade_passes():
    # exit cost = 0.01 + 0.095*0.49*0.51 = 0.0337; net = 0.10 - 0.0337 = 0.066 >= 0.01
    assert g() == ""


def test_wide_spread_blocked():                # the 0.71 / 0.77 book seen in preflight
    assert "spread 0.06" in g(best_bid=0.71, best_ask=0.77, plan_price=0.72, fair=0.85)


def test_cheap_and_expensive_contracts_blocked():   # the 0.03 entry that lost 39.5%
    assert "< 0.10" in g(plan_price=0.03, best_bid=0.02, best_ask=0.04, fair=0.10)
    assert "> 0.90" in g(plan_price=0.92, best_bid=0.91, best_ask=0.93, fair=0.99)


def test_edge_must_survive_selling_back_at_bid():
    # fair 0.53 vs price 0.50: +0.03 edge, but exiting costs ~0.034/share -> skip
    assert "doesn't cover" in g(fair=0.53)
    assert "no bid" in g(best_bid=None)


def test_guard_off_without_config():
    assert entry_cost_guard({}, plan_price=0.03, fair=0.0, best_bid=0.0, best_ask=0.5, fee_schedule=US) == ""


def test_paper_count_from(tmp_path):
    assert paper_count_from({}) == 0.0
    t = paper_count_from({"paper_count_from": "2026-11-09T21:25:00Z"})
    assert t == 1794259500.0
    d = DB(tmp_path / "p.db")
    st = TradeStore(d, "paper", "polymarket_us", "crypto_updown")
    for i, opened in enumerate([t - 60, t + 5, t + 100]):
        d.insert("bot_positions", {"mode": "paper", "status": "closed", "pnl": 0.0, "cost": 1, "entry_fee": 0,
                                   "exit_fee": 0, "opened_ts": opened, "closed_ts": opened + 30,
                                   "venue": "polymarket_us", "module": "crypto_updown"})
    assert len(st.counted_paper_trades(t)) == 2 and len(st.counted_paper_trades()) == 3

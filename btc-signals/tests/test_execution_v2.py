"""Execution rules v2 (2026-11-10): book-driven entries, 3 s re-quote, entry latency alert, signal log,
hold-winners profit lock (max(entry + 25c, fair + 3c) capped at 95c, moved as fair value moves),
no +6c profit take, and the one-time old-vs-new comparison after 50 trades."""
import asyncio
import time

import pytest

from app.execution_report import analyse, compare_text, format_report
from app.ingest.polymarket_ws import BookStore
from app.trading.trader import RULES_VERSION, AutoTrader
from app.trading.store import TradeStore
from tests.test_crypto_fair import _module, _val, book
from tests.test_lock_lifecycle import FakeLive, _cfg, _open_position
from tests.test_trading import FakeEngine

V2 = {"profit_lock_price": 0.95, "profit_lock_offset": 0.25, "profit_lock_fair_margin": 0.03,
      "profit_lock_reprice_ticks": 2, "profit_lock_reprice_s": 3}


def _setup(tmp_path, ex=None, entry=None):
    e = FakeEngine(tmp_path, int(time.time()) - 60)
    b = FakeLive(e)
    cfg = _cfg(**{**V2, **(ex or {})})
    cfg["trading"]["entry"].update(entry or {})
    t = AutoTrader(e, cfg, b, TradeStore(e.db, "live"))
    return e, b, t


def test_lock_price_is_max_of_entry_plus_25_and_fair_plus_3_capped_at_95(tmp_path):
    e, b, t = _setup(tmp_path)
    m = e.win
    t.module.fair = lambda p: (None, False)
    assert t._lock_price({"entry_price": 0.60}, m) == pytest.approx(0.85)       # fair unknown: entry + 25c
    t.module.fair = lambda p: (0.70, True)
    assert t._lock_price({"entry_price": 0.60}, m) == pytest.approx(0.85)       # 0.85 > 0.73
    t.module.fair = lambda p: (0.881, True)
    assert t._lock_price({"entry_price": 0.60}, m) == pytest.approx(0.92)       # fair + 3c, rounded UP to a tick
    t.module.fair = lambda p: (0.99, True)
    assert t._lock_price({"entry_price": 0.60}, m) == pytest.approx(0.95)       # cap
    t.module.fair = lambda p: (0.50, True)
    assert t._lock_price({"entry_price": 0.75}, m) == pytest.approx(0.95)       # entry + 25c capped


def test_lock_moves_with_fair_value_via_the_safe_cancel(tmp_path):
    e, b, t = _setup(tmp_path)

    async def run():
        now = time.time()
        t.module.fair = lambda p: (0.60, True)
        p = await _open_position(e, t, now)                     # entry 0.51 -> lock max(0.76, 0.63) = 0.76
        old = p["lock_order"]
        assert b.orders[old].price == pytest.approx(0.76)
        t.module.fair = lambda p: (0.85, True)                  # target 0.88: 12 ticks away
        await t.tick(now + 1.5)                                 # too soon after placing (< 3 s): no churn
        assert t.store.position(p["id"])["lock_order"] == old
        await t.tick(now + 4.0)                                 # moved: cancel confirmed, new lock placed
        p = t.store.position(p["id"])
        await t.tick(now + 4.25)
        p = t.store.position(p["id"])
        assert p["lock_order"] and p["lock_order"] != old
        assert b.ex[old]["state"] == "canceled"
        assert b.orders[p["lock_order"]].price == pytest.approx(0.88)
        t.module.fair = lambda p: (0.855, True)                 # target 0.89: one tick: leave it alone
        await t.tick(now + 9.0)
        assert t.store.position(p["id"])["lock_order"] == p["lock_order"]
        live = [o for o in b.ex.values() if o["state"] == "live" and o["price"] > 0.7]
        assert len(live) == 1                                   # never two resting sells at once
    asyncio.run(run())


def test_lock_reprice_waits_for_cancel_confirmation(tmp_path):
    e, b, t = _setup(tmp_path)

    async def run():
        now = time.time()
        t.module.fair = lambda p: (0.60, True)
        p = await _open_position(e, t, now)
        old = p["lock_order"]
        b.pending_cancel_reads[old] = 10                        # the exchange is slow to confirm
        t.module.fair = lambda p: (0.85, True)
        await t.tick(now + 4.0)
        p = t.store.position(p["id"])
        assert p["lock_order"] == old                           # not confirmed: no second sell order
        assert [o.order_id for o in b.orders.values() if o.side == "SELL"] == [old]
    asyncio.run(run())


def test_profit_take_6c_is_off_and_winners_are_held():
    now = time.time()
    m, w = _module(now, 0.80, book(0.70, 0.72), book(0.28, 0.30))
    m.xc_exits = {"profit_take_gain": "off", "sell_if_fair_below": 0.30, "lock_in_bid": 0.93}
    pos = {"slug": w.slug, "side": "UP", "token": w.token_up, "entry_price": 0.60}
    d = m.exit_decision(pos, _val(0.68), now)                 # bid 70c = entry + 10c, fair 0.80 > bid
    assert d.action == "HOLD" and d.trigger == "hold_to_resolution"
    m.xc_exits["profit_take_gain"] = 0.06
    m.fair_now[w.slug] = type(m.fair_now[w.slug])(0.70, 100, 1, 0, 100, 1)
    assert m.exit_decision(pos, _val(0.68), now).trigger == "profit_take"     # old rule still switchable


def test_book_store_notifies_listeners():
    bs = BookStore()
    hits = []
    bs.listeners.append(lambda: hits.append(1))
    bs.listeners.append(lambda: 1 / 0)                         # a broken listener never breaks the feed
    bs.apply({"asset_id": "A", "bids": [{"price": "0.5", "size": "1"}], "asks": [{"price": "0.6", "size": "1"}]})
    assert hits == [1]


def test_entry_is_placed_on_a_book_update_not_a_timer(tmp_path):
    e, b, t = _setup(tmp_path, entry={"min_signal_gap_s": 0.0})

    async def run():
        e.signal("UP", 0.70)
        await t.start()
        try:
            await asyncio.sleep(0.05)
            n0 = len(t.store.active_positions())
            e.book("UP", 0.50, 0.54)                            # websocket update -> entry check right away
            for _ in range(50):
                await asyncio.sleep(0.01)
                if t.store.active_positions():
                    break
            assert n0 == 0 and len(t.store.active_positions()) == 1
        finally:
            await t.stop()
    asyncio.run(run())


def test_unfilled_entry_cancelled_after_3s_then_requoted(tmp_path):
    e, b, t = _setup(tmp_path, entry={"order_timeout_s": 3, "reentry_cooldown_s": 20, "max_entries_per_window": 1})

    async def run():
        now = time.time()
        e.book("UP", 0.50, 0.54)
        e.signal("UP", 0.70)
        await t.tick(now)
        p1, = t.store.active_positions()
        await t.tick(now + 2.0)
        assert t.store.position(p1["id"])["status"] == "opening"          # still inside 3 s
        await t.tick(now + 3.5)
        assert t.store.position(p1["id"])["status"] == "void"            # cancelled unfilled
        await t.tick(now + 3.75)                                          # edge still there -> re-quote
        act = t.store.active_positions()
        assert len(act) == 1 and act[0]["id"] != p1["id"]
    asyncio.run(run())


def test_slow_entries_alert(tmp_path):
    e, b, t = _setup(tmp_path)
    from types import SimpleNamespace
    now = time.time()
    for i in range(5):
        sig = SimpleNamespace(extra={"book_ts": now - 0.5})
        t._record_entry_latency(i, sig, now - 0.45, now)
    assert any("Slow entries" in a[1] for a in e.alerts)
    e.alerts.clear()
    t._record_entry_latency(9, SimpleNamespace(extra={"book_ts": now - 0.5}), now, now)
    assert not e.alerts                                                   # at most hourly


def _pos(store, **kw):
    base = dict(slug="btc-updown-5m-2026-11-10-1200", duration="5m", side="UP", token="UP", status="closed",
                strategy="fair_value", venue="polymarket_us", opened_ts=time.time() - 100, filled_ts=time.time() - 99,
                closed_ts=time.time() - 10, shares=10, cost=6.0, entry_fee=0.0, entry_price=0.60)
    base.update(kw)
    return store.new_position(**base)


def test_report_compares_old_and_new_rules(tmp_path):
    e = FakeEngine(tmp_path, int(time.time()) - 60)
    st = TradeStore(e.db, "live", venue="polymarket_us")
    _pos(st, exit_shares=10, exit_proceeds=7.0, exit_trigger="profit_take", pnl=1.0, side_won=1)   # old: left 3.0
    _pos(st, pnl=-6.0, side_won=0, exit_trigger="resolution")
    _pos(st, rules=RULES_VERSION, pnl=4.0, side_won=1, exit_trigger="resolution", payout=10)
    _pos(st, rules=RULES_VERSION, status="void", entry_order="x")
    st.signal(slug="btc-updown-15m-2026-11-10-1200", side="UP", edge=0.06, decision="skipped",
              reason="already holding #3", rules=RULES_VERSION)
    a = analyse(e.db, mode="live", days=1)
    assert a["exits"]["old rules"]["avg_left_on_table"] == pytest.approx(3.0)
    assert a["results"]["old rules"]["avg_loss"] == pytest.approx(-6.0)
    new = [k for k in a["results"] if k != "old rules"][0]
    assert a["results"][new]["avg_win"] == pytest.approx(4.0)
    assert a["entries"][new]["unfilled_cancelled"] == 1
    assert a["entries"][new]["signals"]["windows_missed"] == 1
    txt = compare_text(a)
    assert "old rules" in txt and "new rules" in txt
    assert "OLD vs NEW" in format_report(a)


def test_rules_report_alert_once_after_n_trades(tmp_path):
    e, b, t = _setup(tmp_path)
    t.tc["rules_report_after"] = 2
    st = t.store
    ids = [_pos(st, rules=RULES_VERSION, pnl=1.0, side_won=1, exit_trigger="resolution", mode="live")
           for _ in range(2)]
    t._rules_report(st.position(ids[0]))
    assert sum("old vs new" in a[1] for a in e.alerts) == 1
    t._rules_report(st.position(ids[1]))
    assert sum("old vs new" in a[1] for a in e.alerts) == 1                # only once

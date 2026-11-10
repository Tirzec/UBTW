"""Real-money copy trading: wiring, hard caps, leftover settlement, confirmation screen."""
import asyncio
import base64
import os

import pytest

from app.db import DB
from app.engine import Engine
from app.trading.store import TradeStore


class _FakeCopy:
    traders = [{"name": "T", "wallet": "0xaa"}]

    def __init__(self, engine, cfg, http):
        self.settled = {}
        self.calls = []
        self.books = type("B", (), {"get": staticmethod(lambda k: None),
                                    "trades_since": staticmethod(lambda k, s: [])})()
        self.name = "copytrade"

    def attach(self, store, trader=None):
        self.store, self.trader = store, trader

    def market(self, slug):
        return None

    async def start(self):
        pass

    async def refresh_settlement(self, slug):
        self.calls.append(slug)
        return self.settled.get(slug)


def _engine(tmp_path, monkeypatch, trade_mode, cc_mode, need=0, paper_done=0):
    import app.copytrade.module as cm
    from app.trading.trader import AutoTrader
    monkeypatch.setattr(cm, "CopyTradeModule", _FakeCopy)

    async def _nostart(self):
        return None
    monkeypatch.setattr(AutoTrader, "start", _nostart)
    e = Engine.__new__(Engine)
    e.cfg = {"copytrade": {"enabled": True, "mode": cc_mode, "min_paper_trades_for_live": need, "risk": {}},
             "_live_confirmed": {"key_id": "k", "secret_key": base64.b64encode(os.urandom(32)).decode()}}
    e.venue, e.trade_mode, e.http, e.global_risk, e.traders = "polymarket_us", trade_mode, None, None, {}
    e.db = DB(str(tmp_path / "t.db"))
    e.trade_store = TradeStore(e.db, trade_mode, "polymarket_us")
    e.alerts = []
    e.alert = lambda *a, **k: e.alerts.append(a)
    st = TradeStore(e.db, "paper", "polymarket_us", "copytrade")
    for i in range(paper_done):
        pid = st.new_position(slug=f"s{i}", duration="T", side="x", token=f"s{i}:long", status="closed",
                              opened_ts=2e9, entry_price=0.5)
        st.update_position(pid, closed_ts=2e9, pnl=0.1)
    return e


@pytest.mark.parametrize("trade_mode,cc_mode,need,done,expect", [
    ("live", "live", 0, 0, "live"),          # the user's choice: paper requirement waived
    ("paper", "live", 0, 0, "paper"),        # no typed confirmation / trading.mode paper -> never live
    ("live", "paper", 0, 0, "paper"),        # copytrade.mode paper -> paper
    ("live", "live", 10, 3, "paper"),        # a paper requirement is still honoured when set
    ("live", "live", 2, 3, "live"),
])
def test_copy_trader_goes_live_only_when_every_switch_is_on(tmp_path, monkeypatch, trade_mode, cc_mode, need,
                                                             done, expect):
    pytest.importorskip("nacl")
    e = _engine(tmp_path, monkeypatch, trade_mode, cc_mode, need, done)
    asyncio.run(e._start_copytrade())
    t = e.traders["copytrade"]
    assert t.mode == expect and t.store.mode == expect
    if expect == "live":
        from app.trading.us_broker import UsLiveBroker
        assert isinstance(t.broker, UsLiveBroker)
        assert t.risk_key == "live:risk:copytrade"            # paper streaks/pauses never carry into live


def test_phase1_caps_copy_trading_too():
    from app.trading.live_phase import apply_phase1
    cfg = {"copytrade": {"stake_usd": 5, "entry": {"stake_usd": 5}, "risk": {"max_stake_usd": 5,
                                                                             "daily_loss_limit_usd": 50}}}
    apply_phase1(cfg)
    cc = cfg["copytrade"]
    assert cc["stake_usd"] == 1.0 and cc["entry"]["stake_usd"] == 1.0
    assert cc["risk"]["max_stake_usd"] == 1.0 and cc["risk"]["daily_loss_limit_usd"] == 10.0


class _FakeStatus:
    def __init__(self, settled=None, closed=()):
        self.settled_map, self.closed_set = dict(settled or {}), set(closed)

    async def settlement(self, slug):
        return self.settled_map.get(slug)

    async def closed(self, slug):
        return slug in self.closed_set


class _LiveT:
    mode = "live"


class _PaperT:
    mode = "paper"


def test_leftover_positions_of_a_switched_off_module_are_settled(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, "live", "live")
    e.us_status = _FakeStatus({"btc-a": 1.0, "btc-b": 1.0, "cp-x": 0.0})   # long won a, b; c not settled
    e.traders = {"copytrade": _LiveT()}
    crypto = TradeStore(e.db, "live", "polymarket_us", "crypto_updown")
    won = crypto.new_position(slug="btc-a", duration="5m", side="Up", token="btc-a:long", status="open",
                              opened_ts=1.0, entry_price=0.4)
    crypto.update_position(won, shares=2.5, cost=1.0, entry_fee=0.02)
    lost = crypto.new_position(slug="btc-b", duration="5m", side="Down", token="btc-b:short", status="resolving",
                               opened_ts=1.0, entry_price=0.5)
    crypto.update_position(lost, shares=2.0, cost=1.0, entry_fee=0.02)
    waiting = crypto.new_position(slug="btc-c", duration="5m", side="Up", token="btc-c:long", status="open",
                                  opened_ts=1.0, entry_price=0.5)
    never = crypto.new_position(slug="btc-a", duration="5m", side="Up", token="btc-a:long", status="opening",
                                opened_ts=1.0, entry_price=0.5)                 # no order recorded: void
    copy = TradeStore(e.db, "live", "polymarket_us", "copytrade")
    mine = copy.new_position(slug="btc-a", duration="T", side="x", token="btc-a:long", status="open",
                             opened_ts=1.0, entry_price=0.5)
    done = asyncio.run(e.settle_leftovers(now=5.0))
    assert sorted(done) == sorted([won, lost])
    a, b = crypto.position(won), crypto.position(lost)
    assert a["status"] == "closed" and a["payout"] == pytest.approx(2.5) and a["pnl"] == pytest.approx(1.48)
    assert b["status"] == "closed" and b["payout"] == 0 and b["pnl"] == pytest.approx(-1.02)
    assert crypto.position(waiting)["status"] == "open"
    assert crypto.position(never)["status"] == "void"
    assert copy.position(mine)["status"] == "open"              # a LIVE module settles its own
    assert asyncio.run(e.settle_leftovers()) == []              # nothing twice
    # copy trading switched back to PAPER while LIVE copies are still open: they are leftovers now
    e.traders = {"copytrade": _PaperT()}
    cx = copy.new_position(slug="cp-x", duration="T", side="x", token="cp-x:short", status="open",
                           opened_ts=1.0, entry_price=0.5)
    copy.update_position(cx, shares=2.0, cost=1.0, entry_fee=0.0)
    assert set(asyncio.run(e.settle_leftovers())) == {mine, cx}
    assert copy.position(cx)["payout"] == pytest.approx(2.0)    # short side of a market that settled at 0


def test_reconciler_treats_closed_and_expired_markets_as_settling(tmp_path):
    from app.trading import reconcile as R

    class Eng:
        def __init__(self, st):
            self.trade_store, self.kills = st, []

        async def kill_all(self, reason):
            self.kills.append(reason)

        def alert(self, *a):
            pass

    class Brk:
        async def _req(self, method, path, body=None):
            if path == "/v1/portfolio/positions":
                return {"positions": {"exp": {"netPositionDecimal": "2", "expired": True}}}
            return {"orders": []}
    db = DB(str(tmp_path / "r.db"))
    st = TradeStore(db, "live", "polymarket_us", "copytrade")
    for slug in ("exp", "done", "live"):
        pid = st.new_position(slug=slug, duration="T", side="x", token=f"{slug}:long", status="open",
                              opened_ts=1.0, entry_price=0.5)
        st.update_position(pid, shares=2.0)
    eng = Eng(TradeStore(db, "live", "polymarket_us"))
    rc = R.Reconciler(eng, Brk(), db, closed_fn=_FakeStatus(closed={"done"}).closed)
    r = asyncio.run(rc.startup())
    assert len(r.mismatches) == 1 and r.mismatches[0].startswith("live:")   # open market: still a mismatch
    assert {"exp", "done"} <= set(r.skipped)


def test_failed_live_entry_never_leaves_a_stuck_row(tmp_path):
    from app.trading.broker import BrokerError, PaperBroker
    from app.trading.trader import AutoTrader
    from app.copytrade.module import CopyTradeModule

    class Eng:
        def alert(self, *a, **k):
            pass
    db = DB(str(tmp_path / "e.db"))
    st = TradeStore(db, "live", "polymarket_us", "copytrade")
    mod = CopyTradeModule(Eng(), {"copytrade": {"traders": [{"name": "T", "wallet": "0xaa"}]}}, client=object())
    br = PaperBroker(lambda k: None, lambda k, s: [], lambda k: {})
    t = AutoTrader(Eng(), {}, br, st, module=mod, exec_conf={"risk": {}})
    mod.attach(st, t)
    pid = st.new_position(slug="m", duration="T", side="x", token="m:long", status="opening", opened_ts=1.0,
                          entry_price=0.5)                          # e.g. place_limit raised: no entry_order
    asyncio.run(t._manage(st.position(pid), 2.0))
    assert st.position(pid)["status"] == "void"


def test_resting_copy_buy_is_cancelled_when_the_trader_sold_out():
    from app.copytrade.module import CopyTradeModule

    class Eng:
        def alert(self, *a, **k):
            pass
    mod = CopyTradeModule(Eng(), {"copytrade": {"traders": [{"name": "T", "wallet": "0xaa"}]}}, client=object())
    mod.keep = {"7": 0.0, "8": 0.5}
    assert mod.entry_abandoned({"id": 7}) and not mod.entry_abandoned({"id": 8})
    assert not mod.entry_abandoned({"id": 9})


def test_pause_state_is_kept_apart_for_paper_and_live():
    from app.copytrade.module import copy_mode, state_key
    assert state_key("copy:paused", "paper") == "copy:paused"
    assert state_key("copy:paused", "live") == "live:copy:paused"
    assert copy_mode({"trading": {"mode": "live"}, "copytrade": {"mode": "live"}}) == "live"
    assert copy_mode({"trading": {"mode": "paper"}, "copytrade": {"mode": "live"}}) == "paper"


def test_lost_sell_is_not_matched_to_another_orders_fills():
    from app.trading.broker import OrderState
    from app.trading.us_broker import UsLiveBroker, order_body
    b = UsLiveBroker("k", base64.b64encode(os.urandom(32)).decode(), lambda t: {})
    calls = []

    async def req(method, path, body=None):
        calls.append(path)
        if path == "/v1/orders/open":
            return {"orders": []}
        return {"activities": [{"trade": {"id": "t1", "marketSlug": "m", "price": {"value": "0.40"},
                                          "qtyDecimal": "2", "createTime": "2099-01-01T00:00:00Z"}}]}
    b._req = req
    resting = OrderState("buy1", "m:long", "BUY", 0.40, 2.0, False)       # a stacked copy's resting buy
    b.orders["buy1"] = resting
    o = OrderState("", "m:long", "SELL", 0.45, 2.0, False)
    body = order_body("m:long", "SELL", 0.45, 2.0, False)
    assert asyncio.run(b.find_lost_order(o, body, t0=0.0, settle_s=0, tries=1)) is None
    resting.status = "filled"
    resting.created = 0.0                                                 # long done: no longer ambiguous
    assert asyncio.run(b.find_lost_order(o, body, t0=1e9, settle_s=0, tries=1)) is None   # 0.40 < 0.45 limit
    asyncio.run(b.client.aclose())


def test_live_confirmation_shows_the_copy_trading_limits(capsys, monkeypatch):
    from app.trading import live_gate
    cfg = {"venue": "polymarket_us", "modules": {"crypto_updown": {"enabled": False}},
           "trading": {"live_trading_enabled": True, "min_paper_trades_for_live": 0, "live_phase": 1},
           "copytrade": {"enabled": True, "mode": "live", "stake_usd": 1, "max_per_market_usd": 3,
                         "risk": {"max_total_exposure_usd": 10, "max_consecutive_losses": 5,
                                  "daily_loss_limit_usd": "off"}, "entry": {"stake_usd": 1}}}
    monkeypatch.setenv("POLYMARKET_KEY_ID", "k")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", base64.b64encode(os.urandom(32)).decode())
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(SystemExit):                          # wrong phrase: refuses
            live_gate.check_live_gates(cfg, os.path.join(d, "x.db"), input_fn=lambda _: "no",
                                       account_fn=lambda *a: (True, "ok"))
    out = capsys.readouterr().out
    assert "COPY TRADING with REAL MONEY: $1 per copied trade" in out
    assert "max exposure $10" in out and "stop after 5 losses in a row" in out


def test_order_that_exists_is_returned_even_if_the_first_read_fails():
    from app.trading.broker import BrokerError
    from app.trading.us_broker import UsLiveBroker
    b = UsLiveBroker("k", base64.b64encode(os.urandom(32)).decode(), lambda t: {})

    async def req(method, path, body=None):
        if method == "POST":
            return {"id": "o1"}
        raise BrokerError("ReadTimeout: ")                  # GET /v1/order/o1 times out
    b._req = req
    o = asyncio.run(b.place_limit(token="m:long", side="BUY", price=0.5, size=2, post_only=False, ioc=False))
    assert o.order_id == "o1" and not o.done and "o1" in b.orders  # followed + canceled by the trader
    asyncio.run(b.client.aclose())


def test_rate_limit_during_lost_order_lookup_still_reports_unknown_outcome():
    from app.trading.broker import BrokerError, Maintenance
    from app.trading.us_broker import UsLiveBroker
    b = UsLiveBroker("k", base64.b64encode(os.urandom(32)).decode(), lambda t: {})

    async def req(method, path, body=None):
        if method == "POST":
            raise BrokerError("ReadTimeout: ")
        raise Maintenance("rate limited (429)", 2)

    async def fast_sleep(_):
        return None
    b._req = req
    orig = asyncio.sleep
    asyncio.sleep = fast_sleep
    try:
        with pytest.raises(BrokerError, match="outcome unknown"):
            asyncio.run(b.place_limit(token="m:long", side="BUY", price=0.5, size=2, post_only=False, ioc=False))
    finally:
        asyncio.sleep = orig
    asyncio.run(b.client.aclose())


def test_expired_position_with_the_wrong_quantity_is_still_a_mismatch(tmp_path):
    from app.trading import reconcile as R

    class Eng:
        def __init__(self, st):
            self.trade_store, self.kills = st, []

        async def kill_all(self, reason):
            self.kills.append(reason)

        def alert(self, *a):
            pass

    class Brk:
        async def _req(self, method, path, body=None):
            if path == "/v1/portfolio/positions":
                return {"positions": {"ok": {"netPositionDecimal": "2", "expired": True},
                                      "bad": {"netPositionDecimal": "0.8", "expired": True},
                                      "old": {"netPositionDecimal": "5", "expired": True}}}
            return {"orders": []}
    db = DB(str(tmp_path / "x.db"))
    st = TradeStore(db, "live", "polymarket_us", "copytrade")
    for slug in ("ok", "bad"):
        pid = st.new_position(slug=slug, duration="T", side="x", token=f"{slug}:long", status="open",
                              opened_ts=1.0, entry_price=0.5)
        st.update_position(pid, shares=2.0)
    r = asyncio.run(R.Reconciler(Eng(TradeStore(db, "live", "polymarket_us")), Brk(), db).startup())
    assert len(r.mismatches) == 1 and r.mismatches[0].startswith("bad:")   # 'old' = an already-closed copy

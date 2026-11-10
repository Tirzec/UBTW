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


def test_leftover_positions_of_a_switched_off_module_are_settled(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, "live", "live")
    e.copytrade = _FakeCopy(None, None, None)
    e.traders = {"copytrade": object()}
    crypto = TradeStore(e.db, "live", "polymarket_us", "crypto_updown")
    won = crypto.new_position(slug="btc-a", duration="5m", side="Up", token="btc-a:long", status="open",
                              opened_ts=1.0, entry_price=0.4)
    crypto.update_position(won, shares=2.5, cost=1.0, entry_fee=0.02)
    lost = crypto.new_position(slug="btc-b", duration="5m", side="Down", token="btc-b:short", status="resolving",
                               opened_ts=1.0, entry_price=0.5)
    crypto.update_position(lost, shares=2.0, cost=1.0, entry_fee=0.02)
    waiting = crypto.new_position(slug="btc-c", duration="5m", side="Up", token="btc-c:long", status="open",
                                  opened_ts=1.0, entry_price=0.5)
    copy = TradeStore(e.db, "live", "polymarket_us", "copytrade")
    mine = copy.new_position(slug="btc-a", duration="T", side="x", token="btc-a:long", status="open",
                             opened_ts=1.0, entry_price=0.5)
    e.copytrade.settled = {"btc-a": 1.0, "btc-b": 1.0}          # long won both; btc-c not settled yet
    done = asyncio.run(e.settle_leftovers(now=5.0))
    assert sorted(done) == sorted([won, lost])
    a, b = crypto.position(won), crypto.position(lost)
    assert a["status"] == "closed" and a["payout"] == pytest.approx(2.5) and a["pnl"] == pytest.approx(1.48)
    assert b["status"] == "closed" and b["payout"] == 0 and b["pnl"] == pytest.approx(-1.02)
    assert crypto.position(waiting)["status"] == "open"
    assert copy.position(mine)["status"] == "open"              # a running module settles its own
    assert asyncio.run(e.settle_leftovers()) == []              # nothing twice


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

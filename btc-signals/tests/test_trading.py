"""Auto-trader: rules, risk controls, paper broker, full position lifecycles, live gate."""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from app.db import DB
from app.ingest.polymarket_ws import BookStore
from app.signal import Decision
from app.trading.broker import PaperBroker
from app.trading.live_gate import CONFIRM_PHRASE, check_live_gates
from app.trading.risk import RiskManager, next_local_midnight
from app.trading.rules import evaluate_bot_exit, plan_entry, trigger_levels, value_position
from app.trading.store import RedactSecrets, TradeStore
from app.trading.trader import AutoTrader

SCHED = {"rate": 0.07, "exponent": 1}

# ------------------------------------------------------------------ rules


def test_plan_entry_maker_improves_bid_inside_spread():
    p = plan_entry(best_bid=0.50, best_ask=0.54, stake_usd=5, tick=0.01, min_size=5)
    assert p.ok and p.post_only and p.price == 0.51 and p.expected_fee == 0
    assert p.shares == 9.80 and p.cost == pytest.approx(4.998)


def test_plan_entry_maker_joins_bid_on_one_tick_spread_and_taker_crosses():
    assert plan_entry(best_bid=0.50, best_ask=0.51, stake_usd=5, tick=0.01, min_size=5).price == 0.50
    t = plan_entry(best_bid=0.50, best_ask=0.51, stake_usd=5, tick=0.01, min_size=5, style="taker",
                   fee_schedule=SCHED)
    assert t.price == 0.51 and not t.post_only and t.expected_fee == pytest.approx(9.8 * 0.07 * 0.51 * 0.49)


def test_plan_entry_respects_min_size_and_max_price():
    assert not plan_entry(best_bid=0.95, best_ask=0.97, stake_usd=2, tick=0.01, min_size=5).ok
    assert not plan_entry(best_bid=0.98, best_ask=0.99, stake_usd=5, tick=0.01, min_size=5).ok
    assert not plan_entry(best_bid=None, best_ask=None, stake_usd=5, tick=0.01, min_size=5).ok


def test_value_position_counts_only_sellable_depth_and_fees():
    v = value_position(shares=10, cost_basis=5.0, bids=[(0.60, 4), (0.55, 100)], fee_schedule=SCHED)
    gross = 4 * 0.60 + 6 * 0.55
    fee = 4 * 0.07 * 0.6 * 0.4 + 6 * 0.07 * 0.55 * 0.45
    assert v.proceeds == pytest.approx(gross) and v.fee == pytest.approx(fee)
    assert v.pnl == pytest.approx(gross - fee - 5.0) and v.complete
    thin = value_position(shares=10, cost_basis=5.0, bids=[(0.60, 4)], fee_schedule=SCHED)
    assert not thin.complete and thin.shares_sellable == 4 and thin.pnl < 0   # unsellable shares count 0


X = dict(take_profit_pct=0.08, stop_loss_pct=0.10, flip_threshold=0.5, time_exit_s=20,
         hold_min_ev=0.02, entry_price=0.5)


def val(bid, shares=10, basis=5.0):
    return value_position(shares=shares, cost_basis=basis, bids=[(bid, 1000)], fee_schedule=SCHED)


def test_exit_rules_each_trigger():
    assert evaluate_bot_exit(val=val(0.60), fair=0.9, seconds_left=200, **X).trigger == "take_profit"
    assert evaluate_bot_exit(val=val(0.44), fair=0.9, seconds_left=200, **X).trigger == "stop_loss"
    assert evaluate_bot_exit(val=val(0.50), fair=0.45, seconds_left=200, **X).trigger == "prob_flip"
    assert evaluate_bot_exit(val=val(0.53), fair=0.51, seconds_left=200, **X).trigger == "edge_gone"
    hold = evaluate_bot_exit(val=val(0.52), fair=0.70, seconds_left=200, **X)
    assert hold.action == "HOLD"


def test_exit_time_rule_holds_only_with_positive_ev():
    h = evaluate_bot_exit(val=val(0.52), fair=0.80, seconds_left=15, **X)
    assert h.action == "HOLD" and h.trigger == "hold_to_resolution"
    s = evaluate_bot_exit(val=val(0.52), fair=0.52, seconds_left=15, **X)
    assert s.action == "SELL" and s.trigger in ("time_exit", "edge_gone")


def test_exit_degraded_uses_price_rules_only():
    d = evaluate_bot_exit(val=val(0.50), fair=0.2, seconds_left=200, model_ok=False, **X)
    assert d.action == "HOLD"                                         # model ignored
    assert evaluate_bot_exit(val=val(0.40), fair=None, seconds_left=200, model_ok=False, **X).trigger == "stop_loss"
    assert evaluate_bot_exit(val=val(0.50), fair=0.9, seconds_left=10, model_ok=False, **X).trigger == "time_exit"


def test_exit_grace_period_uses_hard_stop_only():
    g = dict(X, stop_loss_grace_s=15, hard_stop_pct=0.25)
    assert evaluate_bot_exit(val=val(0.44), fair=0.4, seconds_left=200, seconds_since_fill=3, **g).action == "HOLD"
    assert evaluate_bot_exit(val=val(0.36), fair=0.9, seconds_left=200, seconds_since_fill=3, **g).trigger == "stop_loss"
    assert evaluate_bot_exit(val=val(0.60), fair=0.9, seconds_left=200, seconds_since_fill=3, **g).trigger == "take_profit"
    assert evaluate_bot_exit(val=val(0.44), fair=0.9, seconds_left=200, seconds_since_fill=16, **g).trigger == "stop_loss"


def test_trigger_levels_match_exit_rule():
    tp, sl = trigger_levels(shares=10, cost_basis=5.0, take_profit_pct=0.08, stop_loss_pct=0.10,
                            fee_schedule=SCHED)
    assert evaluate_bot_exit(val=val(tp), fair=0.99, seconds_left=200, **X).trigger == "take_profit"
    assert evaluate_bot_exit(val=val(tp - 0.01), fair=0.99, seconds_left=200, **X).action == "HOLD"
    assert evaluate_bot_exit(val=val(sl), fair=0.99, seconds_left=200, **X).trigger == "stop_loss"
    assert evaluate_bot_exit(val=val(sl + 0.01), fair=0.99, seconds_left=200, **X).action == "HOLD"

# ------------------------------------------------------------------ risk


R = {"max_stake_usd": 5, "daily_loss_limit_usd": 25, "max_consecutive_losses": 5,
     "max_total_exposure_usd": 15, "min_wallet_balance_usd": 10, "max_api_errors": 3}


def test_risk_daily_limit_and_reset():
    r = RiskManager(R)
    now = time.time()
    assert not r.entry_blockers(now=now, stake=5, open_exposure=0, window_busy=False, degraded=False)
    alerts = r.record_close(-26, now)
    assert any("Daily loss limit" in a for a in alerts)
    assert any("daily loss" in b for b in r.entry_blockers(now=now, stake=5, open_exposure=0,
                                                           window_busy=False, degraded=False))
    tomorrow = next_local_midnight(now) + 1
    assert not r.entry_blockers(now=tomorrow, stake=5, open_exposure=0, window_busy=False, degraded=False)


def test_risk_consecutive_losses_halt_and_resume():
    r = RiskManager(R)
    now = time.time()
    for i in range(4):
        r.record_close(-1, now)
    r.record_close(+0.5, now)
    assert r.state.consecutive_losses == 0
    for i in range(5):
        alerts = r.record_close(-1, now)
    assert r.state.halted and alerts and r.exits_allowed()           # pause keeps exits running
    r.resume()
    assert not r.state.halted and r.state.consecutive_losses == 0


def test_risk_limits_exposure_stake_window_degraded_api_balance():
    r = RiskManager(R)
    now = time.time()
    b = r.entry_blockers(now=now, stake=6, open_exposure=12, window_busy=True, degraded=True)
    assert any("stake" in x for x in b) and any("exposure" in x for x in b)
    assert any("window" in x for x in b) and "degraded data" in b
    assert r.record_api_error(now) is None and r.record_api_error(now) is None
    assert r.record_api_error(now) and r.state.halted
    r2 = RiskManager(R)
    assert r2.check_balance(9.5) and r2.state.halted
    r3 = RiskManager(R)
    r3.halt("KILL SWITCH: test")
    assert not r3.exits_allowed()

# ------------------------------------------------------------------ paper broker


def test_paper_broker_post_only_and_fills():
    bs = BookStore()
    bs.apply({"asset_id": "T", "bids": [{"price": "0.50", "size": "100"}],
              "asks": [{"price": "0.54", "size": "100"}]}, 1)
    b = PaperBroker(bs.get, bs.trades_since, lambda t: SCHED)

    async def run():
        rej = await b.place_limit(token="T", side="BUY", price=0.54, size=10, post_only=True)
        assert rej.status == "rejected" and "cross" in rej.error
        o = await b.place_limit(token="T", side="BUY", price=0.51, size=10, post_only=True)
        assert o.status == "live" and (await b.poll(o.order_id)).filled == 0
        bs.apply({"event_type": "last_trade_price", "asset_id": "T", "price": "0.51", "size": "5"})
        assert (await b.poll(o.order_id)).filled == 0                  # trade AT our price: not enough
        bs.apply({"event_type": "last_trade_price", "asset_id": "T", "price": "0.50", "size": "5"})
        o = await b.poll(o.order_id)
        assert o.status == "filled" and o.avg_price == 0.51 and o.fee == 0      # maker: no fee
        t = await b.place_limit(token="T", side="SELL", price=0.48, size=10, post_only=False)
        assert t.status == "filled" and t.avg_price == 0.50
        assert t.fee == pytest.approx(10 * 0.07 * 0.5 * 0.5)                    # taker fee
        r = await b.place_limit(token="T", side="BUY", price=0.40, size=10, post_only=True)
        assert await b.cancel_all() == 1 and b.orders[r.order_id].status == "canceled"
    asyncio.run(run())

# ------------------------------------------------------------------ end-to-end trader


class FakeEngine:
    def __init__(self, tmp_path, start):
        self.db = DB(tmp_path / "t.db")
        self.durations = ["5m"]
        self.clob = SimpleNamespace(store=BookStore())
        self.feeds = SimpleNamespace(quality=SimpleNamespace(state=SimpleNamespace(degraded=False)))
        self.win = SimpleNamespace(slug=f"btc-updown-5m-{start}", duration="5m", start=start,
                                   end=start + 300, token_up="UP", token_down="DN", condition_id="0xc",
                                   tick_size=0.01, min_size=5, fee_schedule=SCHED, outcome=None)
        self.discovery = SimpleNamespace(windows={self.win.slug: self.win},
                                         current=lambda d, now=None: self.win)
        self.evals = {}
        self.alerts = []

    def alert(self, kind, title, body="", level="info"):
        self.alerts.append((kind, title, body))

    def signal(self, action, p_up, edge=0.06):
        d = Decision(action, edge, {}, [], action if action != "NO TRADE" else None)
        self.evals["5m"] = SimpleNamespace(window=self.win, decision=d, p_up=p_up)

    def book(self, tok, bid, ask, size=1000):
        self.clob.store.apply({"asset_id": tok, "bids": [{"price": str(bid), "size": str(size)}],
                               "asks": [{"price": str(ask), "size": str(size)}]})


TC = {"trading": {"entry": {"stake_usd": 5, "order_timeout_s": 8, "no_entry_last_s_5m": 60,
                            "late_entry_min_edge": 0.12, "reentry_cooldown_s": 20,
                            "max_entries_per_window": 2},
                  "exits": {"take_profit_pct": 0.08, "stop_loss_pct": 0.10, "flip_threshold": 0.5,
                            "time_exit_s_5m": 20, "hold_min_ev": 0.02, "exit_slippage_ticks": 2,
                            "exit_order_timeout_s": 3},
                  "risk": R, "balance_check_s": 9999}}


def make(tmp_path, start=None):
    start = start or int(time.time()) - 60
    e = FakeEngine(tmp_path, start)
    store = TradeStore(e.db, "paper")
    b = PaperBroker(e.clob.store.get, e.clob.store.trades_since, lambda t: SCHED)
    return e, AutoTrader(e, TC, b, store)


def test_full_trade_take_profit(tmp_path):
    e, t = make(tmp_path)

    async def run():
        e.book("UP", 0.50, 0.54)
        e.signal("UP", 0.70)
        now = time.time()
        await t.tick(now)
        p, = t.store.active_positions()
        assert p["status"] == "opening" and p["entry_price"] == 0.51
        e.book("UP", 0.50, 0.51)                                   # ask drops to our price -> filled
        await t.tick(now + 1)
        p = t.store.position(p["id"])
        assert p["status"] == "open" and p["shares"] == 9.80 and p["entry_fee"] == 0
        assert p["tp_price"] > 0.51 > p["sl_price"]
        e.book("UP", 0.57, 0.58)                                   # +11% gross, ~+8.4% after exit fee
        await t.tick(now + 2)
        p = t.store.position(p["id"])
        assert p["status"] == "closed" and p["exit_trigger"] == "take_profit"
        exp = 9.8 * 0.57 - 9.8 * 0.07 * 0.57 * 0.43 - 9.8 * 0.51
        assert p["pnl"] == pytest.approx(exp) and p["pnl"] > 0
        kinds = [r["kind"] for r in e.db.query("SELECT kind FROM bot_events")]
        assert {"order", "fill", "close"} <= set(kinds)
        orders = e.db.query("SELECT * FROM bot_orders ORDER BY id")
        assert [o["side"] for o in orders] == ["BUY", "SELL"] and orders[0]["post_only"] == 1
        assert t.risk.state.day_pnl == pytest.approx(exp)
        await t.tick(now + 30)                                     # cooldown over: 2nd entry allowed
        await t.tick(now + 60)                                     # (entry 2 times out, void)
        e.book("UP", 0.50, 0.51)
        await t.tick(now + 90)
        await t.tick(now + 91)
        n = e.db.query("SELECT COUNT(*) n FROM bot_positions WHERE status!='void'")[0]["n"]
        assert n <= 2                                              # max_entries_per_window
    asyncio.run(run())


def test_stop_loss_and_one_position_per_window(tmp_path):
    e, t = make(tmp_path)

    async def run():
        e.book("UP", 0.50, 0.51)
        e.signal("UP", 0.70)
        now = time.time()
        await t.tick(now)                                           # post-only at 0.50 (1-tick spread)
        e.book("UP", 0.49, 0.50)
        await t.tick(now + 1)
        assert len(t.store.active_positions()) == 1                 # still one: no second entry
        assert t.store.active_positions()[0]["status"] == "open"
        e.book("UP", 0.43, 0.44)
        await t.tick(now + 2)
        p = t.store.closed_positions()[0]
        assert p["exit_trigger"] == "stop_loss" and p["pnl"] < 0
    asyncio.run(run())


def test_unfilled_entry_is_canceled_after_timeout(tmp_path):
    e, t = make(tmp_path)

    async def run():
        e.book("UP", 0.50, 0.54)
        e.signal("UP", 0.70)
        now = time.time()
        await t.tick(now)
        await t.tick(now + 9)
        p = e.db.query("SELECT * FROM bot_positions")[0]
        assert p["status"] == "void" and "not filled" in p["exit_reason"]
        assert e.db.query("SELECT status FROM bot_orders")[0]["status"] == "canceled"
    asyncio.run(run())


def test_no_entry_late_unless_high_edge_and_not_when_degraded(tmp_path):
    e, t = make(tmp_path, start=int(time.time()) - 250)             # 50s left
    e.book("UP", 0.50, 0.54)

    async def run():
        e.signal("UP", 0.70, edge=0.05)
        await t.tick(time.time())
        assert not t.store.active_positions()
        e.signal("UP", 0.90, edge=0.20)
        await t.tick(time.time())
        assert len(t.store.active_positions()) == 1
    asyncio.run(run())
    e2, t2 = make(tmp_path / "b")
    e2.book("UP", 0.50, 0.54)
    e2.signal("UP", 0.7)
    e2.feeds.quality.state.degraded = True
    asyncio.run(t2.tick(time.time()))
    assert not t2.store.active_positions() and "degraded" in t2.status_note


def test_hold_to_resolution_and_settle(tmp_path):
    e, t = make(tmp_path, start=int(time.time()) - 285)             # 15s left: time-exit zone

    async def run():
        e.book("UP", 0.50, 0.51)
        e.signal("UP", 0.95, edge=0.3)
        now = time.time()
        await t.tick(now)
        e.book("UP", 0.49, 0.50)                                    # fill
        await t.tick(now + 1)
        await t.tick(now + 2)                                       # first exit evaluation
        p = t.store.active_positions()[0]
        assert p["status"] == "open" and "holding to resolution" in p["last_note"]
        await t.tick(e.win.end + 1)                                 # window over, no outcome yet
        assert t.store.position(p["id"])["status"] == "resolving"
        e.win.outcome = "UP"
        await t.tick(e.win.end + 30)
        p = t.store.position(p["id"])
        assert p["status"] == "closed" and p["payout"] == pytest.approx(p["shares"])
        assert p["pnl"] == pytest.approx(p["shares"] - p["cost"])
    asyncio.run(run())


def test_kill_switch_cancels_and_halts_everything_and_persists(tmp_path):
    e, t = make(tmp_path)

    async def run():
        e.book("UP", 0.50, 0.54)
        e.signal("UP", 0.70)
        await t.tick(time.time())
        assert t.store.active_positions()[0]["status"] == "opening"
        n = await t.kill("test")
        assert n == 1 and t.risk.state.halted and not t.risk.exits_allowed()
        assert t.store.db.query("SELECT status FROM bot_positions")[0]["status"] == "void"
        await t.tick(time.time())
        assert not t.store.active_positions()                       # no new entries while halted
        assert any(k == "risk" for k, *_ in e.alerts)
    asyncio.run(run())
    t2 = AutoTrader(e, TC, PaperBroker(e.clob.store.get, e.clob.store.trades_since, lambda x: SCHED), t.store)
    assert t2.risk.state.halted                                     # survives a restart
    t.store.set_state("resume_requested", time.time())
    asyncio.run(t2.tick(time.time()))
    assert not t2.risk.state.halted


def test_cli_kill_request_is_picked_up(tmp_path):
    e, t = make(tmp_path)
    t.store.set_state("kill_requested", {"ts": time.time()})
    asyncio.run(t.tick(time.time()))
    assert t.risk.state.halted and "command line" in t.risk.state.halt_reason


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    e, t = make(tmp_path)
    t.risk.record_close(-30, time.time())
    e.book("UP", 0.50, 0.54)
    e.signal("UP", 0.7)
    asyncio.run(t.tick(time.time()))
    assert not t.store.active_positions() and "daily loss" in t.status_note

# ------------------------------------------------------------------ live gate + security


def _cfg(**over):
    tc = {"mode": "live", "live_trading_enabled": True, "min_paper_trades_for_live": 300,
          "risk": R}
    tc.update(over)
    return {"trading": tc}


def test_live_gate_refuses_without_300_paper_trades_and_when_geoblocked(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "k" * 64)
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", "0xabc")
    db = str(tmp_path / "g.db")
    with pytest.raises(SystemExit) as e:
        check_live_gates(_cfg(), db, input_fn=lambda _: CONFIRM_PHRASE,
                         geoblock_fn=lambda: (True, "US-IA"))
    msg = str(e.value)
    assert "0 paper trades" in msg and "blocks order placement" in msg and "VPN" in msg


def _fill_paper(db_path, n):
    d = DB(db_path)
    TradeStore(d, "paper")
    for i in range(n):
        d.insert("bot_positions", {"mode": "paper", "status": "closed", "pnl": 0.1 if i % 2 else -0.05,
                                   "cost": 5, "entry_fee": 0, "exit_fee": 0.02, "closed_ts": i})


def test_live_gate_needs_flag_and_exact_phrase(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "k" * 64)
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", "0xabc")
    db = tmp_path / "g.db"
    _fill_paper(db, 300)
    ok_geo = lambda: (False, "XX-YY")  # noqa: E731
    import sys
    if sys.version_info < (3, 11):           # e.g. macOS's built-in Python 3.9
        with pytest.raises(SystemExit, match="needs 3.11"):
            check_live_gates(_cfg(), str(db), input_fn=lambda _: CONFIRM_PHRASE, geoblock_fn=ok_geo)
        return
    try:
        import polymarket  # noqa: F401  # optional intl SDK; not installed by requirements.txt
    except ImportError:                      # without it the intl gate must refuse LIVE
        with pytest.raises(SystemExit, match="official SDK missing"):
            check_live_gates(_cfg(), str(db), input_fn=lambda _: CONFIRM_PHRASE, geoblock_fn=ok_geo)
        return
    with pytest.raises(SystemExit, match="live_trading_enabled"):
        check_live_gates(_cfg(live_trading_enabled=False), str(db), input_fn=lambda _: CONFIRM_PHRASE,
                         geoblock_fn=ok_geo)
    with pytest.raises(SystemExit, match="did not match"):
        check_live_gates(_cfg(), str(db), input_fn=lambda _: "yes", geoblock_fn=ok_geo)
    creds = check_live_gates(_cfg(), str(db), input_fn=lambda _: CONFIRM_PHRASE, geoblock_fn=ok_geo)
    assert creds["wallet"] == "0xabc"


def test_secrets_are_redacted_from_logs(monkeypatch, caplog):
    key = "ab" * 32
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", key)
    lg = logging.getLogger("redact-test")
    lg.addFilter(RedactSecrets())
    with caplog.at_level(logging.INFO):
        lg.info("connecting with %s and 0x%s", key, "cd" * 32)
    assert key not in caplog.text and "cd" * 32 not in caplog.text and "REDACTED" in caplog.text


def test_env_files_are_gitignored():
    from pathlib import Path
    gi = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gi.splitlines() and "*.pem" in gi


def test_raised_daily_limit_lifts_todays_stop_on_restart(tmp_path):
    """Stopped under a $1 limit with -$1.21 today; restarted with $5 -> trading allowed again.
    Restarted with the same limit -> stays stopped."""
    import copy
    e, t = make(tmp_path)
    now = time.time()
    t.store.db.insert("bot_positions", {"mode": "paper", "status": "closed", "pnl": -1.21, "cost": 1,
                                        "entry_fee": 0, "exit_fee": 0, "opened_ts": now - 60, "closed_ts": now - 30,
                                        "venue": "polymarket_intl", "module": "crypto_updown"})
    t.risk.state.day_stopped_until = now + 3600
    t._save()
    same = copy.deepcopy(TC)
    same["trading"]["risk"] = {**R, "daily_loss_limit_usd": 1}
    t1 = AutoTrader(e, same, PaperBroker(e.clob.store.get, e.clob.store.trades_since, lambda x: SCHED), t.store)
    assert t1.risk.state.day_stopped_until > now
    raised = copy.deepcopy(TC)
    raised["trading"]["risk"] = {**R, "daily_loss_limit_usd": 5}
    t2 = AutoTrader(e, raised, PaperBroker(e.clob.store.get, e.clob.store.trades_since, lambda x: SCHED), t.store)
    assert t2.risk.state.day_stopped_until == 0.0
    assert not any("daily loss" in b for b in t2.risk.entry_blockers(now=now, stake=1, open_exposure=0,
                                                                     window_busy=False, degraded=False))



def _lock_trader(tmp_path, take_profit):
    import copy
    cfg = copy.deepcopy(TC)
    cfg["trading"]["exits"].update(profit_lock_price=0.96, take_profit_pct=take_profit, stop_loss_pct=5.0)
    e = FakeEngine(tmp_path, int(time.time()) - 60)
    store = TradeStore(e.db, "paper")
    return e, AutoTrader(e, cfg, PaperBroker(e.clob.store.get, e.clob.store.trades_since, lambda t: SCHED), store)


def test_resting_profit_lock_fills_without_reaction_delay(tmp_path):
    e, t = _lock_trader(tmp_path, take_profit=5.0)

    async def run():
        e.book("UP", 0.50, 0.54)
        e.signal("UP", 0.70)
        now = time.time()
        await t.tick(now)
        e.book("UP", 0.50, 0.51)
        await t.tick(now + 1)                                     # entry fills -> open
        await t.tick(now + 1.25)                                  # resting lock placed
        p, = t.store.active_positions()
        lock = t.broker.orders[p["lock_order"]]
        assert p["status"] == "open" and lock.side == "SELL" and lock.price == 0.96 and lock.post_only
        assert lock.purpose == "lock" and lock.size == p["shares"]
        e.book("UP", 0.96, 0.97)                                  # buyers reach 96c
        await t.tick(now + 1.5)
        p = t.store.position(p["id"])
        assert p["status"] == "closed" and p["exit_trigger"] == "profit_lock"
        assert p["pnl"] == pytest.approx(p["shares"] * (0.96 - 0.51))      # maker: no exit fee
    asyncio.run(run())


def test_sell_rule_first_cancels_the_lock_then_sells(tmp_path):
    e, t = _lock_trader(tmp_path, take_profit=0.08)

    async def run():
        e.book("UP", 0.50, 0.54)
        e.signal("UP", 0.70)
        now = time.time()
        await t.tick(now)
        e.book("UP", 0.50, 0.51)
        await t.tick(now + 1)
        await t.tick(now + 1.25)
        p, = t.store.active_positions()
        lock_id = p["lock_order"]
        e.book("UP", 0.57, 0.58)                                  # take-profit rule fires before 96c
        await t.tick(now + 1.5)
        p = t.store.position(p["id"])
        assert p["status"] == "closed" and p["exit_trigger"] == "take_profit"
        assert t.broker.orders[lock_id].status == "canceled"
        sells = [o for o in t.broker.orders.values() if o.side == "SELL" and o.filled]
        assert len(sells) == 1 and sells[0].purpose == "exit"     # sold once, never twice
        ev = e.db.query("SELECT message FROM bot_events WHERE kind='exit_latency'")
        assert ev and "price->order" in ev[0]["message"]
        ticks = e.db.query("SELECT * FROM bot_ticks WHERE position_id=?", (p["id"],))
        assert ticks and ticks[-1]["decision"] == "SELL"
    asyncio.run(run())

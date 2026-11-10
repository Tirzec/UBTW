"""Step 5: phase-1 caps, reconciliation, duplicate-safe order recovery, trade records, kill test."""
import asyncio
import base64
import json
import time

import httpx
import pytest

from app.db import DB
from app.trading import reconcile as R
from app.trading.live_phase import (PHASE1_MAX_TRADES_PER_DAY, apply_phase1, live_entries_today)
from app.trading.risk import GlobalRisk
from app.trading.store import TradeStore
from app.trading.us_broker import UsLiveBroker, outcome_unknown, parse_trades

SECRET = base64.b64encode(bytes(range(64))).decode()


# ------------------------------------------------------------------ phase 1
def test_phase1_caps_only_lower():
    cfg = {"trading": {"risk": {"max_stake_usd": 50, "daily_loss_limit_usd": 100}, "entry": {"stake_usd": 20},
                       "live_phase1": {"max_trades_per_day": 500}},
           "sports": {"risk": {"max_stake_usd": 0.5, "daily_loss_limit_usd": 3}},
           "risk_global": {"daily_loss_limit_usd": 40}}
    s = apply_phase1(cfg)
    assert cfg["trading"]["risk"]["max_stake_usd"] == 1.0 and cfg["trading"]["entry"]["stake_usd"] == 1.0
    assert cfg["trading"]["risk"]["daily_loss_limit_usd"] == 10.0
    assert cfg["sports"]["risk"]["max_stake_usd"] == 0.5 and cfg["sports"]["risk"]["daily_loss_limit_usd"] == 3
    assert cfg["risk_global"]["daily_loss_limit_usd"] == 10.0
    assert s["max_trades_per_day"] == PHASE1_MAX_TRADES_PER_DAY == 10           # config asked for 500


def test_trades_per_day_cap(tmp_path):
    d = DB(tmp_path / "t.db")
    TradeStore(d, "live", "polymarket_us", "crypto_updown")
    now = time.time()
    for i in range(10):
        d.insert("bot_positions", {"mode": "live", "status": "closed", "opened_ts": now - i, "venue": "polymarket_us",
                                   "module": "sports" if i % 2 else "crypto_updown"})
    d.insert("bot_positions", {"mode": "live", "status": "closed", "opened_ts": now, "venue": "polymarket_us",
                               "module": "test_order"})                          # test orders don't count
    assert live_entries_today(d, now) == 10
    g = GlobalRisk({"max_trades_per_day": 10}, trades_today_fn=lambda: live_entries_today(d, now))
    assert any("daily cap is 10" in b for b in g.entry_blockers(now, 1.0))


# ------------------------------------------------------------------ reconciliation
def _db_with(tmp_path, positions):
    d = DB(tmp_path / "r.db")
    TradeStore(d, "live", "polymarket_us", "crypto_updown")
    for p in positions:
        d.insert("bot_positions", {"mode": "live", "venue": "polymarket_us", "module": "crypto_updown",
                                   "exit_shares": 0, **p})
    return d


def test_parse_positions_and_signs():
    d = {"positions": {"a": {"netPositionDecimal": "2.5"}, "b": {"netPositionDecimal": "-1"},
                       "c": {"netPositionDecimal": "0"}, "x": {"netPositionDecimal": "3", "expired": True}}}
    assert R.parse_positions(d) == {"a": 2.5, "b": -1.0}


def test_compare_cases(tmp_path):
    d = _db_with(tmp_path, [{"slug": "up", "token": "up:long", "status": "open", "shares": 2.0},
                            {"slug": "dn", "token": "dn:short", "status": "open", "shares": 1.5},
                            {"slug": "fly", "token": "fly:long", "status": "opening", "shares": 1}])
    exp, infl = R.bot_expected(d)
    assert exp == {"up": 2.0, "dn": -1.5} and infl == {"fly"}
    assert R.compare(exp, {"up": 2.0, "dn": -1.5, "fly": 0.4}, infl, set(), []) == []      # in-flight skipped
    m = R.compare(exp, {"up": 1.0, "other": 3.0}, infl, set(), [])
    assert any("bot thinks +2, exchange shows +1" in x for x in m)
    assert any("dn: bot thinks it holds -1.5, exchange shows none" in x for x in m)
    assert any("other: exchange shows a position of +3 the bot didn't open" in x for x in m)
    m = R.compare({}, {}, set(), {"o9"}, [{"id": "o7", "marketSlug": "q"}])
    assert any("o7" in x and "not one of the bot's" in x for x in m) and any("o9" in x for x in m)
    assert R.compare({}, {}, set(), None, [{"id": "o7", "marketSlug": "q"}]) == []       # startup: positions only


class FakeEngine:
    def __init__(self, store):
        self.trade_store, self.kills, self.alerts = store, [], []

    async def kill_all(self, reason):
        self.kills.append(reason)
        return 0

    def alert(self, kind, title, body="", level="info"):
        self.alerts.append(title)


class FakeBroker:
    def __init__(self, positions, orders=()):
        self.positions, self.orders_ = positions, list(orders)

    async def _req(self, method, path, body=None):
        assert method == "GET"
        if path == "/v1/portfolio/positions":
            return {"positions": {k: {"netPositionDecimal": str(v)} for k, v in self.positions.items()}}
        return {"orders": self.orders_}


def test_reconciler_halts_after_two_strikes(tmp_path):
    d = _db_with(tmp_path, [{"slug": "up", "token": "up:long", "status": "open", "shares": 2.0}])
    store = TradeStore(d, "live", "polymarket_us")
    eng = FakeEngine(store)
    rc = R.Reconciler(eng, FakeBroker({"up": 2.0}), d)
    assert asyncio.run(rc.tick()).ok and not eng.kills
    rc.broker = FakeBroker({"up": 1.0})
    asyncio.run(rc.tick())
    assert not eng.kills and rc.strikes == 1                                     # one blip: no halt yet
    asyncio.run(rc.tick())
    assert eng.kills and "RECONCILE MISMATCH" in eng.kills[0] and eng.alerts
    assert store.get_state("kill_requested")["reason"].startswith("RECONCILE MISMATCH")


def test_startup_halts_immediately_on_orphan_position(tmp_path):
    d = _db_with(tmp_path, [])
    store = TradeStore(d, "live", "polymarket_us")
    eng = FakeEngine(store)
    r = asyncio.run(R.Reconciler(eng, FakeBroker({"m": 1.0}, [{"id": "old", "marketSlug": "z"}]), d).startup())
    assert not r.ok and len(r.mismatches) == 1 and eng.kills                    # leftover order not counted


# ------------------------------------------------------------------ duplicate-safe placement
class Ex:
    """POST /v1/orders 'times out' but the exchange created the order anyway."""

    def __init__(self, ioc_trades=(), create=True):
        self.posts, self.created, self.ioc_trades, self.create = 0, [], list(ioc_trades), create

    def h(self, req):
        p = req.url.path
        if p == "/v1/orders" and req.method == "POST":
            self.posts += 1
            b = json.loads(req.content)
            if self.create and b["participateDontInitiate"]:
                self.created.append({"id": "real1", "marketSlug": b["marketSlug"], "intent": b["intent"],
                                     "price": b["price"], "quantity": b["quantity"], "state": "ORDER_STATE_NEW"})
            raise httpx.ReadTimeout("timed out", request=req)
        if p == "/v1/orders/open":
            return httpx.Response(200, json={"orders": self.created})
        if p == "/v1/portfolio/activities":
            return httpx.Response(200, json={"activities": [{"type": "ACTIVITY_TYPE_TRADE", "trade": t}
                                                            for t in self.ioc_trades]})
        if p.startswith("/v1/order/"):
            return httpx.Response(200, json={"order": {"id": "real1", "state": "ORDER_STATE_NEW", "cumQuantity": "0"}})
        return httpx.Response(404)


def _broker(ex, monkeypatch):
    real = asyncio.sleep

    async def fast(*a, **k):
        await real(0)
    monkeypatch.setattr("app.trading.us_broker.asyncio.sleep", fast)
    return UsLiveBroker("k", SECRET, lambda t: {"rate": 0.095, "exponent": 1, "round_cents": True},
                        client=httpx.AsyncClient(transport=httpx.MockTransport(ex.h)), base="https://t")


def test_timeout_but_created_is_adopted_not_resent(monkeypatch):
    ex = Ex()
    b = _broker(ex, monkeypatch)
    o = asyncio.run(b.place_limit(token="m:long", side="BUY", price=0.4, size=1, post_only=True))
    assert o.order_id == "real1" and o.status == "live" and ex.posts == 1


def test_ioc_timeout_recovered_from_trade_history(monkeypatch):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1))
    ex = Ex(ioc_trades=[{"id": "t1", "marketSlug": "m", "price": {"value": "0.55"}, "qtyDecimal": "1",
                         "isAggressor": True, "createTime": now, "state": "TRADE_STATE_CLEARED"}])
    b = _broker(ex, monkeypatch)
    o = asyncio.run(b.place_limit(token="m:short", side="BUY", price=0.45, size=1, post_only=False))
    assert o.status == "filled" and o.filled == 1 and abs(o.avg_price - 0.45) < 1e-9 and ex.posts == 1
    assert o.fee == 0.02                                                          # 0.095*0.55*0.45 -> $0.02


def test_timeout_not_found_raises_and_never_resends(monkeypatch):
    ex = Ex(create=False)
    b = _broker(ex, monkeypatch)
    from app.trading.broker import BrokerError
    with pytest.raises(BrokerError, match="not re-sent"):
        asyncio.run(b.place_limit(token="m:long", side="BUY", price=0.4, size=1, post_only=True))
    assert ex.posts == 1


def test_outcome_unknown_classification():
    from app.trading.broker import BrokerError
    assert outcome_unknown(BrokerError("ReadTimeout: x")) and outcome_unknown(BrokerError("HTTP 502: bad gateway"))
    assert not outcome_unknown(BrokerError("HTTP 400: bad price"))


def test_parse_trades_filters():
    d = {"activities": [
        {"trade": {"id": "a", "marketSlug": "m", "price": {"value": "0.74"}, "qtyDecimal": "1", "isAggressor": True,
                   "createTime": "2026-11-09T22:16:00Z", "state": "TRADE_STATE_CLEARED"}},
        {"trade": {"id": "b", "marketSlug": "m", "price": {"value": "0.5"}, "qtyDecimal": "2",
                   "createTime": "2026-11-09T22:16:01Z", "state": "TRADE_STATE_BUSTED"}},
        {"accountBalanceChange": {}}]}
    t = parse_trades(d, "m")
    assert len(t) == 1 and t[0]["price"] == 0.74 and t[0]["aggressor"]


def test_trade_cap_can_be_switched_off_but_not_raised():
    for off in (False, None, "off"):
        cfg = {"trading": {"live_phase1": {"max_trades_per_day": off}}}
        assert apply_phase1(cfg)["max_trades_per_day"] is None
        g = GlobalRisk(cfg["risk_global"], trades_today_fn=lambda: 999)
        assert g.entry_blockers(time.time(), 1.0) == []
    assert apply_phase1({"trading": {"live_phase1": {"max_trades_per_day": 50}}})["max_trades_per_day"] == 10


def test_losing_streak_stops_for_the_day_then_resumes_automatically():
    from app.trading.risk import RiskManager
    rm = RiskManager({"max_consecutive_losses": 4, "losing_streak_action": "stop_for_day",
                      "daily_loss_limit_usd": 10})
    now = time.mktime(time.strptime("2026-11-10 14:00", "%Y-%m-%d %H:%M"))
    for _ in range(3):
        assert rm.record_close(-0.2, now) == []
    alerts = rm.record_close(-0.2, now)
    assert alerts and "stopped for today" in alerts[0] and not rm.state.halted
    kw = dict(stake=1, open_exposure=0, window_busy=False, degraded=False)
    assert any("4 losses in a row; resumes tomorrow" in b for b in rm.entry_blockers(now=now + 60, **kw))
    tomorrow = now + 12 * 3600
    assert rm.entry_blockers(now=tomorrow, **kw) == []
    assert rm.state.consecutive_losses == 0 and rm.state.day_stop_reason == ""
    old = RiskManager({"max_consecutive_losses": 4})                      # default still pauses
    for _ in range(4):
        old.record_close(-0.2, now)
    assert old.state.halted


def test_automatic_stops_can_all_be_switched_off():
    """User choice 2026-11-10: only STOP ends trading."""
    from app.trading.risk import RiskManager
    cfg = {"trading": {"risk": {"daily_loss_limit_usd": "off", "max_consecutive_losses": False,
                                "min_wallet_balance_usd": None, "max_stake_usd": 1, "max_total_exposure_usd": 5}},
           "risk_global": {"daily_loss_limit_usd": False, "max_total_exposure_usd": 5}}
    phase = apply_phase1(cfg)
    assert phase["daily_loss_limit_usd"] == "off"                     # phase-1 $10 cap switched off, not re-imposed
    rm = RiskManager(cfg["trading"]["risk"])
    now = time.time()
    for _ in range(20):
        assert rm.record_close(-1.0, now) == []                       # -$20, 20 losses in a row: no stop
    assert not rm.state.halted and not rm.state.day_stopped_until
    assert rm.check_balance(0.10) is None and not rm.state.halted     # low balance: no halt
    assert rm.entry_blockers(now=now, stake=1, open_exposure=0, window_busy=False, degraded=False) == []
    g = GlobalRisk(cfg["risk_global"])
    g.traders.append(type("T", (), {"risk": rm, "_exposure": lambda self: 0.0})())
    assert g.entry_blockers(now, 1.0) == []
    assert rm.summary()["daily_loss_limit"] is None
    # a NUMBER is still capped at $10 in phase 1 (can be switched off, never raised)
    assert apply_phase1({"trading": {"risk": {"daily_loss_limit_usd": 50}}})["daily_loss_limit_usd"] == 10

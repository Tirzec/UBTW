"""Fair-value strategy: settlement math, entry rules (PART 2), exits (PART 3), arbitrage watcher (PART 4)."""
import time
from types import SimpleNamespace

from app.modules.crypto_fair import CryptoFairModule
from app.settlement import FairEstimator, FairValue, fair_up, mc_fair_up, sum_min_offsets
from app.trading.rules import Valuation

SCHED = {"rate": 0.095, "exponent": 1, "takerOnly": True, "round_cents": True}


def test_closed_form_matches_monte_carlo_and_brute_force():
    u = [3, 1, 7, 2.5]
    assert sum_min_offsets(u) == sum(min(a, b) for a in u for b in u)
    for kw in (dict(now=0, end=300, price_to_beat=100000, index_now=100010, sigma=3),
               dict(now=270, end=300, price_to_beat=100000, index_now=99995, sigma=3, locked_prices=[100004] * 30)):
        assert abs(fair_up(**kw).p_up - mc_fair_up(**kw, n_paths=20000)) < 0.015


def test_locked_window_and_tie_rule():
    # all 60 prices locked at exactly the price to beat -> rounds to it -> UP (>= wins)
    fv = fair_up(now=300, end=300, price_to_beat=100.0, index_now=100.0, sigma=1, locked_sum=6000.0)
    assert fv.p_up == 1.0 and fv.locked == 60
    fv = fair_up(now=300, end=300, price_to_beat=100.0, index_now=99.0, sigma=1, locked_sum=60 * 99.994)
    assert fv.p_up == 0.0                                  # 99.994 rounds to 99.99 < 100.00 -> DOWN
    fv = fair_up(now=300, end=300, price_to_beat=100.0, index_now=99.0, sigma=1, locked_sum=60 * 99.996)
    assert fv.p_up == 1.0                                  # 99.996 rounds to 100.00 >= 100.00 -> UP
    fv = fair_up(now=300, end=300, price_to_beat=100.0, index_now=99.0, sigma=1, locked_sum=60 * 99.99)
    assert fv.p_up == 0.0


class W(SimpleNamespace):
    pass


def _module(now, p_up, up_book, dn_book, dur="15m", problems=()):
    L = 900 if dur == "15m" else 300
    start = int(now // L * L)
    w = W(slug=f"m{dur}", start=start, end=start + L, duration=dur, token_up=f"m{dur}:long",
          token_down=f"m{dur}:short", accepting_orders=True, rules=None, gamma_ptb=100.0, tick_size=0.01,
          min_size=0.01, size_step=0.01, fee_schedule=SCHED, condition_id="c", outcome=None)
    for b in (up_book, dn_book):
        b["updated"] = now                                  # fresh relative to the test clock
    books = {w.token_up: up_book, w.token_down: dn_book}
    eng = SimpleNamespace(durations=[dur], discovery=SimpleNamespace(current=lambda d, n: w, windows={w.slug: w}),
                          clob=SimpleNamespace(store=SimpleNamespace(get=books.get)), alert=lambda *a, **k: None)
    m = CryptoFairModule(eng, {"trading": {"entry": {"stake_usd": 1}}, "fair_value": {}})
    m._update = lambda now: None
    m.problems = list(problems)
    m.fair_now = {w.slug: FairValue(p_up, 100, 1, 0, w.end - now, 1)}
    return m, w


def book(bid, ask, size=50, ts=None):
    return {"bids": [(bid, size)], "asks": [(ask, size)], "updated": ts or time.time()}


def test_entry_rules():
    L = 900
    now = (int(time.time()) // L) * L + 400                 # past the first third
    m, w = _module(now, 0.70, book(0.58, 0.60), book(0.40, 0.42))
    (s,) = m.signals(now)
    assert s.side == "UP" and abs(s.edge - (0.70 - 0.60 - 0.095 * 0.6 * 0.4)) < 1e-9
    assert s.extra["limit"] == 0.59                         # moderate edge: one tick below the ask
    m, w = _module(now, 0.90, book(0.58, 0.60), book(0.40, 0.42))
    assert m.signals(now)[0].extra["limit"] == 0.60         # big edge: at the ask
    m, _ = _module(now, 0.66, book(0.58, 0.60), book(0.40, 0.42))
    assert m.signals(now) == []                             # edge 0.037 < 0.05 after fees
    m, _ = _module((now // L) * L + 100, 0.9, book(0.58, 0.60), book(0.40, 0.42))
    assert m.signals((now // L) * L + 100) == []           # first third
    m, _ = _module(now, 0.99, book(0.95, 0.96), book(0.04, 0.05))
    assert m.signals(now) == []                             # above 95c
    m, _ = _module(now, 0.9, book(0.58, 0.60), book(0.40, 0.42), problems=["spot feeds disagree"])
    assert m.signals(now) == []                             # data block
    m, _ = _module(now, 0.70, book(0.58, 0.60, size=0.5), book(0.40, 0.42, size=0.5))
    assert m.signals(now) == []                             # can't exit: book too thin


def test_five_minute_needs_double_edge():
    L = 300
    now = (int(time.time()) // L) * L + 150
    m, _ = _module(now, 0.70, book(0.58, 0.60), book(0.40, 0.42), dur="5m")
    assert m.signals(now) == []                             # 0.077 < 0.10
    m, _ = _module(now, 0.75, book(0.58, 0.60), book(0.40, 0.42), dur="5m")
    assert len(m.signals(now)) == 1


def _val(net, shares=1.0):
    return Valuation(shares, net * shares, 0, net * shares, net, 0, 0, True)


def test_exit_rules():
    now = time.time()
    m, w = _module(now, 0.70, book(0.58, 0.60), book(0.40, 0.42))
    pos = {"slug": w.slug, "side": "UP", "p_model": 0.75, "token": w.token_up}
    assert m.exit_decision(pos, _val(0.72), now).action == "SELL"          # bid 0.72 > fair 0.70
    d = m.exit_decision(pos, _val(0.65), now)
    assert d.action == "HOLD" and d.trigger == "hold_to_resolution"         # fair 0.70 > bid
    assert m.exit_decision(pos, Valuation(0, 0, 0, 0, 0, 0, 0, False), now).action == "HOLD"   # empty book
    m.problems = m.data_failures = ["BRTI feed stale"]
    assert m.exit_decision(pos, _val(0.50), now).trigger == "data_failure"
    m.problems = m.data_failures = []
    m.fair_hist[w.slug] = [(now - 5, 0.70)]
    m.fair_now[w.slug] = FairValue(0.10, 100, 1, 0, 100, 1)
    assert m.exit_decision(pos, _val(0.05), now).trigger == "prob_stop"    # fair 10% < 30%: sell, don't ride to 0


def test_arbitrage_watcher_single_book():
    now = time.time()
    m, w = _module(now, 0.5, book(0.49, 0.51), book(0.49, 0.51))
    m._arb_watch(now)
    st = m.arb["15m"]
    assert st["checks"] == 1 and st["opportunities"] == 0 and st["best_sum"] > 1.0


def test_self_check_rule():
    from app.trading.summary import self_check
    rows = [{"pnl": 0.1 if i % 2 else -0.15, "won": int(i % 2), "p_model": 0.7, "side_won": int(i % 2),
             "cost": 1, "entry_fee": 0, "exit_fee": 0, "closed_ts": i} for i in range(100)]
    r = self_check(rows)
    assert r is not None and not r["ok"] and any("P&L" in p for p in r["problems"])
    assert any("calibration" in p for p in r["problems"])                  # 70% predicted, 50% won
    assert self_check(rows[:99]) is None and self_check(rows + rows[:10]) is None


def test_lock_in_and_late_exit_rules():
    L = 900
    now = (int(time.time()) // L) * L + 500                     # 400 s left
    m, w = _module(now, 0.97, book(0.94, 0.95), book(0.05, 0.06))
    m.xc_exits = {"lock_in_bid": 0.93, "lock_in_hold_above": 0.99, "late_exit_s_15m": 90, "late_exit_min_bid": 0.85}
    pos = {"slug": w.slug, "side": "UP", "p_model": 0.8, "token": w.token_up}
    d = m.exit_decision(pos, Valuation(1, 0.94, 0, 0.94, 0.94, 0.3, 0.4, True), now)
    assert d.action == "SELL" and d.trigger == "lock_in"          # bid 94c >= 93c, fair 0.97 says hold
    m.fair_now[w.slug] = FairValue(0.995, 100, 1, 30, 30, 1)      # averaging started, 99.5% certain
    d = m.exit_decision(pos, Valuation(1, 0.94, 0, 0.94, 0.94, 0.3, 0.4, True), now)
    assert d.action == "HOLD"                                     # exception: near-certain -> hold
    # late exit: 60 s left on a 15m window, profitable, bid 0.88 (fair 0.92 would say hold)
    late = w.end - 60
    m, w = _module(late, 0.92, book(0.88, 0.89), book(0.11, 0.12))
    m.xc_exits = {"lock_in_bid": 0.93, "late_exit_s_15m": 90, "late_exit_min_bid": 0.85}
    d = m.exit_decision(pos | {"slug": w.slug}, Valuation(1, 0.88, 0, 0.86, 0.86, 0.2, 0.3, True), late)
    assert d.action == "SELL" and d.trigger == "late_exit"
    d = m.exit_decision(pos | {"slug": w.slug}, Valuation(1, 0.88, 0, 0.86, 0.86, -0.1, -0.1, True), late)
    assert d.action == "HOLD"                                     # not profitable: rule doesn't apply


def test_settlement_fetched_from_exchange_when_window_not_tracked():
    """A position held to resolution must settle even if the window dropped out of memory (restart)."""
    import asyncio
    from app.ingest.discovery import Window
    from app.ingest.us_markets import us_slug
    start = 1794271800
    slug = us_slug("15m", start)
    tracked = {}
    settled = []

    class Disc:
        windows = tracked

        def get(self, dur, st):
            w = Window(dur, st, st + 900, us_slug(dur, st), venue="polymarket_us")
            tracked[w.slug] = w
            return w

        async def refresh(self, w):
            w.outcome = "DOWN"                         # what the exchange reports after settlement

    eng = SimpleNamespace(durations=["15m"], discovery=Disc(), clob=None, alert=lambda *a, **k: None,
                          _on_settled=lambda w: settled.append(w.slug))
    m = CryptoFairModule(eng, {"trading": {}, "fair_value": {}})
    pos = {"slug": slug, "side": "DOWN"}
    assert m.settlement_per_share(pos) is None         # unknown: fetch scheduled (no loop here)

    async def run():
        assert m.settlement_per_share(pos) is None     # first call starts the fetch
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    m._settle_asked.clear()
    asyncio.run(run())
    assert m.settlement_per_share(pos) == 1.0 and settled == [slug]
    assert m.settlement_per_share({"slug": slug, "side": "UP"}) == 0.0


def test_long_shots_and_93c_plus_are_never_bought():
    L = 900
    now = (int(time.time()) // L) * L + 400
    for ask, fair in ((0.16, 0.40), (0.07, 0.30), (0.54, 0.90)):          # big model "edges", below 55c
        m, _ = _module(now, fair, book(ask - 0.01, ask), book(1 - ask - 0.01, 1 - ask + 0.01))
        sigs = m.signals(now)
        assert all(s.side != "UP" for s in sigs), (ask, fair)
    m, _ = _module(now, 0.999, book(0.93, 0.94), book(0.05, 0.06))
    assert m.signals(now) == []                                          # 94c > 93c cap
    m, _ = _module(now, 0.80, book(0.55, 0.56), book(0.43, 0.44))
    assert [s.side for s in m.signals(now)] == ["UP"]                    # 56c with a real edge: allowed


def test_no_doubled_or_opposite_bets_across_overlapping_windows():
    from app.ingest.us_markets import us_slug
    s15 = 1794272400                                                     # 01:00 UTC: 15m 01:00-01:15
    w15, w5_in, w5_after = us_slug("15m", s15), us_slug("5m", s15 + 600), us_slug("5m", s15 + 900)
    active = [{"id": 7, "slug": w15, "side": "UP", "status": "open"}]
    assert CryptoFairModule.overlapping_position(w5_in, active)["id"] == 7      # settles at the same time
    assert CryptoFairModule.overlapping_position(us_slug("5m", s15), active) is not None
    assert CryptoFairModule.overlapping_position(w5_after, active) is None      # starts when it ends
    assert CryptoFairModule.overlapping_position(us_slug("15m", s15 + 900), active) is None
    m, _ = _module(time.time(), 0.8, book(0.6, 0.61), book(0.38, 0.39))
    sig = SimpleNamespace(market_id=w5_in)
    assert any("overlapping window" in b for b in m.entry_blockers(sig, active, time.time()))
    assert m.entry_blockers(SimpleNamespace(market_id=w5_after), active, time.time()) == []


def test_profit_take_and_probability_stop():
    now = time.time()
    m, w = _module(now, 0.70, book(0.66, 0.67), book(0.33, 0.34))
    m.xc_exits = {"profit_take_gain": 0.06, "profit_take_max_edge": 0.03, "sell_if_fair_below": 0.30}
    pos = {"slug": w.slug, "side": "UP", "p_model": 0.75, "token": w.token_up, "entry_price": 0.60}
    # bid 0.66 = entry + 6c, nets 0.68 after... use net 0.68 vs fair 0.70: edge left 0.02 < 0.03 -> sell
    d = m.exit_decision(pos, _val(0.68), now)
    assert d.action == "SELL" and d.trigger == "profit_take"
    d = m.exit_decision(pos, _val(0.64), now)                            # edge left 0.06: keep holding
    assert d.action == "HOLD"
    m, w = _module(now, 0.70, book(0.62, 0.63), book(0.37, 0.38))       # bid only entry + 2c
    m.xc_exits = {"profit_take_gain": 0.06, "profit_take_max_edge": 0.03}
    assert m.exit_decision(pos | {"slug": w.slug}, _val(0.69), now).action == "HOLD"
    m, w = _module(now, 0.29, book(0.20, 0.22), book(0.78, 0.80))
    m.xc_exits = {"sell_if_fair_below": 0.30}
    d = m.exit_decision(pos | {"slug": w.slug}, _val(0.19), now)
    assert d.action == "SELL" and d.trigger == "prob_stop"
    m, w = _module(now, 0.31, book(0.20, 0.22), book(0.78, 0.80))
    m.xc_exits = {"sell_if_fair_below": 0.30}
    assert m.exit_decision(pos | {"slug": w.slug}, _val(0.19), now).action == "HOLD"
    m, w = _module(now, 0.10, {"bids": [], "asks": [(0.12, 5)]}, book(0.88, 0.90))
    m.xc_exits = {"sell_if_fair_below": 0.30}
    assert m.exit_decision(pos | {"slug": w.slug}, Valuation(0, 0, 0, 0, 0, 0, 0, False), now).action == "HOLD"


def test_limit_price_never_leaves_the_band():
    L = 900
    now = (int(time.time()) // L) * L + 400
    m, _ = _module(now, 0.66, book(0.53, 0.55), book(0.45, 0.47))     # ask 55c, moderate edge
    (s,) = m.signals(now)
    assert s.extra["limit"] == 0.55                                  # not 54c (one tick under the ask)


def test_lock_in_only_takes_profits():
    L = 900
    now = (int(time.time()) // L) * L + 500
    m, w = _module(now, 0.95, book(0.93, 0.94), book(0.06, 0.07))
    m.xc_exits = {"lock_in_bid": 0.93}
    pos = {"slug": w.slug, "side": "UP", "p_model": 0.95, "token": w.token_up, "entry_price": 0.93}
    d = m.exit_decision(pos, Valuation(1, 0.93, 0.01, 0.92, 0.92, -0.02, -0.02, True), now)
    assert d.trigger != "lock_in"                                    # bought at 93c: don't dump it at a loss
    d = m.exit_decision(pos | {"entry_price": 0.80}, Valuation(1, 0.93, 0.01, 0.92, 0.92, 0.11, 0.13, True), now)
    assert d.trigger == "lock_in"


def test_market_info_from_slug_when_window_not_tracked():
    from app.ingest.us_markets import us_slug
    slug = us_slug("15m", 1794272400)
    eng = SimpleNamespace(durations=["15m"], discovery=SimpleNamespace(windows={}), alert=lambda *a, **k: None)
    m = CryptoFairModule(eng, {"trading": {}, "fair_value": {}})
    mi = m.market(slug)
    assert mi is not None and mi.hold_phase_from == 1794272400 + 900 and mi.label == "15m"
    assert m.market("not-a-slug") is None


def test_one_entry_per_settlement_time_even_after_selling():
    from app.ingest.us_markets import us_slug
    s15 = 1794272400
    closed = {"id": 9, "slug": us_slug("15m", s15), "side": "DOWN", "status": "closed"}
    m, _ = _module(time.time(), 0.8, book(0.6, 0.61), book(0.38, 0.39))
    m._recent_positions = lambda now: [closed]
    blk = m.entry_blockers(SimpleNamespace(market_id=us_slug("5m", s15 + 600)), [], time.time())
    assert any("already traded #9" in b for b in blk)
    assert m.entry_blockers(SimpleNamespace(market_id=us_slug("5m", s15 + 900)), [], time.time()) == []



def test_warm_up_after_restart_never_forces_an_exit():
    """After a restart the model warms up for 30 min. That blocks ENTRIES only: an adopted position is held
    (profit lock keeps working), and sold on data failure only if feeds are actually stale/disagreeing."""
    now = time.time()
    m, w = _module(now, 0.70, book(0.58, 0.60), book(0.40, 0.42))
    pos = {"slug": w.slug, "side": "UP", "p_model": 0.75, "token": w.token_up, "entry_price": 0.60}
    m.problems, m.data_failures = ["warming up (1500s left)"], []
    assert m.exit_decision(pos, _val(0.58), now).action == "HOLD"           # fair known: normal rules
    m.fair_now.clear()                                                      # basis not known yet: no fair
    m.problems = ["warming up (1790s left)", "basis not known yet"]
    d = m.exit_decision(pos, _val(0.58), now)
    assert d.action == "HOLD" and d.trigger == "model_not_ready"
    m.data_failures = ["spot feeds disagree by 40 bps"]
    assert m.exit_decision(pos, _val(0.58), now).trigger == "data_failure"
    assert m.signals(now) == []                                             # entries stay blocked

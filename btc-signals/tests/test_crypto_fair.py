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
    pos = {"slug": w.slug, "side": "UP", "p_model": 0.75}
    assert m.exit_decision(pos, _val(0.72), now).action == "SELL"          # bid 0.72 > fair 0.70
    d = m.exit_decision(pos, _val(0.65), now)
    assert d.action == "HOLD" and d.trigger == "hold_to_resolution"         # fair 0.70 > bid
    assert m.exit_decision(pos, Valuation(0, 0, 0, 0, 0, 0, 0, False), now).action == "HOLD"   # empty book
    m.problems = ["BRTI feed stale"]
    assert m.exit_decision(pos, _val(0.50), now).trigger == "data_failure"
    m.problems = []
    m.fair_hist[w.slug] = [(now - 5, 0.70)]
    m.fair_now[w.slug] = FairValue(0.10, 100, 1, 0, 100, 1)
    assert m.exit_decision(pos, _val(0.05), now).trigger == "sharp_flip"   # 0.70 -> 0.10 in 5 s


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

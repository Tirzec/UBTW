"""Step 4: prove real order handling on Polymarket US with the smallest possible orders.

    python -m app.test_order            PART 1 only: one minimum-size BUY at 1c (far below the market,
                                        maker-only so it cannot fill), confirm it is listed as open,
                                        cancel it, confirm the cancel. Cost: $0.
    python -m app.test_order --fill     PART 1, then PART 2 after you type a confirmation phrase:
                                        BUY the minimum size at the ask (fill-or-cancel-the-rest), then
                                        SELL it straight back at the bid. Reports actual fees and slippage
                                        vs. what the bot predicted.
Options: --qty 1 (default: the market minimum, 0.01 contracts; at that size fees round to $0.00, so
         use --qty 1 (about $0.50) to measure a real fee) | --market 5m|15m (default 15m)

Never touches config or live mode. Every order is logged to data/signals.db (bot_orders/bot_events,
module "test_order"). If anything goes wrong mid-way it cancels its own order, and tells you exactly
what is left on the account.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

from .config import load_config, resolve_path
from .fees import fee_usdc

GATEWAY = "https://gateway.polymarket.us"
FILL_PHRASE = "BUY AND SELL FOR REAL"


class Abort(Exception):
    pass


@dataclass
class Report:
    lines: list = field(default_factory=list)
    ok: bool = True

    def step(self, status: str, text: str) -> None:
        self.lines.append((status, text))
        print(f"[{status:4s}] {text}", flush=True)
        if status == "FAIL":
            self.ok = False


def _parse_book(data: dict):
    from .preflight import parse_book
    return parse_book(data)


async def get_book(gw: httpx.AsyncClient, slug: str):
    r = await gw.get(f"{GATEWAY}/v1/markets/{slug}/book")
    if r.status_code == 404:              # a brand-new window's book isn't created yet
        return [], []
    r.raise_for_status()
    return _parse_book(r.json())


async def pick_market(gw: httpx.AsyncClient, cfg: dict, prefer: str, now: float | None = None):
    """Current crypto window with time left and a two-sided book. Returns (Window, bids, asks)."""
    from .ingest.discovery import DURATION_SECONDS, Window
    from .ingest.us_markets import apply_us_market, us_slug
    now = now or time.time()
    order = [prefer] + [d for d in ("15m", "5m") if d != prefer]
    why = []
    for dur in order:
        sec = DURATION_SECONDS[dur]
        start = int(now // sec * sec)
        left = start + sec - now
        if left < (240 if dur == "15m" else 150):
            why.append(f"{dur}: only {int(left)} s left")
            continue
        slug = us_slug(dur, start)
        r = await gw.get(f"{GATEWAY}/v1/market/slug/{slug}")
        if r.status_code != 200:
            why.append(f"{dur}: market not found (HTTP {r.status_code})")
            continue
        w = apply_us_market(Window(dur, start, start + sec, slug, venue="polymarket_us"),
                            r.json().get("market") or {})
        if not w.accepting_orders:
            why.append(f"{dur}: not open")
            continue
        bids, asks = await get_book(gw, slug)
        if not bids or not asks:
            why.append(f"{dur}: order book not two-sided yet")
            continue
        return w, bids, asks
    raise Abort("no suitable market right now (" + "; ".join(why) + "). Try again in a minute.")


async def open_order_ids(broker) -> set:
    return await broker.open_order_ids()


async def cancel_new_orders(broker, known: set) -> None:
    """Emergency cleanup: cancel every order this run created that may still be open."""
    for oid, o in list(broker.orders.items()):
        if oid in known or o.done:
            continue
        try:
            o = await broker.cancel(oid)
            print(f"cleanup: order {oid} -> {o.status}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"!! cleanup could not cancel {oid} ({e}). Cancel it in the Polymarket app NOW.", flush=True)


async def order_diagnostics(broker, order_id: str) -> str:
    """What the exchange itself reports about an order (state, quantities, reject reason)."""
    try:
        d = await broker._req("GET", f"/v1/order/{order_id}")
    except Exception as e:  # noqa: BLE001
        return f"(could not read order: {e})"
    o = d.get("order") or d
    keep = ("state", "type", "tif", "price", "quantity", "cumQuantity", "leavesQuantity", "avgPx",
            "rejectReason", "ordRejReason", "text", "cancelReason", "commissionNotionalTotalCollected")
    return ", ".join(f"{k}={o[k]}" for k in keep if k in o) or str(o)[:300]


async def settle(broker, o, timeout: float = 6.0, poll_s: float = 0.4):
    """Re-read an order until the exchange reports it finished (filled / canceled / rejected)."""
    deadline = time.time() + timeout
    while not o.done and time.time() < deadline:
        await asyncio.sleep(poll_s)
        o = await broker.poll(o.order_id)
    return o


# ----------------------------------------------------------------------------- part 1
async def part1(broker, w, bids, asks, qty: float, rep: Report, store=None, confirm=input) -> None:
    from .trading.us_broker import format_price
    price = 0.01
    if bids[0][0] < 0.05:
        raise Abort(f"best bid {bids[0][0]} is too close to 1c to place a far-away order safely")
    print(f"\nPART 1: rest + cancel. BUY {qty:g} Up @ {format_price(price, w.tick_size)} on {w.slug}\n"
          f"        (market: bid {bids[0][0]} / ask {asks[0][0]}; maker-only, so it can't fill). Cost: $0.",
          flush=True)
    if confirm("Place this resting test order? [y/N] ").strip().lower() not in ("y", "yes"):
        raise Abort("cancelled by you; nothing was placed")
    o = None
    try:
        t0 = time.time()
        o = await broker.place_limit(token=w.token_up, side="BUY", price=price, size=qty, post_only=True)
        if store:
            store.order(o, None, "test_rest")
        if o.status == "rejected":
            rep.step("FAIL", f"order rejected by the exchange: {o.error}")
            return
        rep.step("PASS", f"order accepted: id {o.order_id}, status {o.status} ({(time.time() - t0) * 1000:.0f} ms)")
        if o.filled:
            rep.step("FAIL", f"it filled {o.filled} - should be impossible at 1c; cancelling the rest")
        listed = o.order_id in await open_order_ids(broker)
        rep.step("PASS" if listed else "FAIL", "listed in your open orders" if listed
                 else "NOT listed in GET /v1/orders/open")
        t1 = time.time()
        o = await broker.cancel(o.order_id)
        if store:
            store.order(o, None, "test_rest")
        rep.step("PASS" if o.status == "canceled" else "FAIL",
                 f"cancel sent: status {o.status} ({(time.time() - t1) * 1000:.0f} ms)")
        for _ in range(4):                        # fresh reads from the exchange
            o.status = "live"
            o = await broker.poll(o.order_id)
            still = o.order_id in await open_order_ids(broker)
            if o.status == "canceled" and not still:
                break
            await asyncio.sleep(0.5)
        good = o.status == "canceled" and not still
        rep.step("PASS" if good else "FAIL",
                 "cancel confirmed: exchange reports it canceled and it is gone from open orders" if good
                 else f"cancel NOT confirmed: status {o.status}, still listed: {still}")
    finally:
        if o is not None and o.order_id and o.status not in ("canceled", "rejected", "filled"):
            try:
                await broker.cancel(o.order_id)
                print("cleanup: canceled the test order", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"!! cleanup failed ({e}). Cancel order {o.order_id} in the Polymarket app NOW, "
                      "or run: python -m app.trader stop", flush=True)


# ----------------------------------------------------------------------------- part 2
async def part2(broker, gw, w, qty: float, rep: Report, store=None, input_fn=input,
                max_cost: float = 1.0) -> dict:
    from .ingest.us_markets import long_token
    bids, asks = await get_book(gw, w.slug)
    if not bids or not asks:
        raise Abort("book went one-sided; try again")
    ask, bid = asks[0][0], bids[0][0]
    if not 0.10 <= ask <= 0.90:
        raise Abort(f"ask {ask} outside 0.10-0.90 (fees/ticks distort a test there); try again later")
    sched = w.fee_schedule
    p_buy_fee = fee_usdc(qty, ask, sched)
    p_sell_fee = fee_usdc(qty, bid, sched)
    p_cost = qty * ask
    p_pnl = qty * bid - p_cost - p_buy_fee - p_sell_fee
    if p_cost + p_buy_fee > max_cost:
        raise Abort(f"cost ${p_cost + p_buy_fee:.2f} is above the ${max_cost:g} cap; use a smaller --qty")
    print(f"\nPART 2: REAL fill. BUY {qty:g} Up at the ask {ask} (fill now or cancel), then SELL them straight\n"
          f"        back at the bid {bid}. Predicted: pay ${p_cost:.4f} + fee ${p_buy_fee:.2f}, get back "
          f"${qty * bid:.4f} - fee ${p_sell_fee:.2f} = round trip ${p_pnl:+.4f}.", flush=True)
    if not sys.stdin.isatty() and input_fn is input:
        raise Abort("PART 2 needs you to type the confirmation in a terminal window")
    if input_fn(f'Type exactly "{FILL_PHRASE}" to go ahead: ').strip() != FILL_PHRASE:
        raise Abort("confirmation did not match; nothing was bought")

    token = long_token(w.slug)
    tick = w.tick_size
    # typing the phrase takes a while and the market moves: re-read the price NOW
    _, fresh_asks = await get_book(gw, w.slug)
    if not fresh_asks:
        raise Abort("no sellers in the book right now; nothing was bought. Try again")
    ask_now = fresh_asks[0][0]
    if ask_now > ask + 2 * tick + 1e-9:
        raise Abort(f"the ask moved from {ask} to {ask_now} while you were typing; nothing was bought. "
                    "Run it again")
    limit = round(min(ask_now + tick, ask + 2 * tick), 6)        # 1 tick of room, never > approved + 2
    if qty * limit + fee_usdc(qty, limit, sched) > max_cost:
        raise Abort("price moved above the cost cap; nothing was bought")
    print(f"ask now {ask_now}; buying with limit {limit}", flush=True)
    try:
        pv = await broker.preview(token=token, side="BUY", price=limit, size=qty, post_only=False)
        keep = ("state", "price", "quantity", "cumQuantity", "leavesQuantity", "avgPx", "rejectReason",
                "text", "commissionNotionalTotalCollected")
        print("exchange preview (nothing placed): " +
              (", ".join(f"{k}={pv[k]}" for k in keep if k in pv) or str(pv)[:300]), flush=True)
    except Exception as e:  # noqa: BLE001 - the preview is diagnostic only
        print(f"exchange preview unavailable: {e}", flush=True)
    ask = ask_now
    buy = await broker.place_limit(token=token, side="BUY", price=limit, size=qty, post_only=False)
    buy = await settle(broker, buy)
    if store:
        store.order(buy, None, "test_fill_buy")
    if buy.status == "rejected":
        rep.step("FAIL", f"buy rejected: {buy.error}")
        return {}
    if not buy.done:
        buy = await broker.cancel(buy.order_id)
        rep.step("FAIL", f"buy order {buy.order_id} was still not finished after 6 s; cancel sent "
                         f"(status {buy.status}, filled {buy.filled:g}). Check the Polymarket app")
        return {}
    if not buy.filled:
        ex = (getattr(broker, "last_create", {}) or {}).get("executions")
        raw = await order_diagnostics(broker, buy.order_id) + (f", executions={ex}" if ex is not None else "")
        _, a2 = await get_book(gw, w.slug)
        rep.step("WARN", f"buy did not fill; nothing to sell, no money spent. Exchange says: {raw}. "
                         f"Ask right after: {a2[0][0] if a2 else 'none'} (our limit {limit})")
        return {}
    rep.step("PASS", f"bought {buy.filled:g} @ {buy.avg_price:.4f} (quoted ask {ask}), fee ${buy.fee:.2f}")

    left, sells, attempt = buy.filled, [], 0
    while left > 1e-9 and attempt < 4:
        bb, _ = await get_book(gw, w.slug)
        if not bb:
            await asyncio.sleep(1)
            attempt += 1
            continue
        quoted = bb[0][0]
        floor = max(0.01, round(quoted - (2 + attempt) * tick, 6))       # widen a tick each retry
        s = await broker.place_limit(token=token, side="SELL", price=floor, size=round(left, 2), post_only=False)
        s = await settle(broker, s)
        if store:
            store.order(s, None, "test_fill_sell")
        if s.filled:
            sells.append((s, quoted))
            left = round(left - s.filled, 6)
        attempt += 1
    sold = sum(s.filled for s, _ in sells)
    if left > 1e-9:
        rep.step("FAIL", f"could not sell {left:g} of {buy.filled:g} contracts. Sell them in the Polymarket "
                         f"app (market {w.slug}); they settle at $1 or $0 when the window ends anyway")
    proceeds = sum(s.notional for s, _ in sells)
    sell_fee = sum(s.fee for s, _ in sells)
    avg_sell = proceeds / sold if sold else 0.0
    q_bid = sells[0][1] if sells else bid
    if sold:
        rep.step("PASS", f"sold {sold:g} @ {avg_sell:.4f} (quoted bid {q_bid}), fee ${sell_fee:.2f}")
    a_pnl = proceeds - buy.notional - buy.fee - sell_fee
    res = {
        "qty": buy.filled, "buy_quoted": ask, "buy_avg": buy.avg_price, "buy_slippage": buy.avg_price - ask,
        "buy_fee_pred": fee_usdc(buy.filled, ask, sched), "buy_fee": buy.fee,
        "sell_quoted": q_bid, "sell_avg": avg_sell, "sell_slippage": (q_bid - avg_sell) if sold else None,
        "sell_fee_pred": fee_usdc(sold, q_bid, sched) if sold else 0.0, "sell_fee": sell_fee,
        "pnl_pred": p_pnl, "pnl": a_pnl}
    return res


def print_fill_report(r: dict) -> None:
    if not r:
        return
    print("\n=== PART 2 RESULT: predicted vs actual ===")
    print(f"{'':14s}{'predicted':>12s}{'actual':>12s}{'difference':>12s}")
    rows = [("buy price", r["buy_quoted"], r["buy_avg"]), ("buy fee $", r["buy_fee_pred"], r["buy_fee"]),
            ("sell price", r["sell_quoted"], r["sell_avg"]), ("sell fee $", r["sell_fee_pred"], r["sell_fee"]),
            ("round trip $", r["pnl_pred"], r["pnl"])]
    for name, p, a in rows:
        print(f"{name:14s}{p:12.4f}{a:12.4f}{a - p:+12.4f}")
    print(f"slippage: buy {r['buy_slippage']:+.4f}/contract (paid above the quoted ask), "
          f"sell {(r['sell_slippage'] or 0):+.4f}/contract (received below the quoted bid)")


# ----------------------------------------------------------------------------- kill switch
async def kill_test(broker, w, bids, qty: float, rep: Report, cfg: dict, store=None, confirm=input) -> None:
    """Place a 1c resting order, then fire the SAME exchange-side cancel the kill switch uses
    (`python -m app.trader stop` -> cancel ALL open orders) and confirm the order is gone."""
    from .trader import _direct_cancel_all
    print(f"\nKILL SWITCH TEST: BUY {qty:g} Up @ 0.01 on {w.slug} (can't fill), then the kill switch's\n"
          "cancel-all. NOTE: cancel-all cancels EVERY open order on your account, including any you placed.",
          flush=True)
    if confirm("Run the kill switch test? [y/N] ").strip().lower() not in ("y", "yes"):
        raise Abort("cancelled by you; nothing was placed")
    if bids[0][0] < 0.05:
        raise Abort("best bid too close to 1c for a safe far-away order")
    o = await broker.place_limit(token=w.token_up, side="BUY", price=0.01, size=qty, post_only=True)
    if store:
        store.order(o, None, "test_kill")
    if o.status == "rejected":
        rep.step("FAIL", f"order rejected: {o.error}")
        return
    listed = o.order_id in await broker.open_order_ids()
    rep.step("PASS" if listed else "FAIL", f"resting order {o.order_id} is open" if listed else "order not listed")
    t0 = time.time()
    msg = await _direct_cancel_all(cfg)
    rep.step("PASS" if "canceled" in msg else "FAIL", f"kill switch cancel-all: {msg} ({(time.time() - t0) * 1000:.0f} ms)")
    gone = False
    for _ in range(6):
        await asyncio.sleep(0.5)
        o.status = "live"
        o = await broker.poll(o.order_id)
        if o.status == "canceled" and o.order_id not in await broker.open_order_ids():
            gone = True
            break
    rep.step("PASS" if gone else "FAIL", "confirmed: the order is canceled on the exchange" if gone
             else f"order still {o.status} - cancel it in the Polymarket app")


# ----------------------------------------------------------------------------- main
async def run(cfg: dict, *, fill: bool, qty: float | None, prefer: str, confirm=input, input_fn=input,
              broker=None, gw: httpx.AsyncClient | None = None, kill: bool = False) -> Report:
    from .trading.us_broker import UsLiveBroker
    rep = Report()
    key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if broker is None and not (key_id and secret):
        raise Abort("POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY are not set in .env")
    own_gw = gw is None
    gw = gw or httpx.AsyncClient(timeout=10, headers={"User-Agent": "btc-signals-test-order/1.0"})
    store = None
    try:
        from .db import DB
        from .trading.store import TradeStore
        store = TradeStore(DB(resolve_path(cfg, cfg.get("db_path", "data/signals.db"))), "live",
                           "polymarket_us", "test_order")
    except Exception:  # noqa: BLE001 - logging is nice to have, never a reason to stop
        store = None
    try:
        w, bids, asks = await pick_market(gw, cfg, prefer)
        q = float(qty if qty is not None else w.min_size)
        if q < w.min_size:
            raise Abort(f"--qty {q} is below the market minimum {w.min_size}")
        if broker is None:
            broker = UsLiveBroker(key_id, secret, lambda t: w.fee_schedule,
                                  base=(cfg.get("polymarket_us", {}) or {}).get("api_url", "https://api.polymarket.us"),
                                  tick_for=lambda t: w.tick_size)
        bal = await broker.balance()
        rep.step("PASS", f"API key works; buying power ${bal:,.2f}" if bal is not None else "API key works")
        before = set(await open_order_ids(broker))
        if before:
            rep.step("INFO", f"you already have {len(before)} open order(s); the test leaves them alone")
        if store:
            store.event("test_order", f"start on {w.slug} qty {q} fill={fill}", "WARNING")
        known = set(broker.orders)
        result = {}
        try:
            if kill:
                await kill_test(broker, w, bids, q, rep, cfg, store, confirm)
                return rep
            await part1(broker, w, bids, asks, q, rep, store, confirm)
            if fill:
                if not rep.ok:
                    raise Abort("PART 1 had a failure, so PART 2 (the real fill) was not attempted")
                max_cost = float(((cfg.get("trading", {}) or {}).get("risk", {}) or {}).get("max_stake_usd", 1))
                result = await part2(broker, gw, w, q, rep, store, input_fn, max_cost=max_cost)
                print_fill_report(result)
                try:                                  # the exchange's own trade records = ground truth
                    trades = await broker.recent_trades(w.slug, since=time.time() - 120)
                    if trades:
                        print("exchange trade records for these fills: " + "; ".join(
                            f"{t['qty']:g} @ {t['price']:.4f} ({'taker' if t['aggressor'] else 'maker'})"
                            for t in trades))
                except Exception as e:  # noqa: BLE001
                    print(f"(could not read trade records: {e})")
        except BaseException:
            await cancel_new_orders(broker, known)
            raise
        after = await broker.balance()
        if after is not None and bal is not None:
            print(f"\nbuying power before ${bal:,.4f}, after ${after:,.4f} (change {after - bal:+.4f})")
        if store:
            store.event("test_order", f"done ok={rep.ok}", "WARNING", **{k: (round(v, 6) if isinstance(v, float)
                                                                            else v) for k, v in result.items()})
        return rep
    finally:
        if own_gw:
            await gw.aclose()
        if broker is not None and hasattr(broker, "close"):
            await broker.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.test_order", description=__doc__.split("\n\n")[0])
    ap.add_argument("--fill", action="store_true", help="also do PART 2: a real minimum-size buy + immediate sell")
    ap.add_argument("--qty", type=float, help="contracts (default: the market minimum)")
    ap.add_argument("--market", choices=["5m", "15m"], default="15m")
    ap.add_argument("--kill-test", action="store_true",
                    help="test the kill switch live: 1c resting order, then the kill switch's cancel-all")
    a = ap.parse_args(argv)
    from .console import safe_console
    safe_console()
    from .env_guard import check_env_safety
    check_env_safety()
    import logging
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .trading.store import install_redaction
    install_redaction()
    cfg = load_config()
    if (cfg.get("venue") or "").strip() != "polymarket_us":
        print("venue must be polymarket_us")
        return 1
    from .clock import check_clock
    ck = check_clock(ntp_timeout=1.5)
    print(f"[{ck.status:4s}] clock: {ck.detail}")
    if not ck.ok:
        print("Fix the clock first (python -m app.clock).")
        return 1
    try:
        rep = asyncio.run(run(cfg, fill=a.fill, qty=a.qty, prefer=a.market, kill=a.kill_test))
    except Abort as e:
        print(f"\nSTOPPED: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {e}\nCheck the Polymarket app for open orders; "
              "`python -m app.trader stop` cancels all of them.")
        return 1
    print("\nALL STEPS PASSED" if rep.ok else "\nSOME STEPS FAILED (see above)")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

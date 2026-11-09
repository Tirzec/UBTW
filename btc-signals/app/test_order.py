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
    d = await broker._req("GET", "/v1/orders/open")
    items = d.get("orders") if isinstance(d, dict) else d
    if not isinstance(items, list):
        items = next((v for v in (d or {}).values() if isinstance(v, list)), []) if isinstance(d, dict) else []
    return {str(o.get("id") or o.get("orderId") or "") for o in items}


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
    buy = await broker.place_limit(token=token, side="BUY", price=ask, size=qty, post_only=False)
    buy = await settle(broker, buy)
    if store:
        store.order(buy, None, "test_fill_buy")
    if buy.status == "rejected":
        rep.step("FAIL", f"buy rejected: {buy.error}")
        return {}
    if not buy.done:
        rep.step("FAIL", f"buy order {buy.order_id} still not finished after 6 s; check the Polymarket app")
        return {}
    if not buy.filled:
        rep.step("WARN", "buy did not fill (the ask moved away); nothing to sell, no money spent")
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


# ----------------------------------------------------------------------------- main
async def run(cfg: dict, *, fill: bool, qty: float | None, prefer: str, confirm=input, input_fn=input,
              broker=None, gw: httpx.AsyncClient | None = None) -> Report:
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
        await part1(broker, w, bids, asks, q, rep, store, confirm)
        result = {}
        if fill:
            if not rep.ok:
                raise Abort("PART 1 had a failure, so PART 2 (the real fill) was not attempted")
            max_cost = float(((cfg.get("trading", {}) or {}).get("risk", {}) or {}).get("max_stake_usd", 1))
            result = await part2(broker, gw, w, q, rep, store, input_fn, max_cost=max_cost)
            print_fill_report(result)
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
        rep = asyncio.run(run(cfg, fill=a.fill, qty=a.qty, prefer=a.market))
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

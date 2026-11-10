"""Copy-trading strategy module: copy the approved traders' international Polymarket trades onto Polymarket US.

Signal source (read-only): the traders' public fills on international Polymarket (Data API v2).
Orders: ONLY on Polymarket US, through the shared AutoTrader (same order lifecycle, kill switch, risk limits,
audit log, paper/live broker as before). This build is PAPER ONLY.

Copy rules (copytrade: in config.yaml):
  BUY  - only if the market exists on Polymarket US with the same question/line/side (strict matchers),
         the US price for our side is at most max_worse (3c) above the trader's price,
         the book has buyers to exit into (min_exit_depth_usd within exit_band of the ask, max_spread),
         it resolves more than min_time_to_resolution_s (1 h) from now,
         no other approved trader is on the opposite side of that market,
         at most max_per_market_usd ($3) in that market; stake_usd ($1) per copy.
  SELL - sell the same FRACTION of our copies of that trader in that market (unknown fraction: keep holding).
  A trader who goes silent: positions are held to the market's resolution (official US settlement).
Risk (AutoTrader/RiskManager): max_total_exposure_usd ($10), max_consecutive_losses (5) -> halt + alert.
Auto-pause a trader once their copies lose money after pause_after_trades (30) closed copies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

from ..ingest.polymarket_ws import BookStore
from ..ingest.us_markets import GATEWAY, nocache, us_book_to_store
from ..modules.base import MarketInfo, Reason, Signal, StrategyModule
from ..trading.rules import ExitDecision
from . import match_politics, match_sports
from .leaders import LeaderTrade, LeaderWatcher

log = logging.getLogger("copytrade")
GAMMA = "https://gamma-api.polymarket.com"
GAME_HOURS = {"nfl": 3.5, "cfb": 4.0, "nba": 2.75, "cbb": 2.5, "nhl": 3.0, "mlb": 3.5, "wnba": 2.5}
# shortest normal game length: the "< 1 h to resolution" rule must never let a late in-game copy through
MIN_GAME_HOURS = {"nfl": 3.0, "cfb": 3.2, "nba": 2.1, "cbb": 1.9, "nhl": 2.4, "mlb": 2.5, "wnba": 1.9}
SETTLE_CHECK_S = 120


def load_traders(cfg: dict) -> list[dict]:
    """Approved traders: copytrade.traders in config.yaml (committed, reaches the user's PC with git pull)
    plus data/copy_traders.json written by `python -m app.copytrade approve`."""
    out = {}
    for t in (cfg.get("copytrade", {}) or {}).get("traders") or []:
        if t.get("wallet"):
            out[t["wallet"].lower()] = {**t, "wallet": t["wallet"].lower()}
    f = Path(cfg.get("_root", ".")) / "data" / "copy_traders.json"
    try:
        for t in json.loads(f.read_text()).get("traders") or []:
            if t.get("wallet"):
                out.setdefault(t["wallet"].lower(), {**t, "wallet": t["wallet"].lower()})
    except (OSError, ValueError):
        pass
    return list(out.values())


def floor_tick(px: float, tick: float) -> float:
    return round(math.floor(px / tick + 1e-9) * tick, 6)


def copy_checks(*, their_price: float, book: Optional[dict], tick: float, stake: float, max_worse: float,
                min_exit_depth_usd: float, exit_band: float, max_spread: float, max_price: float = 0.99,
                min_price: float = 0.01) -> tuple[Optional[float], Optional[float], str]:
    """(limit price, best ask, '') if a $stake copy is allowed, else (None, ask, reason). Pure, unit-tested.
    Prices are for OUR side, which is the same outcome the trader bought (so directly comparable)."""
    if not book or not book.get("asks"):
        return None, None, "no sellers on Polymarket US"
    ask = book["asks"][0][0]
    limit = min(floor_tick(their_price + max_worse, tick), max_price)
    if ask > limit + 1e-9:
        return None, ask, f"US price {ask:.3f} is more than {max_worse * 100:.0f}c worse than theirs {their_price:.3f}"
    if ask < min_price:
        return None, ask, "price below 1c"
    depth_buy = sum(px * q for px, q in book["asks"] if px <= limit + 1e-9)
    if depth_buy < stake * 0.999:
        return None, ask, f"only ${depth_buy:.2f} for sale at <= {limit:.3f}"
    bids = book.get("bids") or []
    if not bids:
        return None, ask, "no buyers on Polymarket US (could not exit)"
    if ask - bids[0][0] > max_spread + 1e-9:
        return None, ask, f"spread {ask - bids[0][0]:.3f} > {max_spread:.2f} (too thin to exit)"
    depth_exit = sum(px * q for px, q in bids if px >= ask - exit_band - 1e-9)
    if depth_exit < min_exit_depth_usd:
        return None, ask, f"only ${depth_exit:.2f} of buyers within {exit_band * 100:.0f}c (need ${min_exit_depth_usd:g})"
    return limit, ask, ""


class CopyTradeModule(StrategyModule):
    name = "copytrade"

    def __init__(self, engine, cfg: dict, client: Optional[httpx.AsyncClient] = None):
        self.engine = engine
        self.cfg = cfg
        self.cc = cfg.get("copytrade", {}) or {}
        self.client = client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "btc-signals/1.0"})
        self.traders = load_traders(cfg)
        self.books = BookStore()
        self._last_shares: dict = {}
        self.us_markets: dict[str, dict] = {}       # slug -> US market JSON
        self.meta: dict[str, dict] = {}             # slug -> {"resolve_ts", "start_ts", "league", "label"}
        self.pending: list[Signal] = []
        self.sell_targets: dict[int, dict] = {}     # position id -> {"ts", "why"} (a copied sale is due)
        self.keep: dict[str, float] = {}            # position id -> share of the copy to keep (leader sells)
        self.last_copy: dict[tuple, float] = {}     # (wallet, intl token) -> when we last copied/queued a BUY
        self._last_market_try: dict[str, float] = {}
        self._last_settle_check: dict[str, float] = {}
        self.decisions: deque = deque(maxlen=200)
        self.sides_seen: dict[str, dict] = {}       # us slug -> {leg: {wallet: ts}}
        self.settled: dict[str, float] = {}
        self._last_settle_poll: dict[str, float] = {}
        self._gamma_cache: dict[str, tuple] = {}
        self._us_event_cache: dict[str, tuple] = {}
        self._pol_index: list = []
        self._pol_index_ts = 0.0
        self._gw_sem_obj: Optional[asyncio.Semaphore] = None
        self._gw_block_until = 0.0
        self.store = None
        self.trader = None
        self.watcher = LeaderWatcher(self.client, self.traders, float(self.cc.get("poll_interval_s", 3)))
        self._tasks: list[asyncio.Task] = []

    def attach(self, store, trader=None) -> None:
        self.store = store
        self.trader = trader

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.keep = {str(k): float(v) for k, v in (self.store.get_state("copy:keep") or {}).items()}
        catchup: dict = {}
        for p in self.store.active_positions():          # positions recovered after a restart
            if p.get("copy_leader") and p.get("copy_src_token"):
                k = (p["copy_leader"], p["copy_src_token"])
                self.last_copy[k] = max(self.last_copy.get(k, 0), p.get("opened_ts") or 0)
                catchup[k] = min(catchup.get(k, 1e18), p.get("opened_ts") or 0)
            if self.keep.get(str(p["id"]), 1.0) < 1.0:
                self.sell_targets[p["id"]] = {"ts": time.time(), "why": "copied sale (resumed after restart)"}
            try:
                await self._load_market(p["slug"])
            except Exception as e:  # noqa: BLE001
                log.warning("copytrade: could not load %s at startup: %s", p["slug"], repr(e))
            if p.get("copy_resolve_ts"):
                self.meta.setdefault(p["slug"], {})["resolve_ts"] = p["copy_resolve_ts"]
        self.watcher.catchup = catchup                 # their sells while we were stopped still count
        g = self.engine._guard
        for name, coro in (("copy-leaders", self.watcher.run(self.on_trade)), ("copy-books", self._books_loop()),
                           ("copy-review", self._review_loop())):
            self._tasks.append(asyncio.create_task(g(name, coro), name=name))
        log.warning("copytrade (PAPER) watching %s", ", ".join(t.get("name") or t["wallet"] for t in self.traders))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []

    # ------------------------------------------------------------ http
    @property
    def _gw_sem(self) -> asyncio.Semaphore:
        if self._gw_sem_obj is None:
            self._gw_sem_obj = asyncio.Semaphore(3)
        return self._gw_sem_obj

    async def _gw(self, path: str, tries: int = 4) -> Optional[dict]:
        """GET a Polymarket US gateway path (its own query string is KEPT: httpx >= 0.28 would drop it if we
        passed params=). 404 -> None. 429 / 5xx -> wait (Retry-After) and retry; every caller then waits too."""
        url = httpx.URL(GATEWAY + path).copy_merge_params(nocache())
        for i in range(tries):
            wait = self._gw_block_until - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 60))
            async with self._gw_sem:
                r = await self.client.get(url)
                await asyncio.sleep(0.1)                   # well under the shared 25 req/s gateway limit
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    ra = float(r.headers.get("retry-after") or 0)
                except ValueError:
                    ra = 0.0
                self._gw_block_until = time.time() + (ra or 3.0 * (i + 1))
                if i < tries - 1:
                    continue
            r.raise_for_status()
            return r.json()
        return None

    async def _gamma(self, path: str, params: dict, ttl: float = 600) -> list:
        k = path + json.dumps(params, sort_keys=True)
        hit = self._gamma_cache.get(k)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        for i in range(3):
            r = await self.client.get(GAMMA + path, params=params)
            if (r.status_code == 429 or r.status_code >= 500) and i < 2:
                await asyncio.sleep(2.0 * (i + 1))
                continue
            break
        r.raise_for_status()
        d = r.json() or []
        now = time.time()
        self._gamma_cache = {kk: v for kk, v in self._gamma_cache.items() if now - v[0] < ttl}
        if len(self._gamma_cache) >= 200:
            for kk, _ in sorted(self._gamma_cache.items(), key=lambda kv: kv[1][0])[:50]:
                self._gamma_cache.pop(kk, None)
        self._gamma_cache[k] = (now, d)
        return d

    async def _gw_event(self, path: str) -> Optional[dict]:
        """US event lookups are big (~3 MB for an NFL game): cache 10 minutes."""
        hit = self._us_event_cache.get(path)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        d = await self._gw(path)
        self._us_event_cache[path] = (time.time(), d)
        if len(self._us_event_cache) > 20:
            for k, _ in sorted(self._us_event_cache.items(), key=lambda kv: kv[1][0])[:8]:
                self._us_event_cache.pop(k, None)
        return d

    async def _load_market(self, slug: str) -> Optional[dict]:
        d = await self._gw(f"/v1/market/slug/{slug}")
        m = (d or {}).get("market")
        if m:
            self.us_markets[slug] = m
        return m

    async def _load_book(self, slug: str) -> None:
        d = await self._gw(f"/v1/markets/{slug}/book")
        if d:
            us_book_to_store(self.books, slug, d, time.time(), self._last_shares)

    # ------------------------------------------------------------ decisions log
    def _decide(self, t: Optional[LeaderTrade], action: str, reason: str, **kw) -> None:
        d = {"ts": time.time(), "action": action, "reason": reason, **kw}
        if t is not None:
            d.update({"trader": t.name, "wallet": t.wallet, "side": t.side, "market": t.title,
                      "outcome": t.outcome, "their_price": round(t.price, 4), "their_size": round(t.size, 2),
                      "lag_s": round(t.lag_s, 1), "intl_slug": t.slug})
        self.decisions.appendleft(d)
        if self.store is not None:
            self.store.event("copy", f"{action}: {d.get('trader', '')} {t.side if t else ''} "
                             f"{(t.title if t else '')[:60]} {t.outcome if t else ''} - {reason}"[:400],
                             **{k: v for k, v in d.items() if k in ("trader", "token", "their_price", "us_ask")})

    def paused(self) -> dict:
        return (self.store.get_state("copy:paused") or {}) if self.store else {}

    # ------------------------------------------------------------ leader events
    async def on_trade(self, t: LeaderTrade) -> None:
        try:
            if t.side == "BUY":
                await self._copy_buy(t)
            else:
                self._copy_sell(t)
        except httpx.HTTPError as e:
            if t.side == "BUY":                       # their next fills of this entry may still be copied
                self.last_copy.pop((t.wallet, t.token_id), None)
            self._decide(t, "skip", f"data error: {type(e).__name__}")
        except Exception as e:  # noqa: BLE001 - one odd trade must never stop copying the others
            if t.side == "BUY":
                self.last_copy.pop((t.wallet, t.token_id), None)
            log.exception("copytrade: trade %s %s", t.name, t.slug)
            self._decide(t, "skip", f"internal error: {type(e).__name__}: {e}"[:200])

    async def _map(self, t: LeaderTrade) -> tuple[Optional[dict], str]:
        """Intl trade -> US market/side, or (None, reason)."""
        mk = await self._gamma("/markets", {"condition_ids": t.condition_id}) or \
            await self._gamma("/markets", {"condition_ids": t.condition_id, "closed": "true"})
        ev = await self._gamma("/events", {"slug": t.event_slug}) if t.event_slug else []
        if not mk or not ev:
            return None, "market details not found"
        m, iev = mk[0], ev[0]
        if match_sports.league_of(t.slug):
            ue, why = await match_sports.resolve_us_event(iev, self._gw_event)
            if ue is None:
                return None, why
            try:
                r = match_sports.map_market(m, iev, ue, t.outcome_index, t.outcome)
            except match_sports.NoMatch as e:
                return None, str(e)
            lg = match_sports.league_of(t.slug)
            if r["start_ts"] is None:
                return None, "game start time unknown"
            r["resolve_ts"] = r["start_ts"] + GAME_HOURS.get(lg, 3.0) * 3600          # hold/settle timing
            r["earliest_end_ts"] = r["start_ts"] + MIN_GAME_HOURS.get(lg, 1.9) * 3600  # for the 1 h rule
            r["league"] = lg
            return r, ""
        if time.time() - self._pol_index_ts > float(self.cc.get("politics_index_refresh_s", 900)) \
                or not self._pol_index:
            self._pol_index = match_politics.build_index(await match_politics.fetch_us_politics(self._gw))
            self._pol_index_ts = time.time()
        r, why = match_politics.match(iev, m, self._pol_index, t.outcome)
        if r is None:
            return None, why or "not listed on Polymarket US"
        if r["confidence"] < float(self.cc.get("politics_min_confidence", 0.95)):
            return None, f"politics match only {r['confidence']:.2f} sure ({'; '.join(r['notes'])})"
        r["resolve_ts"] = r.get("end_ts")
        r["earliest_end_ts"] = r.get("end_ts")
        r["league"] = "politics"
        r["start_ts"] = None
        return r, ""

    async def _copy_buy(self, t: LeaderTrade) -> None:
        cc, now = self.cc, time.time()
        if t.wallet in self.paused():
            return self._decide(t, "skip", f"trader paused: {self.paused()[t.wallet]}")
        if t.lag_s > float(cc.get("max_signal_age_s", 300)):
            return self._decide(t, "skip", f"seen {t.lag_s:.0f} s after their trade (too late)")
        key = (t.wallet, t.token_id)
        same = float(cc.get("same_entry_window_s", 1800))
        if now - self.last_copy.get(key, 0) < same or any(
                p.get("copy_leader") == t.wallet and p.get("copy_src_token") == t.token_id
                and p["status"] in ("opening", "open", "closing") for p in self.store.active_positions()):
            return self._decide(t, "ignore", "more fills of an entry we already copied (one $1 copy per entry)")
        self.last_copy[key] = now
        r, why = await self._map(t)
        if r is None:
            return self._decide(t, "skip", f"not copyable on Polymarket US: {why}")
        slug, leg, token = r["us_slug"], r["leg"], r["token"]
        other = "short" if leg == "long" else "long"
        seen = self.sides_seen.setdefault(slug, {"long": {}, "short": {}})
        seen[leg][t.wallet] = now
        horizon = float(cc.get("opposite_side_window_s", 7 * 86400))
        opp = [w for w, ts in seen[other].items() if w != t.wallet and now - ts < horizon]
        if opp or any(p["slug"] == slug and p["token"] != token for p in self.store.active_positions()):
            return self._decide(t, "skip", "approved traders are on opposite sides of this market", token=token)
        m = await self._load_market(slug)
        if not m:
            self.last_copy.pop((t.wallet, t.token_id), None)
            return self._decide(t, "skip", "could not load the Polymarket US market (will retry on their next fill)",
                                token=token)
        if m.get("status") != "MARKET_STATUS_OPEN":
            return self._decide(t, "skip", "Polymarket US market is not open", token=token)
        res_ts = r.get("resolve_ts")
        if res_ts is None:
            return self._decide(t, "skip", "resolution time unknown", token=token)
        if (r.get("earliest_end_ts") or res_ts) - now < float(cc.get("min_time_to_resolution_s", 3600)):
            return self._decide(t, "skip", "market may resolve in under 1 hour", token=token)
        stake = float(cc.get("stake_usd", 1.0))
        held = sum(((p["cost"] or 0) + (p["entry_fee"] or 0)) if p["status"] != "opening" else stake
                   for p in self.store.active_positions() if p["slug"] == slug)
        if held + stake > float(cc.get("max_per_market_usd", 3.0)) + 1e-9:
            return self._decide(t, "skip", f"already ${held:.2f} in this market (max "
                                f"${float(cc.get('max_per_market_usd', 3.0)):g})", token=token)
        await self._load_book(slug)
        if self.books.get(token) is None:
            self.last_copy.pop((t.wallet, t.token_id), None)
            return self._decide(t, "skip", "could not load the US order book (will retry on their next fill)",
                                token=token)
        tick = float(m.get("orderPriceMinTickSize") or 0.01)
        limit, ask, why = copy_checks(their_price=t.price, book=self.books.get(token), tick=tick, stake=stake,
                                      max_worse=float(cc.get("max_worse", 0.03)),
                                      min_exit_depth_usd=float(cc.get("min_exit_depth_usd", 5.0)),
                                      exit_band=float(cc.get("exit_band", 0.05)),
                                      max_spread=float(cc.get("max_spread", 0.05)))
        if limit is None:
            return self._decide(t, "skip", why, token=token, us_ask=ask)
        self.meta[slug] = {"resolve_ts": res_ts, "start_ts": r.get("start_ts"), "league": r.get("league"),
                           "label": r.get("label")}
        sig = Signal(module=self.name, market_id=slug, token=token, side=r.get("label") or t.outcome,
                     fair_prob=t.price, confidence=1.0, edge=0.0,
                     reasons=[Reason(f"copy {t.name}: {t.side} {t.outcome} @ {t.price:.3f} on '{t.title}'",
                                     "polymarket.com", 0.0, t.first_ts)],
                     group=slug, league=r.get("league"), bet_type=r.get("bet_type") or "", label=t.name[:16],
                     starts_at=r.get("start_ts"), expires_at=now + 30, stake_usd=stake,
                     extra={"limit": limit, "strategy": "copy", "ask": ask, "book_ts": now,
                            "position_fields": {"copy_leader": t.wallet, "copy_leader_name": t.name,
                                                "copy_leader_price": t.price, "copy_src_token": t.token_id,
                                                "copy_src_condition": t.condition_id, "copy_resolve_ts": res_ts},
                            "_trade": t})
        self.pending.append(sig)
        self._decide(t, "copy", f"queued: buy ${stake:g} at <= {limit:.3f} (US ask {ask:.3f})", token=token,
                     us_ask=ask)

    def _copy_sell(self, t: LeaderTrade) -> None:
        """Keep the same share of each copy as the trader keeps: keep *= (1 - fraction sold). Covers copies
        still queued, whose buy order is resting, being sold, or open; a copied sale that is still waiting
        compounds correctly (50% then 50% of the rest = keep 25%)."""
        mine = [p for p in self.store.active_positions()
                if p.get("copy_leader") == t.wallet and p.get("copy_src_token") == t.token_id
                and p["status"] in ("opening", "open", "closing")]
        queued = [s for s in self.pending if (s.extra or {}).get("position_fields", {}).get("copy_leader") == t.wallet
                  and s.extra["position_fields"].get("copy_src_token") == t.token_id]
        if not mine and not queued:
            return self._decide(t, "ignore", "sell in a market we did not copy")
        if t.sell_fraction is None:
            return self._decide(t, "hold", "could not read how much they still hold: keeping our copy")
        frac = 1.0 if t.sell_fraction >= 0.98 else max(0.0, t.sell_fraction)
        why = f"{t.name} sold {t.sell_fraction:.0%} of their position"
        for p in mine:
            k = str(p["id"])
            self.keep[k] = self.keep.get(k, 1.0) * (1.0 - frac)
            self.sell_targets[p["id"]] = {"ts": time.time(), "why": why}
        for s in queued:
            s.extra["keep"] = s.extra.get("keep", 1.0) * (1.0 - frac)
            if s.extra["keep"] <= 0.02:                  # they already got out: don't buy at all
                self.pending.remove(s)
        self._save_keep()
        self._decide(t, "sell", f"selling {t.sell_fraction:.0%} of our {len(mine) + len(queued)} copy(ies)")

    def _save_keep(self) -> None:
        live = {str(p["id"]) for p in self.store.active_positions()}
        self.keep = {k: v for k, v in self.keep.items() if k in live}
        self.store.set_state("copy:keep", self.keep)

    # ------------------------------------------------------------ AutoTrader hooks
    def signals(self, now: float) -> list[Signal]:
        out = [s for s in self.pending if (s.expires_at or now + 1) > now]
        for s in self.pending:
            if s not in out:
                self._decide(s.extra.get("_trade"), "skip", "copy expired before it could be placed", token=s.token)
        self.pending = []
        return out

    def on_skip(self, sig: Signal, reason: str) -> None:
        self._decide((sig.extra or {}).get("_trade"), "skip", f"risk: {reason}", token=sig.token)

    def on_entered(self, sig: Signal, pid: int, order) -> None:
        if (sig.extra or {}).get("keep", 1.0) < 1.0:
            self.keep[str(pid)] = sig.extra["keep"]
            self.sell_targets[pid] = {"ts": time.time(), "why": "trader sold before our copy filled"}
            self._save_keep()
        self._decide((sig.extra or {}).get("_trade"), "order", f"BUY order {order.status} "
                     f"(filled {order.filled:.2f} of {order.size:.2f} @ <= {order.price:.3f})", token=sig.token,
                     position=pid)

    def market(self, slug: str) -> Optional[MarketInfo]:
        meta = self.meta.get(slug, {})
        m = self.us_markets.get(slug)
        if not m:
            if not meta.get("resolve_ts"):
                return None
            m = {}                                    # not loaded yet (retried in the books loop): safe defaults
        res = meta.get("resolve_ts")
        now = time.time()
        status = m.get("status")
        if slug in self.settled or (status and status != "MARKET_STATUS_OPEN"):
            hold = now                                # closed/resolved on Polymarket US: wait for the settlement
        elif res:                                     # still open: keep mirroring their sells (overtime, late
            grace = 30 * 86400 if meta.get("league") == "politics" else 4 * 3600   # certification); fallback only
            hold = res + float(self.cc.get("hold_grace_s", grace))
        else:
            hold = None
        min_size = float(m.get("minimumTradeQty") or 0.01)
        return MarketInfo(slug=slug, tick_size=float(m.get("orderPriceMinTickSize") or 0.01), min_size=min_size,
                          size_step=1.0 if min_size >= 1 else 0.01,
                          fee_schedule={"rate": float(m.get("feeCoefficient") or 0.095), "exponent": 1,
                                        "takerOnly": True, "round_cents": True},
                          condition_id=str(m.get("id") or ""), label=meta.get("label") or m.get("question") or slug,
                          entry_deadline=(res - float(self.cc.get("min_time_to_resolution_s", 3600))) if res else None,
                          hold_phase_from=hold, seconds_left=(res - now) if res else None)

    def book(self, token: str) -> Optional[dict]:
        return self.books.get(token)

    def fair(self, position: dict) -> tuple[Optional[float], bool]:
        return None, False

    def exit_decision(self, p: dict, val, now: float) -> ExitDecision:
        req = self.sell_targets.get(p["id"])
        if not req:
            return ExitDecision("HOLD", "holding until the trader sells or the market resolves", "copy_hold")
        target = p["shares"] * (1.0 - self.keep.get(str(p["id"]), 1.0))
        left = target - (p["exit_shares"] or 0)
        if left <= 0.0099:
            self.sell_targets.pop(p["id"], None)
            return ExitDecision("HOLD", "copied sale done", "copy_hold")
        if now - req["ts"] > float(self.cc.get("sell_retry_s", 900)):
            self.sell_targets.pop(p["id"], None)
            self.store.event("copy", f"gave up selling position {p['id']} (no buyers for 15 min): holding",
                             "WARNING")
            return ExitDecision("HOLD", "no buyers for the copied sale: holding to resolution", "copy_hold")
        if val.shares_sellable <= 0:
            return ExitDecision("HOLD", "copied sale waiting for buyers", "copy_hold")
        return ExitDecision("SELL", req["why"], "copy_sell", shares=left)

    async def refresh_settlement(self, slug: str) -> Optional[float]:
        m = await self._load_market(slug)
        if (m or {}).get("status") != "MARKET_STATUS_RESOLVED":
            return None
        s = await self._gw(f"/v1/markets/{slug}/settlement")
        if not s or s.get("settlement") is None:
            return None
        self.settled[slug] = float(s["settlement"])
        return self.settled[slug]

    def settlement_per_share(self, p: dict) -> Optional[float]:
        v = self.settled.get(p["slug"])
        if v is None:
            if time.time() - self._last_settle_poll.get(p["slug"], 0) > 120:
                self._last_settle_poll[p["slug"]] = time.time()
                asyncio.ensure_future(self._safe(self.refresh_settlement(p["slug"])))
            return None
        return v if p["token"].endswith(":long") else 1.0 - v

    def outcome_label(self, p: dict) -> str:
        v = self.settled.get(p["slug"])
        if v is None:
            return "settled"
        mine = v if p["token"].endswith(":long") else 1.0 - v
        return f"resolved: our side paid ${mine:.2f}/share"

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            log.warning("copytrade background call failed: %s", e)

    # ------------------------------------------------------------ loops
    async def _books_loop(self) -> None:
        """Keep the books of markets we hold fresh (exit valuation, paper fills, copied sells)."""
        while True:
            slugs = {p["slug"] for p in self.store.active_positions()} | {s.market_id for s in self.pending}
            now = time.time()
            for slug in slugs:
                try:
                    if slug not in self.us_markets and now - self._last_market_try.get(slug, 0) > 60:
                        self._last_market_try[slug] = now
                        await self._load_market(slug)
                    await self._load_book(slug)
                    if slug not in self.settled and now - self._last_settle_check.get(slug, 0) > SETTLE_CHECK_S:
                        self._last_settle_check[slug] = now
                        await self.refresh_settlement(slug)      # catches markets that resolve early
                except Exception as e:  # noqa: BLE001 - never let one market stop the loop
                    log.warning("copytrade book %s: %s", slug, repr(e))
            await asyncio.sleep(float(self.cc.get("book_poll_s", 4)))

    def trader_stats(self) -> dict:
        """Per copied trader: copies, open, closed, P&L, win rate, average copy cost (our price - theirs)."""
        rows = self.store.db.query("SELECT * FROM bot_positions WHERE mode=? AND venue=? AND module=? "
                                   "AND status!='void' AND copy_leader IS NOT NULL",
                                   (self.store.mode, self.store.venue, self.name))
        out = {}
        for t in self.traders:
            mine = [r for r in rows if r["copy_leader"] == t["wallet"]]
            closed = [r for r in mine if r["status"] == "closed"]
            gaps = [r["entry_price"] - r["copy_leader_price"] for r in mine
                    if r["entry_price"] is not None and r["copy_leader_price"] is not None and r["shares"]]
            pnl = sum(r["pnl"] or 0 for r in closed)
            out[t["wallet"]] = {"copies": len(mine), "open": len(mine) - len(closed), "closed": len(closed),
                                "pnl": round(pnl, 2),
                                "win_rate": (sum(1 for r in closed if (r["pnl"] or 0) > 0) / len(closed))
                                if closed else None,
                                "avg_copy_cost": (sum(gaps) / len(gaps)) if gaps else None,
                                "open_cost": round(sum((r["cost"] or 0) for r in mine if r["status"] != "closed"), 2)}
        return out

    def _check_auto_pause(self) -> None:
        n_after = int(self.cc.get("pause_after_trades", 30))
        paused = dict(self.paused())
        base = self.store.get_state("copy:unpaused") or {}      # set by `unpause`: judge only later copies
        for w, s in self.trader_stats().items():
            b = base.get(w) or {}
            closed, pnl = s["closed"] - int(b.get("closed", 0)), s["pnl"] - float(b.get("pnl", 0.0))
            if w not in paused and closed >= n_after and pnl < 0:
                name = next((t.get("name") for t in self.traders if t["wallet"] == w), w[:10])
                paused[w] = f"copies lost ${-pnl:.2f} over {closed} closed trades"
                self.store.set_state("copy:paused", paused)
                self.store.event("risk", f"copying {name} PAUSED: {paused[w]}", "WARNING")
                self.engine.alert("risk", f"Copy trading: {name} paused", paused[w] +
                                  ". Their open copies are held to resolution. "
                                  f"Resume: python -m app.copytrade unpause {name}", "warn")

    async def _review_loop(self) -> None:
        """Every minute: auto-pause check. Weekly: re-score each trader on the last 30 days, alert on a drop."""
        while True:
            try:
                self._check_auto_pause()
                now = time.time()                     # forget old opposite-side / same-entry records
                hz = float(self.cc.get("opposite_side_window_s", 7 * 86400))
                for slug in list(self.sides_seen):
                    for leg in ("long", "short"):
                        self.sides_seen[slug][leg] = {w: ts for w, ts in self.sides_seen[slug][leg].items()
                                                      if now - ts < hz}
                    if not any(self.sides_seen[slug].values()):
                        del self.sides_seen[slug]
                self.last_copy = {k: v for k, v in self.last_copy.items()
                                  if now - v < float(self.cc.get("same_entry_window_s", 1800))}
                last = float(self.store.get_state("copy:last_rescore") or 0)
                if time.time() - last > float(self.cc.get("rescore_every_days", 7)) * 86400:
                    self.store.set_state("copy:last_rescore", time.time())
                    res = await asyncio.to_thread(rescore, self.traders)
                    self.store.set_state("copy:rescore", res)
                    for w, r in res.items():
                        if r.get("alert"):
                            self.engine.alert("risk", f"Copy trading: {r['name']} performance dropped", r["alert"],
                                              "warn")
                            self.store.event("risk", f"weekly re-score {r['name']}: {r['alert']}", "WARNING")
            except Exception as e:  # noqa: BLE001
                log.warning("copytrade review: %s", e)
            await asyncio.sleep(60)

    # ------------------------------------------------------------ dashboard
    def snapshot(self) -> dict:
        now = time.time()
        stats = self.trader_stats() if self.store else {}
        resc = (self.store.get_state("copy:rescore") or {}) if self.store else {}
        paused = self.paused()
        traders = []
        for t in self.traders:
            w = t["wallet"]
            traders.append({"name": t.get("name") or w[:10], "wallet": w, "paused": paused.get(w),
                            "last_trade_ts": self.watcher.last_fill_ts.get(w),
                            "last_poll_ts": self.watcher.last_poll.get(w), "feed_error": self.watcher.errors.get(w),
                            "stats": stats.get(w, {}), "rescore": resc.get(w)})
        pos = []
        for p in (self.store.active_positions() if self.store else []):
            b = self.books.get(p["token"]) or {}
            bid = b["bids"][0][0] if b.get("bids") else None
            pos.append({"id": p["id"], "trader": p.get("copy_leader_name"), "market": p["slug"], "side": p["side"],
                        "status": p["status"], "their_price": p.get("copy_leader_price"),
                        "my_price": p["entry_price"], "shares": p["shares"], "sold": p["exit_shares"],
                        "bid": bid, "resolves": p.get("copy_resolve_ts"),
                        "value": (bid * (p["shares"] - (p["exit_shares"] or 0))) if bid is not None and p["shares"]
                        else None, "cost": p["cost"]})
        lags = sorted(self.watcher.lags)
        return {"mode": "paper", "traders": traders, "positions": pos, "decisions": list(self.decisions)[:60],
                "feed_lag_s": lags[len(lags) // 2] if lags else None,
                "rules": {k: self.cc.get(k) for k in ("stake_usd", "max_worse", "max_per_market_usd",
                                                      "min_time_to_resolution_s", "pause_after_trades")},
                "risk": (self.cc.get("risk") or {}), "ts": now}


def rescore(traders: list[dict], days: int = 30) -> dict:
    """Weekly check (runs in a worker thread): each trader's own record over the last `days` days.
    Alert when they lost money in that window (realized or on the daily P&L curve)."""
    from .intl import IntlData
    from .selection import score_trader
    api = IntlData()
    now = time.time()
    since = now - days * 86400
    out = {}
    for t in traders:
        w = t["wallet"]
        try:
            fills, trunc = api.user_trades(w, start=int(since), max_rows=20000)
            fin = api.closed_positions(w, start=int(since)) + api.resolved_open_positions(w, start=int(since))
            pts = api.user_pnl(w, interval="1m", fidelity="1d")
            s = score_trader(wallet=w, name=t.get("name") or w[:10], fills=fills, truncated=trunc, closed=fin,
                             pnl_points=pts, since=since, now=now, min_trades=0)
        except Exception as e:  # noqa: BLE001
            out[w] = {"name": t.get("name"), "ts": now, "error": str(e)[:200]}
            continue
        alert = None
        if s["entries"] == 0:
            alert = f"no trades in the last {days} days (nothing to copy)"
        elif (s["realized_pnl"] or 0) < -0.02 * (s["cost"] or 0) or \
                (s["curve_pnl"] is not None and s["curve_pnl"] < -0.02 * (s["cost"] or 0)):   # lost > 2% of stakes
            alert = (f"last {days} days: realized P&L {s['realized_pnl']:+,.0f}, curve "
                     f"{(s['curve_pnl'] or 0):+,.0f}, win rate {(s['win_rate'] or 0):.0%} over {s['entries']} trades")
        out[w] = {"name": t.get("name"), "ts": now, "entries": s["entries"], "realized_pnl": s["realized_pnl"],
                  "curve_pnl": s["curve_pnl"], "win_rate": s["win_rate"], "roi": s["roi"], "alert": alert}
    return out

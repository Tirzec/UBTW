"""Polymarket US (CFTC-regulated) BTC Up/Down markets: discovery, rules check, order books.

Verified against the live public API on 2026-11-09:
  * slugs:   cpc-btc-updown-{5m|15m}-YYYY-MM-DD-HHMMz   (window start, UTC)
  * GET https://gateway.polymarket.us/v1/market/slug/{slug} -> market.assetPriceTerms
      priceToBeat (published a few seconds after the window opens), settlementPrice,
      indexSymbol "BRTI"; market.feeCoefficient (0.095), minimumTradeQty (0.01),
      status MARKET_STATUS_OPEN / CLOSED (also before opening!) / RESOLVED
  * GET .../v1/markets/{slug}/book -> marketData.bids/offers (LONG = "Up" side prices)
      + stats.lastTradePx / sharesTraded
  * Settlement: simple average of 60 BRTI prices in the minute before open and close;
    a flat window settles Up.
Only the long (Up) instrument trades; Down is the short side at price 1 - p. The bot keeps
two synthetic books, "<slug>:long" (Up) and "<slug>:short" (Down), so the rest of the app
treats both venues the same.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from ..rules import RuleSpec
from .discovery import DURATION_SECONDS, Discovery, Window
from .polymarket_ws import BookStore

log = logging.getLogger("polymarket_us")

GATEWAY = "https://gateway.polymarket.us"


def us_slug(duration: str, start: int, asset: str = "btc") -> str:
    t = datetime.fromtimestamp(start, tz=timezone.utc)
    return f"cpc-{asset}-updown-{duration}-{t:%Y-%m-%d-%H%M}z"


def long_token(slug: str) -> str:
    return f"{slug}:long"


def short_token(slug: str) -> str:
    return f"{slug}:short"


def _amount(x) -> float | None:
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def validate_us_rules(market: dict, duration: str, expected_lookback: list[int] | None = None) -> RuleSpec:
    """Check the market text/terms match what the model assumes (60s BRTI average, >= is Up)."""
    t = market.get("assetPriceTerms") or {}
    text = market.get("description") or ""
    low = text.lower()
    spec = RuleSpec(asset=(t.get("asset") or {}).get("symbol"), duration=t.get("horizon"),
                    twap_enabled=True, twap_lookback_s=60, resolution_mechanism="exchange",
                    source_url=t.get("indexSymbol") or "", comparator=">=" if
                    "greater than or equal to" in low else None, raw_text=text)
    mm = []
    if t.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN":
        mm.append(f"market type {t.get('marketType')!r}, expected UP_DOWN")
    if spec.asset != "btc":
        mm.append(f"asset {spec.asset!r}, expected 'btc'")
    if spec.duration not in (None, duration):
        mm.append(f"horizon {spec.duration!r}, expected {duration!r}")
    if t.get("indexSymbol") != "BRTI":
        mm.append(f"index {t.get('indexSymbol')!r}, model assumes CF Benchmarks BRTI")
    if "60 brti prices" not in low or "simple average" not in low:
        mm.append("rules no longer describe a 60-price simple average (model assumes a 60s average)")
    if spec.comparator != ">=":
        mm.append("rules text lacks 'greater than or equal to' (tie handling may differ)")
    spec.mismatches = mm
    return spec


def apply_us_market(w: Window, market: dict, taker_theta_override: float | None = None) -> Window:
    """Fill a Window from a Polymarket US market payload (pure; unit-tested)."""
    t = market.get("assetPriceTerms") or {}
    w.found = True
    w.venue = "polymarket_us"
    w.title = market.get("subtitle") or market.get("question") or w.slug
    w.condition_id = str(market.get("id", ""))
    w.token_up, w.token_down = long_token(w.slug), short_token(w.slug)
    theta = taker_theta_override if taker_theta_override is not None else market.get("feeCoefficient", 0.095)
    w.fee_schedule = {"rate": float(theta), "exponent": 1, "takerOnly": True, "round_cents": True}
    w.tick_size = float(market.get("orderPriceMinTickSize") or 0.01)
    w.min_size = float(market.get("minimumTradeQty") or 1)
    w.size_step = 0.01 if w.min_size < 1 else 1.0     # a few US markets trade whole contracts only
    status = market.get("status", "")
    w.accepting_orders = status == "MARKET_STATUS_OPEN"
    w.rules = validate_us_rules(market, w.duration)
    ptb = _amount(t.get("priceToBeat"))
    if ptb is not None:
        w.gamma_ptb = ptb
    final = _amount(t.get("settlementPrice"))
    if final is not None:
        w.final_price = final
    if status == "MARKET_STATUS_RESOLVED" and w.final_price is not None and w.gamma_ptb is not None:
        w.outcome = "UP" if w.final_price >= w.gamma_ptb else "DOWN"   # flat window settles Up
    return w


class UsDiscovery(Discovery):
    """Same interface as Discovery; talks to Polymarket US's public gateway."""

    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None):
        super().__init__(cfg, client)
        uc = cfg.get("polymarket_us", {}) or {}
        self.gateway = uc.get("gateway_url", GATEWAY)
        self.durations = uc.get("durations", ["5m", "15m"])
        self.interval = float(uc.get("discovery_interval_s", 5))
        self.theta_override = uc.get("taker_fee_theta")
        self.site_url = None

    def get(self, duration: str, start: int) -> Window:
        slug = us_slug(duration, start)
        w = self.windows.get(slug)
        if w is None:
            w = Window(duration, start, start + DURATION_SECONDS[duration], slug, venue="polymarket_us")
            self.windows[slug] = w
        return w

    async def refresh(self, w: Window) -> None:
        r = await self.client.get(f"{self.gateway}/v1/market/slug/{w.slug}", params=nocache())
        w.last_fetch = time.time()
        if r.status_code == 404:
            return
        r.raise_for_status()
        m = r.json().get("market") or {}
        if m:
            apply_us_market(w, m, None if self.theta_override in (None, "") else float(self.theta_override))


def nocache(params: dict | None = None) -> dict:
    """The public gateway is behind Cloudflare with `cache-control: public, max-age=30`: a plain
    GET can return a book up to 30 s old. A unique query parameter forces a fresh response."""
    return {**(params or {}), "_": str(time.time_ns())}


def book_time(data: dict, now: float) -> float:
    """When the exchange produced this book (`transactTime`), never later than `now`.
    Staleness checks then measure the exchange's age of the data, not when we received it."""
    md = data.get("marketData") or data
    t = md.get("transactTime")
    if not t:
        return now
    try:
        from datetime import datetime
        return min(now, datetime.fromisoformat(str(t)[:26].rstrip("Z") + "+00:00").timestamp())
    except ValueError:
        return now


def us_book_to_store(store: BookStore, slug: str, data: dict, now: float, last_shares: dict) -> None:
    """Write a /book response into the long (Up) and derived short (Down) books (pure)."""
    md = data.get("marketData") or data
    exch = book_time(data, now)           # exchange time of the snapshot (= now if it has none)
    cur = store.book(long_token(slug))
    if getattr(cur, "exch_ts", 0) and exch < cur.exch_ts - 1e-6:
        return                            # older than what we already have (websocket vs poll race)
    # freshness is when WE received it (requests bypass the cache); transactTime is the time of the
    # book's last change, so a quiet but current book must not look stale
    bids = [(_amount(x.get("px")), float(x.get("qty") or 0)) for x in md.get("bids") or []]
    offers = [(_amount(x.get("px")), float(x.get("qty") or 0)) for x in md.get("offers") or []]
    bids = [(p, q) for p, q in bids if p is not None and q > 0]
    offers = [(p, q) for p, q in offers if p is not None and q > 0]
    fmt = lambda lv: [{"price": f"{p:.4f}", "size": f"{q}"} for p, q in lv]  # noqa: E731
    store.book(long_token(slug)).snapshot(fmt(bids), fmt(offers), now)
    # Down side: buying Down at x == selling Up at 1-x
    store.book(short_token(slug)).snapshot(fmt([(round(1 - p, 4), q) for p, q in offers]),
                                           fmt([(round(1 - p, 4), q) for p, q in bids]), now)
    store.book(long_token(slug)).exch_ts = exch
    store.book(short_token(slug)).exch_ts = exch
    st = md.get("stats") or {}
    shares = st.get("sharesTraded")
    px = _amount(st.get("lastTradePx"))
    if shares is not None and px is not None:
        shares = float(shares)
        prev = last_shares.get(slug)
        last_shares[slug] = shares
        if prev is not None and shares > prev:
            q = shares - prev
            for tok, p in ((long_token(slug), px), (short_token(slug), round(1 - px, 4))):
                b = store.book(tok)
                b.last_trade = p
                b.trades.append((now, p, q))


class UsBookPoller:
    """Same interface as ClobMarketStream, fed by polling the public /book endpoint
    (well inside the 25 req/s limit: 2 markets x 1/s)."""

    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None):
        uc = cfg.get("polymarket_us", {}) or {}
        self.gateway = uc.get("gateway_url", GATEWAY)
        self.interval = float(uc.get("book_poll_interval_s", 1.0))
        self.client = client or httpx.AsyncClient(timeout=10)
        self.store = BookStore()
        self._slugs: set[str] = set()
        self.fast_slugs: set[str] = set()               # open positions: polled every fast_interval
        self.fast_interval = float(uc.get("book_poll_fast_interval_s", 0.25))
        self._last_shares: dict[str, float] = {}
        self.connected = False
        self.last_msg = 0.0
        self.last_error = ""
        self.reconnects = 0

    def set_assets(self, assets: set[str]) -> None:
        self._slugs = {a.rsplit(":", 1)[0] for a in assets if a}
        if getattr(self, "ws", None) is not None:
            self.ws.set_slugs(self._slugs)

    async def poll_slug(self, slug: str) -> None:
        r = await self.client.get(f"{self.gateway}/v1/markets/{slug}/book", params=nocache())
        if r.status_code == 404:
            return
        r.raise_for_status()
        us_book_to_store(self.store, slug, r.json(), time.time(), self._last_shares)
        self.last_msg = time.time()

    async def refresh_rest(self, token: str) -> None:
        await self.poll_slug(token.rsplit(":", 1)[0])

    def set_fast(self, slugs: set[str]) -> None:
        self.fast_slugs = set(slugs)

    async def run_fast(self) -> None:
        """Markets with an open position: poll ~4x a second so exits react quickly."""
        while True:
            t0 = time.time()
            if self.fast_slugs:
                try:
                    await asyncio.gather(*(self.poll_slug(s) for s in list(self.fast_slugs)))
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"fast poll: {type(e).__name__}: {e}"[:200]
            await asyncio.sleep(max(0.05, self.fast_interval - (time.time() - t0)))

    async def run(self) -> None:
        asyncio.ensure_future(self.run_fast())
        while True:
            t0 = time.time()
            try:
                await asyncio.gather(*(self.poll_slug(s) for s in list(self._slugs - self.fast_slugs)))
                self.connected = bool(self._slugs)
                self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.connected = False
                self.reconnects += 1
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("Polymarket US book poll failed: %s", self.last_error)
            await asyncio.sleep(max(0.1, self.interval - (time.time() - t0)))


class UsBookStream:
    """Real-time order books over the authenticated Markets WebSocket (docs: /api-reference/websocket/
    markets): wss://api.polymarket.us/v1/ws/markets, handshake signed like REST (timestamp + "GET" +
    path), SUBSCRIPTION_TYPE_MARKET_DATA per market slug. Writes the same BookStore as the poller;
    the newest snapshot (exchange transactTime) wins, so polling stays on as a backup."""

    URL = "wss://api.polymarket.us/v1/ws/markets"
    PATH = "/v1/ws/markets"

    def __init__(self, store: BookStore, last_shares: dict, key_id: str, secret: str, url: str = URL):
        self.store, self.last_shares = store, last_shares
        self.key_id, self.secret, self.url = key_id, secret, url
        self.wanted: set[str] = set()
        self.subscribed: set[str] = set()
        self.ws = None
        self.connected = False
        self.last_msg = 0.0
        self.messages = 0
        self.reconnects = 0
        self.last_error = ""
        self._req = 0

    def set_slugs(self, slugs: set[str]) -> None:
        self.wanted = set(slugs)
        if self.ws is not None and self.connected:
            asyncio.ensure_future(self._subscribe(self.wanted - self.subscribed))

    async def _subscribe(self, slugs: set[str]) -> None:
        slugs = sorted(slugs)
        for i in range(0, len(slugs), 100):                     # max 100 markets per subscription
            chunk = slugs[i:i + 100]
            self._req += 1
            await self.ws.send(json.dumps({"subscribe": {"requestId": f"md-{self._req}",
                                                         "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                                         "marketSlugs": chunk}}))
            self.subscribed |= set(chunk)

    def _headers(self) -> dict:
        from ..trading.us_broker import Signer
        h = Signer(self.key_id, self.secret).headers("GET", self.PATH)
        h.pop("Content-Type", None)
        return h

    async def _connect(self):
        import websockets
        try:
            return await websockets.connect(self.url, additional_headers=self._headers(), max_size=2 ** 22,
                                            ping_interval=15)
        except TypeError:                                         # websockets < 14
            return await websockets.connect(self.url, extra_headers=self._headers(), max_size=2 ** 22,
                                            ping_interval=15)

    def handle(self, raw) -> None:
        msg = json.loads(raw)
        md = msg.get("marketData") if isinstance(msg, dict) else None
        if md and md.get("marketSlug"):
            us_book_to_store(self.store, md["marketSlug"], msg, time.time(), self.last_shares)
            self.last_msg = time.time()
            self.messages += 1

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                self.ws = await self._connect()
                self.connected, self.subscribed = True, set()
                backoff = 1.0
                await self._subscribe(self.wanted)
                async for raw in self.ws:
                    try:
                        self.handle(raw)
                    except Exception as e:  # noqa: BLE001 - one malformed frame must not drop the feed
                        self.last_error = f"bad frame: {type(e).__name__}: {e}"[:200]
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("book websocket: %s (reconnecting in %.0fs)", self.last_error, backoff)
            self.connected = False
            self.reconnects += 1
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def status(self) -> dict:
        return {"connected": self.connected, "messages": self.messages, "reconnects": self.reconnects,
                "last_msg_age": round(time.time() - self.last_msg, 1) if self.last_msg else None,
                "error": self.last_error, "markets": len(self.subscribed)}

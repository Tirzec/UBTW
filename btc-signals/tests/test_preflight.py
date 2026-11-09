"""Preflight: read-only (GET only), PASS/FAIL logic with a fake exchange."""
import base64
import json

import httpx
import pytest

from app import preflight
from app.preflight import Account, check_account, check_crypto_markets, check_risk, parse_book

SECRET = base64.b64encode(bytes(range(64))).decode()


def _account(handler):
    return Account("kid", SECRET, "https://api.test", httpx.Client(transport=httpx.MockTransport(handler)))


def _exchange(balance=50.0, orders=(), positions=(), status=200, seen=None):
    def h(req: httpx.Request):
        if seen is not None:
            seen.append((req.method, req.url.path))
        assert req.method == "GET", "preflight must never send anything but GET"
        assert req.headers["X-PM-Access-Key"] == "kid" and req.headers["X-PM-Signature"]
        if status != 200:
            return httpx.Response(status, text="unauthorized")
        if req.url.path == "/v1/account/balances":
            return httpx.Response(200, json={"balances": [{"currency": "USD", "buyingPower": str(balance),
                                                           "currentBalance": str(balance)}]})
        if req.url.path == "/v1/orders/open":
            return httpx.Response(200, json={"orders": list(orders)})
        if req.url.path == "/v1/portfolio/positions":
            return httpx.Response(200, json={"positions": list(positions)})
        return httpx.Response(404)
    return h


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("POLYMARKET_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_SECRET_KEY", SECRET)


def _by(checks):
    return {c.name: c for c in checks}


def test_account_checks_pass_and_only_get(monkeypatch):
    seen = []
    c = _by(check_account({}, 50.0, _account(_exchange(seen=seen))))
    assert c["API authentication"].status == "PASS"
    assert c["balance matches app"].status == "PASS" and "$50.00" in c["balance matches app"].detail
    assert c["open orders"].status == "PASS" and c["open positions"].status == "INFO"
    assert {m for m, _ in seen} == {"GET"} and len(seen) == 3


def test_balance_mismatch_and_manual_check():
    assert _by(check_account({}, 75.0, _account(_exchange())))["balance matches app"].status == "FAIL"
    assert _by(check_account({}, None, _account(_exchange())))["balance matches app"].status == "CHECK"


def test_low_balance_and_open_orders_warn():
    c = _by(check_account({}, None, _account(_exchange(balance=5, orders=[{"marketSlug": "m", "state": "x"}]))))
    assert c["balance above minimum"].status == "FAIL"
    assert c["open orders"].status == "WARN" and "cancels ALL open orders" in c["open orders"].detail


def test_auth_failure_and_missing_keys(monkeypatch):
    c = check_account({}, None, _account(_exchange(status=401)))
    assert c[0].status == "FAIL" and "clock" in c[0].detail
    monkeypatch.delenv("POLYMARKET_SECRET_KEY")
    c = check_account({}, None)
    assert c[0].status == "FAIL" and "POLYMARKET_SECRET_KEY" in c[0].detail


def test_crypto_market_and_book_checks(monkeypatch):
    market = {"market": {"slug": "x", "status": "MARKET_STATUS_OPEN", "feeCoefficient": 0.095,
                         "orderPriceMinTickSize": 0.01, "minimumTradeQty": 0.01}}
    book = {"marketData": {"bids": [{"px": {"value": "0.48"}, "qty": "10"}],
                           "offers": [{"px": {"value": "0.50"}, "qty": "12"}]}}

    def h(req):
        assert req.method == "GET"
        return httpx.Response(200, json=book if req.url.path.endswith("/book") else market)
    cfg = {"polymarket_us": {"durations": ["5m"]}}
    client = httpx.Client(transport=httpx.MockTransport(h))
    out = check_crypto_markets(cfg, client, now=1_800_000_100)
    assert out[0].status == "FAIL" and "rules mismatch" in out[0].detail      # no rules text -> refuse
    assert out[1].name == "crypto BTC 5m order book" and out[1].status == "PASS"

    from app.ingest import us_markets
    from app.rules import RuleSpec
    monkeypatch.setattr(us_markets, "validate_us_rules",
                        lambda m, d, *a: RuleSpec("btc", d, True, 60, "x", "", ">=", ""))
    out = check_crypto_markets(cfg, client, now=1_800_000_100)
    assert out[0].status == "PASS" and "tick 0.01" in out[0].detail and "0.095" in out[0].detail


def test_risk_sanity():
    cfg = {"trading": {"risk": {"max_stake_usd": 5, "daily_loss_limit_usd": 25, "max_total_exposure_usd": 15,
                                "max_consecutive_losses": 5}},
           "sports": {"risk": {"max_stake_usd": 50, "daily_loss_limit_usd": 30, "max_total_exposure_usd": 40,
                               "max_consecutive_losses": 6}},
           "risk_global": {"daily_loss_limit_usd": 40, "max_total_exposure_usd": 60}}
    c = _by(check_risk(cfg, buying_power=50))
    assert c["risk limits (crypto)"].status == "PASS"
    assert c["risk limits (sports)"].status == "FAIL"                 # stake > daily loss limit
    assert c["daily loss limit vs balance"].status == "WARN"          # $40 is 80% of $50


def test_parse_book_sorted():
    bids, asks = parse_book({"bids": [{"px": {"value": "0.4"}, "qty": "1"}, {"px": {"value": "0.45"}, "qty": "2"}],
                             "offers": [{"px": "0.6", "qty": "1"}, {"px": "0.55", "qty": "0"}]})
    assert bids[0] == (0.45, 2.0) and asks == [(0.6, 1.0)]


def test_account_client_has_no_write_methods():
    assert not any(hasattr(Account, m) for m in ("post", "put", "delete", "patch", "place_limit", "cancel"))

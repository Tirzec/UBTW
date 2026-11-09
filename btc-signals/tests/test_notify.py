import asyncio

import httpx

from app.notify import Notifier


def make(monkeypatch, handler, **env):
    for k in ("NTFY_TOPIC", "NTFY_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Notifier({"notifications": {"kinds": ["signal", "exit"], "min_interval_s": 60}}, client)


def test_disabled_without_config(monkeypatch):
    n = make(monkeypatch, lambda r: httpx.Response(200))
    assert not n.enabled and not n.wants({"kind": "signal", "title": "x"})


def test_ntfy_request_and_dedupe(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200)

    n = make(monkeypatch, handler, NTFY_TOPIC="my-secret-topic")
    a = {"kind": "signal", "title": "5m: BUY UP @ ≤0.55", "body": "edge +5%"}
    assert n.wants(a) and not n.wants(a)                       # same alert within 60s is dropped
    assert not n.wants({"kind": "settled", "title": "y"})      # kind not enabled
    assert asyncio.run(n.send(a))
    import json
    r = seen[0]
    body = json.loads(r.content)
    assert str(r.url).rstrip("/") == "https://ntfy.sh"
    assert body == {"topic": "my-secret-topic", "title": "5m: BUY UP @ ≤0.55", "message": "edge +5%",
                    "priority": 4, "tags": ["chart_with_upwards_trend"]}


def test_telegram_and_failure_is_swallowed(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(500)

    n = make(monkeypatch, handler, TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="42")
    assert n.channels == ["telegram"]
    assert not asyncio.run(n.send({"kind": "exit", "title": "SELL", "body": "edge gone"}))
    assert "api.telegram.org/botT/sendMessage" in str(seen[0].url) and n.last_error

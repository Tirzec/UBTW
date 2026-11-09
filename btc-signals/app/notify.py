"""Phone push notifications (ntfy or Telegram) for alerts.

ntfy (default): install the free "ntfy" app, subscribe to a topic name only you know,
put the same name in .env as NTFY_TOPIC. No account needed.
Telegram: create a bot with @BotFather, put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

Send a test push:  python -m app.notify
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("notify")

PRIORITY = {"signal": 4, "exit": 4, "degraded": 3, "settled": 2, "rules": 4, "test": 3}
TAGS = {"signal": "chart_with_upwards_trend", "exit": "rotating_light", "degraded": "warning",
        "settled": "checkered_flag", "rules": "no_entry", "test": "white_check_mark"}


class Notifier:
    def __init__(self, cfg: dict, client: httpx.AsyncClient | None = None):
        nc = cfg.get("notifications", {}) or {}
        self.kinds = set(nc.get("kinds", ["signal", "exit", "degraded", "rules"]))
        self.min_interval_s = float(nc.get("min_interval_s", 2))
        self.click_url = nc.get("click_url") or ""
        self.ntfy_server = (nc.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.ntfy_token = os.environ.get("NTFY_TOKEN", "").strip()
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = bool(nc.get("enabled", True)) and bool(
            self.ntfy_topic or (self.tg_token and self.tg_chat))
        self.client = client or httpx.AsyncClient(timeout=10)
        self._last: dict[str, float] = {}
        self.sent = 0
        self.last_error = ""

    @property
    def channels(self) -> list[str]:
        out = []
        if self.ntfy_topic:
            out.append("ntfy")
        if self.tg_token and self.tg_chat:
            out.append("telegram")
        return out

    def status(self) -> dict:
        return {"enabled": self.enabled, "channels": self.channels, "sent": self.sent,
                "kinds": sorted(self.kinds), "error": self.last_error}

    def wants(self, alert: dict) -> bool:
        if not self.enabled or alert.get("kind") not in self.kinds:
            return False
        key = f"{alert.get('kind')}:{alert.get('title')}"
        now = time.time()
        if now - self._last.get(key, 0) < self.min_interval_s:
            return False          # de-duplicate bursts of the same alert
        self._last[key] = now
        return True

    def submit(self, alert: dict) -> None:
        """Fire-and-forget from sync code inside the event loop."""
        if self.wants(alert):
            asyncio.create_task(self.send(alert))

    async def send(self, alert: dict) -> bool:
        title = alert.get("title", "BTC signal")
        body = alert.get("body", "") or title
        kind = alert.get("kind", "test")
        ok = True
        try:
            if self.ntfy_topic:
                # JSON publishing (POST to the server root) handles any unicode text
                msg = {"topic": self.ntfy_topic, "title": title, "message": body,
                       "priority": PRIORITY.get(kind, 3), "tags": [TAGS.get(kind, "bell")]}
                if self.click_url:
                    msg["click"] = self.click_url
                headers = {"Authorization": f"Bearer {self.ntfy_token}"} if self.ntfy_token else {}
                r = await self.client.post(self.ntfy_server, json=msg, headers=headers)
                r.raise_for_status()
            if self.tg_token and self.tg_chat:
                r = await self.client.post(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    json={"chat_id": self.tg_chat, "text": f"{title}\n{body}",
                          "disable_web_page_preview": True})
                r.raise_for_status()
            self.sent += 1
            self.last_error = ""
        except Exception as e:  # noqa: BLE001 - never let alerts break the engine
            ok = False
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("push notification failed: %s", self.last_error)
        return ok


async def _test() -> None:
    from .config import load_config

    n = Notifier(load_config())
    if not n.channels:
        print("No channel configured. Set NTFY_TOPIC (or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) in .env")
        return
    ok = await n.send({"kind": "test", "title": "BTC signals: test alert",
                       "body": "Phone alerts are working."})
    print(f"sent via {', '.join(n.channels)}" if ok else f"failed: {n.last_error}")
    await n.client.aclose()


if __name__ == "__main__":
    asyncio.run(_test())

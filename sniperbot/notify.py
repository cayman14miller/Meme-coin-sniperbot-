"""Notifications to the dashboard + Telegram / Discord / ntfy (phone push).

Also runs an optional Telegram command listener so the bot can be controlled
from your phone:  /status  /on  /off  /resume  /panic
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from .config import Secrets
from .store import Store

log = logging.getLogger(__name__)

ICONS = {"info": "ℹ️", "trade": "💱", "win": "✅", "loss": "🔻", "warning": "⚠️", "risk": "🛑", "error": "❌"}


class Notifier:
    def __init__(self, store: Store, secrets: Secrets) -> None:
        self.store = store
        self.s = secrets
        self.client = httpx.AsyncClient(timeout=10)
        self._tasks: set[asyncio.Task] = set()

    @property
    def channels(self) -> list[str]:
        out = ["dashboard"]
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            out.append("telegram")
        if self.s.discord_webhook_url:
            out.append("discord")
        if self.s.ntfy_topic:
            out.append("ntfy")
        return out

    def send(self, level: str, title: str, message: str = "", push: bool = True) -> None:
        """Record an event and (optionally) push it to external channels without blocking."""
        self.store.add_event(level, title, message)
        log.info("[%s] %s %s", level, title, message)
        if not push:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._push(level, title, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _push(self, level: str, title: str, message: str) -> None:
        icon = ICONS.get(level, "")
        text = f"{icon} {title}\n{message}".strip()
        jobs = []
        if "telegram" in self.channels:
            jobs.append(self.client.post(f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                                         json={"chat_id": self.s.telegram_chat_id, "text": text,
                                               "disable_web_page_preview": True}))
        if "discord" in self.channels:
            jobs.append(self.client.post(self.s.discord_webhook_url, json={"content": text[:1900]}))
        if "ntfy" in self.channels:
            jobs.append(self.client.post(f"{self.s.ntfy_server.rstrip('/')}/{self.s.ntfy_topic}",
                                         content=message.encode() or title.encode(),
                                         headers={"Title": title.encode("ascii", "ignore").decode() or "sniperbot",
                                                  "Priority": "urgent" if level == "risk" else "default",
                                                  "Tags": level}))
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("notification failed: %s", res)

    async def telegram_commands(self, handler: Callable[[str], Awaitable[str]]) -> None:
        """Long-poll Telegram for commands from the configured chat only."""
        if "telegram" not in self.channels:
            return
        base = f"https://api.telegram.org/bot{self.s.telegram_bot_token}"
        offset = 0
        while True:
            try:
                r = await self.client.get(f"{base}/getUpdates", params={"offset": offset, "timeout": 25},
                                          timeout=35)
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != str(self.s.telegram_chat_id):
                        continue  # ignore strangers
                    text = (msg.get("text") or "").strip()
                    if text.startswith("/"):
                        reply = await handler(text.split()[0].split("@")[0].lower())
                        await self.client.post(f"{base}/sendMessage",
                                               json={"chat_id": self.s.telegram_chat_id, "text": reply})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network hiccups etc.
                log.debug("telegram poll error: %s", exc)
                await asyncio.sleep(5)

    async def close(self) -> None:
        await self.client.aclose()

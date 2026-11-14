"""Alert-only Telegram sender. No secrets or chat IDs are logged."""
import asyncio
import logging
import os
import time

import aiohttp

import config as cfg
from data import Slots

log = logging.getLogger("telegram")


def batches(messages, limit=3700):
    part = ""
    for text in messages:
        if part and len(part) + len(text) + 2 > limit:
            yield part
            part = ""
        part += ("\n\n" if part else "") + text
    if part:
        yield part


class Telegram:
    def __init__(self, session, lane, dry):
        self.session, self.dry = session, dry
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chats = list(dict.fromkeys(x.strip() for x in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()))
        self.slots = Slots(lane, cfg.TELEGRAM_SLOT_SECONDS)
        if not dry and (not self.token or not self.chats or any(not x.lstrip("-").isdigit() for x in self.chats)):
            raise ValueError("Missing Telegram bot token or numeric chat IDs")

    async def send(self, messages, deadline=None):
        if not messages:
            return 0
        if self.dry:
            for message in messages:
                print("DRY_RUN " + message, flush=True)
            return 0
        expires = deadline or time.time() + 85
        failed = 0
        for text in batches(messages):
            for i, chat in enumerate(self.chats, 1):
                for attempt in range(3):
                    try:
                        await self.slots.wait(expires)
                        async with self.session.post(
                            f"https://api.telegram.org/bot{self.token}/sendMessage",
                            json={"chat_id": chat, "text": text},
                            timeout=aiohttp.ClientTimeout(total=6),
                        ) as response:
                            payload = await response.json(content_type=None)
                            if response.status == 200 and payload.get("ok") is True:
                                break
                            if response.status == 429 and attempt < 2:
                                delay = max(1, int(payload.get("parameters", {}).get("retry_after", 2)))
                                if time.time() + delay < expires:
                                    await asyncio.sleep(delay)
                                    continue
                            log.error("telegram_failed recipient=%s HTTP=%s", i, response.status)
                    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
                        # A timeout may mean Telegram accepted the message. Don't duplicate it.
                        log.error("telegram_failed recipient=%s type=%s", i, type(exc).__name__)
                    failed += 1
                    break
        return failed

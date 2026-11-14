"""Send scanner alerts through two separate Telegram bot/chat pairs."""
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
        self.session = session
        self.dry = dry
        self.slots = Slots(lane, cfg.TELEGRAM_SLOT_SECONDS)
        self.pairs = []

        if not dry:
            for number in (1, 2):
                token_name = f"TELEGRAM_BOT_TOKEN_{number}"
                chat_name = f"TELEGRAM_CHAT_ID_{number}"
                token = os.getenv(token_name, "").strip()
                chat = os.getenv(chat_name, "").strip()

                if not token:
                    raise ValueError(f"Missing secret: {token_name}")
                if not chat or not chat.lstrip("-").isdigit():
                    raise ValueError(f"{chat_name} must be one numeric chat ID")

                self.pairs.append((number, token, chat))

        # Keep the existing connection test's success count working.
        self.chats = [chat for _, _, chat in self.pairs]

    async def send(self, messages, deadline=None):
        if not messages:
            return 0
        if self.dry:
            for message in messages:
                print("DRY_RUN " + message, flush=True)
            return 0

        expires = deadline if deadline is not None else time.time() + 85
        failed = 0

        for text in batches(messages):
            for number, token, chat in self.pairs:
                delivered = False
                for attempt in range(3):
                    try:
                        await self.slots.wait(expires)
                        remaining = expires - time.time()
                        if remaining <= 0:
                            log.error("telegram_failed pair=%s deadline elapsed", number)
                            break

                        async with self.session.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat, "text": text},
                            timeout=aiohttp.ClientTimeout(total=min(6, remaining)),
                        ) as response:
                            payload = await response.json(content_type=None)
                            if response.status == 200 and payload.get("ok") is True:
                                delivered = True
                                log.info("telegram_ok pair=%s", number)
                                break

                            if response.status == 429 and attempt < 2:
                                delay = max(
                                    1, int(payload.get("parameters", {}).get("retry_after", 2))
                                )
                                if time.time() + delay < expires:
                                    await asyncio.sleep(delay)
                                    continue

                            log.error("telegram_failed pair=%s HTTP=%s", number, response.status)
                            break
                    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
                        log.error("telegram_failed pair=%s type=%s", number, type(exc).__name__)
                        break

                if not delivered:
                    failed += 1

        return failed

"""Allowlisted Dhan REST calls and shared-account request pacing."""
import asyncio
import logging
import math
import random
import time
from datetime import datetime, time as daytime

import aiohttp

import config as cfg
from clock import floor
from instruments import Bar

log = logging.getLogger("data")
BASE = "https://api.dhan.co/v2"
ENDPOINTS = frozenset(("/charts/historical", "/charts/intraday", "/marketfeed/ltp"))


class APIError(RuntimeError):
    pass


class CredentialsError(APIError):
    pass


def retry_after(headers, default):
    try:
        return max(0, float(headers.get("Retry-After", default)))
    except (ValueError, TypeError):
        return default


class Slots:
    """Clock-aligned independent runner lanes, shared across a single workflow."""
    def __init__(self, lane, width, lanes=len(cfg.GROUPS)):
        self.lane, self.width, self.lanes = lane, width, lanes
        self.used = -1
        self.lock = asyncio.Lock()

    async def wait(self, deadline=float("inf")):
        async with self.lock:
            while True:
                n = time.time()
                slot = math.floor(n / self.width)
                slot += (self.lane - slot) % self.lanes
                if slot <= self.used or n - slot * self.width > cfg.SLOT_LATE_TOLERANCE:
                    slot += self.lanes
                when = slot * self.width
                if when >= deadline:
                    raise APIError("Request deadline elapsed")
                await asyncio.sleep(max(0, when - n))
                if time.time() - when <= cfg.SLOT_LATE_TOLERANCE:
                    self.used = slot
                    return


class Dhan:
    def __init__(self, session, client, token, lane):
        if not client or not token:
            raise CredentialsError("DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN is missing")
        self.session = session
        self.headers = {"client-id": client, "access-token": token, "Accept": "application/json"}
        self.data = Slots(lane, cfg.DATA_SLOT_SECONDS)
        self.quotes = Slots(lane, cfg.QUOTE_SLOT_SECONDS)

    async def request(self, path, payload, deadline=float("inf")):
        if path not in ENDPOINTS:
            raise ValueError("Unapproved endpoint")
        gate = self.quotes if path == "/marketfeed/ltp" else self.data
        reason = "unknown"
        for attempt in range(cfg.HTTP_ATTEMPTS):
            await gate.wait(deadline)
            delay = 1.4 * 2**attempt + random.random() * .5
            try:
                timeout = min(cfg.HTTP_TIMEOUT, max(.1, deadline-time.time()))
                async with self.session.post(BASE + path, json=payload, headers=self.headers,
                                             timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                    status = response.status
                    try:
                        obj = await response.json(content_type=None)
                    except ValueError:
                        obj = {}
                    code = str(obj.get("errorCode", "")) if isinstance(obj, dict) else ""
                    retry_codes = {"DH-904", "DH-908", "DH-909", "800", "805"}
                    if ((status in {401, 403} and code not in retry_codes)
                            or code in {"DH-901", "DH-902", "806", "807", "808", "809", "810"}):
                        raise CredentialsError(f"Dhan authentication/data subscription rejected (HTTP {status}, {code or 'no code'})")
                    if status == 200 and isinstance(obj, dict) and not code and obj.get("status") != "failure":
                        return obj
                    reason = f"HTTP {status}, code {code or 'none'}"
                    if status != 429 and status < 500 and code not in retry_codes:
                        raise APIError(f"Dhan {path}: {reason}")
                    delay = max(delay, retry_after(response.headers, delay), 6 if code == "805" else 0)
            except (aiohttp.ClientError, TimeoutError) as exc:
                reason = type(exc).__name__
            if attempt + 1 < cfg.HTTP_ATTEMPTS and time.time() + delay < deadline:
                log.warning("dhan_retry endpoint=%s reason=%s attempt=%s", path, reason, attempt+1)
                await asyncio.sleep(delay)
            else:
                break
        raise APIError(f"Dhan {path}: {reason}")

    async def candles(self, inst, start, stop, interval=None, deadline=float("inf")):
        daily = interval is None
        fmt = "%Y-%m-%d" if daily else "%Y-%m-%d %H:%M:%S"
        payload = {"securityId": str(inst.sid), "exchangeSegment": inst.segment,
                   "instrument": inst.kind, "oi": False, "fromDate": start.strftime(fmt),
                   "toDate": stop.strftime(fmt)}
        if not daily:
            payload["interval"] = str(interval)
        obj = await self.request("/charts/historical" if daily else "/charts/intraday", payload, deadline)
        return parse_bars(obj, stop, interval)

    async def ltp(self, instruments, deadline):
        found = {}
        instruments = list(dict.fromkeys(instruments))
        for offset in range(0, len(instruments), 1000):
            batch = {}
            for inst in instruments[offset:offset+1000]:
                batch.setdefault(inst.segment, []).append(inst.sid)
            response = await self.request("/marketfeed/ltp", batch, deadline)
            for segment, entries in response.get("data", {}).items():
                for sid, item in entries.items():
                    price = item.get("last_price")
                    if isinstance(price, (float, int)) and math.isfinite(price) and price > 0:
                        found[segment, int(sid)] = price
        return found


def parse_bars(obj, stop, interval):
    keys = ("timestamp", "open", "high", "low", "close")
    columns = [obj.get(k) for k in keys]
    if not all(isinstance(x, list) for x in columns):
        raise APIError("Dhan candle response missing expected arrays")
    columns.append(obj.get("volume") or [0] * len(columns[0]))
    if len({len(x) for x in columns}) != 1:
        raise APIError("Dhan candle response has mismatched lengths")
    from datetime import timedelta
    result = {}
    for epoch, o, h, l, c, v in zip(*columns):
        try:
            t = datetime.fromtimestamp(int(epoch), cfg.IST)
            o, h, l, c = map(float, (o,h,l,c))
            v = int(v or 0)
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if not (all(math.isfinite(x) and x > 0 for x in (o,h,l,c))
                and l <= min(o,c) <= max(o,c) <= h and v >= 0):
            continue
        if interval is None:
            if t.date() < stop.date():
                result[t.date()] = Bar(t, o, h, l, c, v)
        elif (floor(t, interval) == t and cfg.OPEN <= t.time() < cfg.SESSION_CLOSE
              and t + timedelta(minutes=interval) <= stop):
            result[t] = Bar(t, o, h, l, c, v)
    return result

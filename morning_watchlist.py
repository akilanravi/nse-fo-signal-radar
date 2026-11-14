"""Standalone pre-market report. Reuses scanner readers without changing them."""
import asyncio
from collections import Counter, defaultdict
from datetime import time as wall_time, timedelta, datetime
import logging
import math
import os
import statistics
import time
from urllib.parse import urlencode, urlsplit
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import aiohttp

import config as cfg
from clock import at, now, trading_day
from data import APIError, CredentialsError, Dhan, Slots
from instruments import Bar, MASTER, discover
from strategy import ema, score
from telegram import Telegram, batches

LOG = logging.getLogger("morning")
START, CUTOFF = wall_time(8), wall_time(9)
FINAL_BAR = wall_time(15, 25)
HISTORY_DAYS = 12
TOP = 10
LIMIT = 3600
NSE = "https://www.nseindia.com"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"}


def session_dates(t):
    target = t.date()
    if t.time() >= cfg.SESSION_CLOSE:
        target += timedelta(days=1)
    while not trading_day(target):
        target += timedelta(days=1)
    base = target - timedelta(days=1)
    while not trading_day(base):
        base -= timedelta(days=1)
    return target, base


def rank_symbol(symbol, candles, session_day):
    """Original Next-Day Desk technical ranking, separate from live 7/9 signals."""
    rows = [candles[t] for t in sorted(candles) if t.date() <= session_day]
    if len(rows) < 70 or rows[-1].start.date() != session_day or rows[-1].start.time() != FINAL_BAR:
        raise ValueError("missing prior session close or warm-up")
    sessions = defaultdict(list)
    for b in rows:
        sessions[b.start.date()].append(b)
    if len(sessions) < 2:
        raise ValueError("missing CPR sessions")
    keys = sorted(sessions)
    periods = {}
    for day in keys[-2:]:
        s = sessions[day]
        if len(s) != 75:
            raise ValueError("incomplete CPR session")
        periods[day] = Bar(s[0].start, s[0].o, max(b.h for b in s), min(b.l for b in s), s[-1].c,
                           sum(b.v for b in s))
    # The original next-day engine uses the latest completed session and the
    # session before it to score against the upcoming session's CPR.
    # Keep original intraday dates for VWAP. Pass a synthetic period key just
    # before the last date so score() includes the latest completed day.
    periods = {day - timedelta(days=1): bar for day, bar in periods.items()}
    result = score(rows, periods, 0)
    if result is None or result.weighted_max != 10:
        raise ValueError("technical points unavailable")
    p = rows[-1].c
    mean_v = statistics.mean(b.v for b in rows[-cfg.VOL_LEN:])
    if mean_v <= 0:
        raise ValueError("volume baseline unavailable")
    vr = rows[-1].v / mean_v
    atr = rows[0].h - rows[0].l
    prev = rows[0].c
    for b in rows[1:]:
        tr = max(b.h-b.l, abs(b.h-prev), abs(b.l-prev))
        atr += (tr-atr)/14
        prev = b.c
    if atr <= 0:
        raise ValueError("ATR unavailable")
    stretch = abs(p - ema([b.c for b in rows], cfg.EMA_SLOW)) / (1.5 * atr)
    up, down = result.weighted
    if stretch > 1 or vr < .8 or max(up,down) < 5 or up == down:
        return None
    side = "bull" if up > down else "bear"
    points, opposite = max(up,down), min(up,down)
    r = round(70 * points/10 + 15 * min(vr/1.5, 1) + 15 * max(0, 1-stretch), 1)
    session = sessions[session_day]
    sign = 1 if side == "bull" else -1
    trigger = (max(b.h for b in session) if sign == 1 else min(b.l for b in session)) + sign*.1*atr
    stop, t1, t2 = trigger-sign*1.5*atr, trigger+sign*1.5*atr, trigger+sign*3*atr
    if min(trigger,stop,t1,t2) <= 0:
        return None
    return dict(symbol=symbol, side=side, rank=r, points=points, opposite=opposite,
                close=p, vol=vr, trigger=trigger, stop=stop, t1=t1, t2=t2)


def parse_flows(rows, expected):
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category", "")).upper().strip()
        side = "FII" if category.startswith(("FII","FPI")) else "DII" if category == "DII" else ""
        try:
            stamp = datetime.strptime(str(row["date"]).strip(), "%d-%b-%Y").date()
            net = float(str(row["netValue"]).replace(",", ""))
        except (ValueError, KeyError, TypeError):
            continue
        if side and stamp == expected and math.isfinite(net):
            if side in found and found[side] != net:
                return None  # Ambiguous scope; do not mix two series.
            found[side] = net
    return found if set(found) == {"FII","DII"} else None


async def cash_flows(session, day):
    for origin in (NSE, "https://www.nseindia.in"):
        try:
            async with session.get(origin + "/reports/fii-dii", headers=UA):
                pass
            async with session.get(origin + "/api/fiidiiTradeReact",
                                   headers={**UA, "Referer": origin + "/reports/fii-dii"}) as r:
                if r.status != 200:
                    continue
                items = await r.json(content_type=None)
            if isinstance(items, list) and (found := parse_flows(items, day)):
                return found
        except (aiohttp.ClientError, TimeoutError, ValueError):
            continue
    return None


async def news(session, query, cap):
    try:
        url = "https://news.google.com/rss/search?" + urlencode(
            dict(q=query, hl="en-IN", gl="IN", ceid="IN:en"))
        async with session.get(url) as r:
            if r.status != 200:
                return []
            root = ET.fromstring(await r.read())
        out = []
        for item in root.findall("./channel/item"):
            title = " ".join(item.findtext("title", "").split())[:125]
            link = item.findtext("link", "").strip()
            try:
                published = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(cfg.IST)
                stamp = published.strftime("%d %b")
            except (ValueError, TypeError):
                stamp = ""
            if title and len(link) <= 900 and urlsplit(link).scheme == "https":
                out.append((f"{title} ({stamp})" if stamp else title, link))
            if len(out) == cap:
                break
        return out
    except (aiohttp.ClientError, TimeoutError, ET.ParseError, ValueError):
        return []


def sections(top, target, source, checked, total, missing, flow, headlines):
    header = [
        f"NSE F&O Morning Watchlist | {target:%d %b %Y}",
        f"Previous session {source:%d %b %Y} | Validated {checked}/{total} stocks | Excluded {missing}",
        "Prior-session research levels. Not live CE/PE signals. No orders.",
    ]
    if checked < total:
        header.append("Partial coverage: rankings are from validated stocks only.")
    if flow:
        header.append(f"NSE provisional cash flow ({source:%d %b}): FII/FPI {flow['FII']:+,.2f} cr | "
                      f"DII {flow['DII']:+,.2f} cr | Combined {sum(flow.values()):+,.2f} cr")
    else:
        header.append("NSE FII/DII cash flow unavailable for the previous session. No figures substituted.")
    header.append("FII/DII and headlines are market context, not stock-level flows or ranking inputs.")
    if headlines:
        header.append("Market news: " + headlines[0][0] + "\n" + headlines[0][1])
    else:
        header.append("Market headlines unavailable.")
    yield "\n".join(header)
    for side, title in (("bull","Bullish CE watchlist"),("bear","Bearish PE watchlist")):
        items = top[side]
        if not items:
            yield title + "\nNo stocks qualified under the original filters."
            continue
        yield title + f" | {len(items)}/{TOP} shown\nConfirm a live 5m close beyond the trigger with volume before acting."
        for idx, r in enumerate(items, 1):
            text = (f"{idx}. {r['symbol']} | Rank {r['rank']:.1f}/100 | Tech {r['points']}/10 "
                    f"vs {r['opposite']}/10 | Volume {r['vol']:.2f}x\n"
                    f"Close ₹{r['close']:,.2f} | Trigger {r['trigger']:,.2f} | "
                    f"Stop {r['stop']:,.2f} | T1 {r['t1']:,.2f} | T2 {r['t2']:,.2f}")
            for label, field in (("Google News","news"),("Moneycontrol","moneycontrol")):
                entries = r.get(field, [])
                if entries:
                    text += f"\n{label}: {entries[0][0]}\n{entries[0][1]}"
            if not r.get("news") and not r.get("moneycontrol"):
                text += "\nNo matching headlines retrieved."
            yield text


async def master(session):
    for attempt in range(3):
        try:
            async with session.get(MASTER, timeout=aiohttp.ClientTimeout(total=35)) as r:
                if r.status == 200:
                    return await r.text(encoding="utf-8-sig")
        except (aiohttp.ClientError, TimeoutError):
            pass
        if attempt < 2:
            await asyncio.sleep(2**attempt)
    raise APIError("Dhan public instrument master unavailable")


async def build(session, target, source, deadline):
    api = Dhan(session, os.getenv("DHAN_CLIENT_ID","").strip(),
               os.getenv("DHAN_ACCESS_TOKEN","").strip(), lane=0)
    # One runner, not eleven. Leave headroom under Dhan's 5 data requests/s.
    api.data = Slots(0, .34, lanes=1)
    instruments, _, _, unmapped = await asyncio.to_thread(discover, await master(session), target)
    stocks = sorted((i for i in instruments.values() if i.kind == "EQUITY"), key=lambda i:i.name)
    if len(stocks) < 50:
        raise APIError("F&O stock universe unexpectedly small")
    if unmapped:
        LOG.warning("unmapped_master_entries=%s", ",".join(unmapped))
    limit = asyncio.Semaphore(9)
    problems = Counter()
    begin = at(source - timedelta(days=HISTORY_DAYS), cfg.OPEN)
    end = at(source, cfg.SESSION_CLOSE)

    async def one(inst):
        async with limit:
            try:
                frame = await api.candles(inst, begin, end, 5, deadline)
                return True, rank_symbol(inst.name, frame, source)
            except CredentialsError:
                raise
            except (APIError, ValueError) as e:
                problems[str(e)] += 1
                return False, None

    async with asyncio.TaskGroup() as tg:
        market = tg.create_task(news(session, "NSE India stock market FII DII outlook when:2d", 1))
        flows = tg.create_task(cash_flows(session, source))
        jobs = [tg.create_task(one(i)) for i in stocks]
    done = [j.result() for j in jobs]
    checked = sum(ok for ok, _ in done)
    LOG.info("target=%s source=%s validated=%d total=%d rejected=%s", target,source,checked,len(stocks),dict(problems))
    if not checked:
        raise APIError("No validated prior-session stock candles; no watchlist sent")
    top = {side: sorted((row for _,row in done if row and row["side"] == side),
                        key=lambda row:(-row["rank"],row["symbol"]))[:TOP] for side in ("bull","bear")}
    async def enrich(r):
        async with limit:
            r["news"], r["moneycontrol"] = await asyncio.gather(
                news(session, f'"{r["symbol"]}" NSE stock India when:2d', 1),
                news(session, f'"{r["symbol"]}" site:moneycontrol.com when:3d', 1))
    await asyncio.gather(*(enrich(r) for items in top.values() for r in items))
    report = list(sections(top,target,source,checked,len(stocks),len(stocks)-checked,flows.result(),market.result()))
    return list(batches(report, limit=LIMIT))


async def main():
    t = now()
    mode = os.getenv("WATCHLIST_MODE", "preview")
    if mode not in {"live","preview"}:
        raise ValueError("Unknown watchlist mode")
    preview = mode == "preview"
    if not preview and (not trading_day(t.date()) or not START <= t.time() < CUTOFF):
        LOG.info("Skipped: not an NSE trading day or outside 08:00–09:00 IST")
        return
    target, source = session_dates(t)
    cutoff = time.time() + 550 if preview else at(t.date(), CUTOFF).timestamp()
    deadline = min(cutoff-70, time.time()+365)
    if deadline <= time.time():
        LOG.info("Skipped: not enough time remains before morning cutoff")
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=True) as session:
        sender = Telegram(session, lane=0, dry=preview)
        sender.slots = Slots(0, 1.3, lanes=1)
        async with asyncio.timeout(max(1, min(580, cutoff-time.time()))):
            messages = await build(session,target,source,deadline)
            if not preview and (target != now().date() or now().time() >= CUTOFF):
                LOG.warning("Watchlist not sent after cutoff")
                return
            failed = await sender.send(messages, deadline=cutoff)
            if failed:
                raise APIError(f"Telegram rejected or timed out for {failed} watchlist batch(es)")
            LOG.info("%s batches=%d", "preview_complete" if preview else "watchlist_sent", len(messages))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except ExceptionGroup as e:
        for sub in e.exceptions:
            LOG.error("%s", str(sub) if isinstance(sub, APIError) else type(sub).__name__)
        raise SystemExit(1) from None
    except (APIError, ValueError, TimeoutError, aiohttp.ClientError) as e:
        LOG.error("%s", str(e) if isinstance(e, (APIError,ValueError)) else type(e).__name__)
        raise SystemExit(1) from None

"""NSE alert-only scanner. Run with prepare, scan, or report."""
import argparse
import asyncio
import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path

import aiohttp

import config as cfg
from clock import at, floor, fresh, latest, now, trading_day
from data import APIError, CredentialsError, Dhan
from instruments import MASTER, discover, groups, option_levels
from state import State, transition
from strategy import NAMES, score, tide
from telegram import Telegram

log = logging.getLogger("scanner")
MASTER_PATH = Path(".run/instruments.csv")
CACHE_LOOKBACK = timedelta(minutes=15)


def fmt(inst, candle, quote, result, side, boundary, chain):
    checks = result.ce if side == "CE" else result.pe
    ltp = f"{quote:,.2f}" if quote is not None else "unavailable"
    first = [f"BUY {side} | {inst.name} | {sum(x is True for x in checks)}/9",
             f"{boundary:%d %b %H:%M} IST | 5m close {candle.c:,.2f} | Quote LTP {ltp}",
             f"Weighted {result.weighted[0 if side == 'CE' else 1]}/10"
             + (f" ({result.weighted_max}/10 points available)" if result.weighted_max != 10 else "")]
    for value, label in ((True, "PASS"), (False, "FAIL"), (None, "N/A")):
        first.append(label + ": " + (", ".join(f"C{i} {n}" for i,(n,x) in enumerate(zip(NAMES,checks),1)
                                              if x is value) or "none"))
    opt = option_levels(chain, boundary.date(), side, candle.c)
    if opt:
        expiry, atm, itm, alternate = opt
        distinct = alternate if itm == atm else itm
        itm_label = f"{distinct:g} {side}" if distinct is not None else "unavailable"
        first.append(f"{expiry:%d %b %Y} | ATM {atm:g} {side} | "
                     f"{'Deeper ITM' if itm == atm else 'Nearest ITM'} {itm_label}")
        if itm == atm:
            first.append("The ATM reference is already slightly ITM.")
    else:
        first.append("No listed strike verified for this side.")
    first.append("Strikes use the candle close, not live premium. Alert only, no order.")
    return "\n".join(first)


def complete_session(bars, opening, end):
    return all(opening + i * cfg.FIVE in bars for i in range((end-opening)//cfg.FIVE))


class Runner:
    def __init__(self, api, telegram, state, group, day, raw):
        self.api, self.telegram, self.state, self.group, self.day = api, telegram, state, group, day
        subjects, self.proxies, self.chains, _ = discover(raw, day)
        self.nifty = next(i for i in subjects.values() if i.name == "NIFTY")
        self.members = groups(subjects)[group]
        self.opening = at(day, cfg.OPEN)
        self.daily = {}
        self.history_floor = self.opening - timedelta(days=cfg.HISTORY_DAYS)
        self.hist = {i: {} for i in self.members}
        self.benchmark = {}
        self.bad_streak = 0
        self.quote_warned = False

    async def guarded(self, inst, start, stop, interval, deadline):
        try:
            return await self.api.candles(inst, start, stop, interval, deadline)
        except CredentialsError:
            raise
        except APIError as e:
            log.warning("candle_error group=%s symbol=%s interval=%s reason=%s",
                        self.group, inst.name, interval, e)
            return {}

    async def warm(self, deadline):
        start = self.history_floor
        async def warm_daily(inst):
            self.daily[inst] = await self.guarded(inst, start, self.opening, None, deadline)
        async def warm_5m(inst):
            # Warm only through the prior day; fetch the current session at evaluation.
            self.hist[inst].update(await self.guarded(inst, start, self.opening, 5, deadline))
        await warm_daily(self.nifty)
        self.benchmark.update(await self.guarded(self.nifty, start, self.opening, cfg.TIDE_MINUTES, deadline))
        tasks = [warm_daily(i) for i in self.members if i != self.nifty]
        tasks += [warm_5m(i) for i in self.members]
        await asyncio.gather(*tasks)
        log.info("warmup group=%s daily_ready=%s intraday_ready=%s size=%s",
                 self.group, sum(len(v)>=2 for v in self.daily.values()),
                 sum(bool(v) for v in self.hist.values()), len(self.members))

    async def bias(self, boundary, deadline):
        t = floor(boundary, cfg.TIDE_MINUTES)
        target = t - timedelta(minutes=cfg.TIDE_MINUTES)
        if len(self.daily.get(self.nifty, {})) < 2:
            self.daily[self.nifty] = await self.guarded(self.nifty, self.history_floor, self.opening, None, deadline)
        if target not in self.benchmark or len(self.benchmark) < cfg.TIDE_MIN_BARS:
            start = self.opening if len(self.benchmark) >= cfg.TIDE_MIN_BARS else self.history_floor
            self.benchmark.update(await self.guarded(self.nifty, start, t, cfg.TIDE_MINUTES, deadline))
        if target not in self.benchmark:
            return None
        return tide([self.benchmark[k] for k in sorted(self.benchmark) if k <= target],
                    self.daily.get(self.nifty, {}), self.day)

    async def candles_for(self, inst, boundary, deadline):
        cache = self.hist[inst]
        start = self.opening
        if complete_session(cache, self.opening, max(self.opening, boundary - cfg.FIVE)):
            start = max(self.opening, boundary - CACHE_LOOKBACK)
        if not any(t < self.opening for t in cache):
            start = self.history_floor
        if len(self.daily.get(inst, {})) < 2 and inst != self.nifty:
            self.daily[inst] = await self.guarded(inst, self.history_floor, self.opening, None, deadline)
        cache.update(await self.guarded(inst, start, boundary, 5, deadline))
        return cache

    async def notices(self, key, message):
        if key in self.state.doc["notices"]:
            return
        self.state.doc["notices"][key] = now().isoformat()
        await self.state.save()
        await self.telegram.send([message])

    async def scan(self, boundary):
        deadline = (boundary + timedelta(seconds=cfg.MAX_STALENESS)).timestamp()
        if time.time() >= deadline or self.state.doc.get("last_scan", "") >= boundary.isoformat():
            return
        bias_task = asyncio.create_task(self.bias(boundary, deadline))
        # Each group uses one batched LTP request, on its own shared quote-rate slot.
        ltp_task = asyncio.create_task(self.api.ltp(self.members, deadline))
        candle_tasks = {i: asyncio.create_task(self.candles_for(i, boundary, deadline)) for i in self.members}
        try:
            bias = await bias_task
            data = dict(zip(candle_tasks, await asyncio.gather(*candle_tasks.values())))
            try:
                prices = await ltp_task
            except CredentialsError:
                raise
            except APIError as e:
                prices = {}
                log.warning("quote_unavailable group=%s reason=%s", self.group, e)
            target = boundary - cfg.FIVE
            late = [i for i,b in data.items() if target not in b]
            if late and time.time() + len(late) * len(cfg.GROUPS) * cfg.DATA_SLOT_SECONDS + 5 < deadline:
                fills = await asyncio.gather(*(self.guarded(i, target, boundary, 5, deadline) for i in late))
                for i, bars in zip(late, fills):
                    data[i].update(bars)
        finally:
            pending = [bias_task, ltp_task, *candle_tasks.values()]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        stale, messages = [], []
        if not fresh(now(), boundary):
            log.warning("scan_expired group=%s boundary=%s", self.group, boundary.isoformat())
            self.state.doc["scans"] += 1
            self.state.doc["last_scan"] = boundary.isoformat()
            self.state.doc["missing"] += len(self.members)
            self.bad_streak += 1
            if self.bad_streak == cfg.STALE_GROUP_AFTER:
                await self.notices("stale:" + boundary.isoformat(), f"DATA STALE | {self.group} | scan deadline elapsed")
            else:
                await self.state.save()
            return
        for inst, frame in data.items():
            daily = self.daily.get(inst, {})
            if bias is None or not complete_session(frame, self.opening, boundary):
                stale.append(inst.name)
                continue
            ordered = [frame[k] for k in sorted(frame) if k <= target]
            result = score(ordered, daily, bias, is_index=inst.kind == "INDEX")
            if result is None:
                stale.append(inst.name)
                continue
            key = f"{inst.segment}:{inst.sid}"
            updated, events = transition(self.state.doc["symbols"].get(key), result, boundary)
            self.state.doc["symbols"][key] = updated
            for event in events:
                action, side = event.split()
                self.state.doc["counts"][side if action == "BUY" else "EXIT"] += 1
                if action == "BUY":
                    messages.append(fmt(inst, frame[target], prices.get((inst.segment,inst.sid)),
                                        result, side, boundary, self.chains[inst.key]))
                else:
                    messages.append(f"{event} | {inst.name} | {boundary:%H:%M} IST | Supertrend reversal"
                                    f" | Close {frame[target].c:,.2f} | No order")
        self.state.doc["scans"] += 1
        self.state.doc["last_scan"] = boundary.isoformat()
        self.state.doc["missing"] += len(stale)
        if stale or (not prices and self.members):
            self.bad_streak += 1
            if self.bad_streak == cfg.STALE_GROUP_AFTER:
                messages.append(f"DATA STALE / INCOMPLETE | {self.group} | {boundary:%H:%M} IST"
                                f" | {len(stale)}/{len(self.members)} symbols suppressed"
                                + (" | No quote snapshots" if not prices and self.members else "")
                                + (" | NIFTY Tide unavailable" if bias is None else "")
                                + (" | " + ", ".join(stale[:10]) if stale else ""))
        else:
            self.bad_streak = 0
        # Checkpoint first: a runner crash can suppress an unsent alert, but cannot
        # blindly repeat already-sent signals. This is not exactly-once delivery.
        await self.state.save()
        unsent = await self.telegram.send(messages, deadline)
        if unsent:
            self.state.doc["delivery_failures"] = self.state.doc.get("delivery_failures", 0) + unsent
            await self.state.save()
        log.info("scan group=%s bar=%s evaluated=%s/%s signals=%s missing=%s failed_deliveries=%s",
                 self.group, boundary.isoformat(), len(self.members)-len(stale), len(self.members),
                 len(messages), len(stale), unsent)

    async def run(self, once):
        current = now()
        end = at(self.day, cfg.EXIT_BY)
        if current >= end:
            log.info("after cutoff; no stale catch-up")
            return
        if once:
            warm_deadline = min(end.timestamp(), time.time() + 180)
        else:
            warm_deadline = min(end.timestamp(), time.time() + 230)
        warm_task = asyncio.create_task(self.warm(warm_deadline))
        start_task = None
        async def started():
            marker = at(self.day, cfg.STATUS_AT)
            await asyncio.sleep(max(0, (marker-now()).total_seconds()))
            if now() < end:
                await self.notices("started", f"SCANNER STARTED | {self.day} | NSE F&O | 7/9"
                                   f" | indices job online at {now():%H:%M:%S} IST"
                                   f" | 10 stock groups scheduled | No orders")
        # No writes on this State until warm-up completes. If late, label actual time.
        try:
            if self.group == "indices" and not once:
                start_task = asyncio.create_task(started())
            await warm_task
            if start_task:
                await start_task
            if once:
                selected = latest(now())
                if fresh(now(), selected):
                    await self.scan(selected)
                else:
                    log.info("manual once: connectivity warm-up done; no fresh completed candle")
                return
            last = None
            while now() < end:
                n = now()
                t = latest(n)
                if t != last and fresh(n, t):
                    await self.scan(t)
                    last = t
                if t.time() == cfg.LAST:
                    break
                next_close = max(at(self.day, cfg.FIRST), t + cfg.FIVE)
                await asyncio.sleep(max(0, (next_close + timedelta(seconds=cfg.FINALIZE_SECONDS)-now()).total_seconds()))
        finally:
            if start_task and not start_task.done():
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
        log.info("group_complete group=%s", self.group)


async def download(session):
    for attempt in range(4):
        try:
            async with session.get(MASTER, timeout=aiohttp.ClientTimeout(total=45)) as r:
                if r.status != 200:
                    raise APIError(f"Master HTTP {r.status}")
                return await r.text(encoding="utf-8-sig")
        except (APIError, aiohttp.ClientError, TimeoutError):
            if attempt == 3:
                raise
            await asyncio.sleep(2**attempt)


def state_for(session, day, group, dry):
    return State(session, os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", ""), day, group, dry)


async def main(args):
    day = now().date()
    dry = args.dry_run
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=True) as session:
        if args.command == "prepare":
            eligible = trading_day(day)
            if os.getenv("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                    print(f"eligible={str(eligible).lower()}\nday={day}", file=out)
            if not eligible:
                log.info("closed day=%s", day)
                return
            raw = await download(session)
            subjects, _, _, missing = discover(raw, day)
            split = groups(subjects)
            if not any(i.name == "NIFTY" for i in split["indices"]) or any(not g for g in split.values()):
                raise APIError("Dhan instrument master incomplete")
            if missing:
                log.warning("unmapped=%s", ",".join(missing))
            log.info("universe day=%s groups=%s", day, json.dumps({k:len(v) for k,v in split.items()}))
            MASTER_PATH.parent.mkdir(exist_ok=True)
            MASTER_PATH.write_text(raw)
            if not dry:
                await state_for(session, day, "prepare", dry).branch(os.environ["GITHUB_SHA"])
            return
        if args.day:
            from datetime import date
            day = date.fromisoformat(args.day)
        if args.command == "report":
            group_status = args.status
            sender = Telegram(session, 10, dry)
            if dry:
                await sender.send([f"WORKFLOW STATUS | {day} | matrix {group_status}"])
                return
            if "prepare=failure" in group_status:
                await sender.send([f"SCANNER WORKFLOW ERROR | {day} | prepare failed; inspect Actions logs"])
                return
            reporter = state_for(session, day, "report", dry)
            await reporter.load()
            key = "summary" if now().time() >= cfg.LAST else f"early-{os.getenv('GITHUB_RUN_ID','local')}"
            if key in reporter.doc["notices"]:
                return
            totals = dict(CE=0, PE=0, EXIT=0)
            incomplete = []
            missing, failed_deliveries = 0, 0
            for g in cfg.GROUPS:
                group = state_for(session, day, g, dry)
                await group.load()
                if not group.doc["scans"]:
                    incomplete.append(g)
                missing += group.doc.get("missing", 0)
                failed_deliveries += group.doc.get("delivery_failures", 0)
                for side in totals:
                    totals[side] += group.doc["counts"][side]
            reporter.doc["notices"][key] = now().isoformat()
            await reporter.save()
            await sender.send([f"{'DAILY SUMMARY' if key == 'summary' else 'SCANNER WORKFLOW ERROR'} | {day} | {now():%H:%M} IST"
                               f"\nMatrix {group_status} | CE {totals['CE']} | PE {totals['PE']} | EXIT {totals['EXIT']}"
                               + ("\nNo completed scans: " + ", ".join(incomplete) if incomplete else "")
                               + f"\nSuppressed symbol-candles: {missing} | Failed Telegram batches: {failed_deliveries}"
                               + "\nCounts reflect generated signals. Check Actions logs for failures."])
            return
        if not trading_day(day) or day != now().date():
            log.info("no current trading session")
            return
        lane = cfg.GROUPS.index(args.group)
        sender = Telegram(session, lane, dry)
        state = state_for(session, day, args.group, dry)
        try:
            await state.load()
            api = Dhan(session, os.getenv("DHAN_CLIENT_ID", "").strip(),
                       os.getenv("DHAN_ACCESS_TOKEN", "").strip(), lane)
            runner = Runner(api, sender, state, args.group, day, MASTER_PATH.read_text())
            await runner.run(args.once)
        except Exception as exc:
            safe = str(exc) if isinstance(exc, (APIError, ValueError)) else type(exc).__name__
            log.error("group_failed group=%s reason=%s", args.group, safe)
            await sender.send([f"SCANNER ERROR | {args.group} | {day} | {safe}"])
            raise SystemExit(1) from None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(name)s %(message)s")
    logging.Formatter.converter = time.gmtime
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["prepare", "scan", "report"])
    p.add_argument("--group", choices=cfg.GROUPS, default="indices")
    p.add_argument("--day")
    p.add_argument("--status", default="unknown")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except (RuntimeError, ValueError, OSError, aiohttp.ClientError, TimeoutError) as e:
        detail = str(e) if isinstance(e, (APIError, ValueError)) else type(e).__name__
        log.error("scanner_failed type=%s reason=%s", type(e).__name__, detail)
        raise SystemExit(1) from None

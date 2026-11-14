"""After-close research replay only. No order endpoints or broker fills."""
import asyncio
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime, time as wall, timedelta
import io
import logging
import math
import os
from pathlib import Path
import time

import aiohttp
import config as cfg
from clock import at, floor, now, trading_day
from data import APIError, CredentialsError, Dhan, Slots
from instruments import Instrument, MASTER, discover
from strategy import NAMES, score, tide

LOG = logging.getLogger("paper")
MINUTE = timedelta(minutes=1)
ENTRY_LAST = wall(15, 10)
SQUARE_OFF = wall(15, 15)
TARGET_PCT, STOP_PCT = .20, .10
OUTPUT = Path(".paper-output")
GROUPS = [f"C{i}" for i in range(1, 11)]
FIELDS = ("session", "strategy", "underlying", "side", "status", "reason", "signal_ist",
          "underlying_close_at_signal", "contract", "security_id", "expiry", "strike",
          "lot_size", "entry_ist", "entry_premium", "target_trigger", "stop_trigger",
          "exit_signal_ist", "trigger_candle_ist", "exit_ist", "exit_premium", "return_pct_gross",
          "gross_pnl_inr_one_lot", "charges_inr", "net_pnl_inr", "price_source")


@dataclass(frozen=True)
class Contract:
    instrument: Instrument
    expiry: date
    strike: float
    lot: int


@dataclass(frozen=True)
class TradePlan:
    strategy: str
    side: str
    start: datetime
    spot: float
    exit_at: datetime


def session_day(t, scheduled):
    if scheduled:
        return t.date() if trading_day(t.date()) and t.time() >= cfg.SESSION_CLOSE else None
    day = t.date() if t.time() >= cfg.SESSION_CLOSE else t.date()-timedelta(days=1)
    while not trading_day(day):
        day -= timedelta(days=1)
    return day


def contract_book(raw, subjects, day):
    names = {"".join(c for c in x.name.upper() if c.isalnum()):x for x in subjects.values()}
    names["NIFTY50"] = names.get("NIFTY")
    stocks = {x.sid:x for x in subjects.values() if x.kind == "EQUITY"}
    book = defaultdict(list)
    for row in csv.DictReader(io.StringIO(raw.lstrip("\ufeff"))):
        kind, side = row.get("INSTRUMENT"), row.get("OPTION_TYPE")
        if row.get("EXCH_ID") != "NSE" or kind not in {"OPTSTK","OPTIDX"} or side not in {"CE","PE"}:
            continue
        try:
            expiry = date.fromisoformat(row["SM_EXPIRY_DATE"][:10])
            # At night Dhan's active-contract endpoint may no longer serve same-day expiries.
            if expiry <= day:
                continue
            if kind == "OPTSTK":
                subject = stocks.get(int(row["UNDERLYING_SECURITY_ID"]))
            else:
                subject = names.get("".join(c for c in row["UNDERLYING_SYMBOL"].upper() if c.isalnum()))
            strike = float(row["STRIKE_PRICE"])
            lot_raw = float(row["LOT_SIZE"])
            sid = int(row["SECURITY_ID"])
            if not subject or not math.isfinite(strike) or strike <= 0 or not lot_raw.is_integer() or lot_raw <= 0 or sid <= 0:
                continue
            contract = Contract(Instrument(row["SYMBOL_NAME"],"NSE_FNO",sid,kind),
                                expiry,strike,int(lot_raw))
            book[subject.key,side].append(contract)
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
    return book


def choose(contracts, spot):
    return min(contracts,key=lambda x:(x.expiry,abs(x.strike-spot),x.strike,x.instrument.sid),default=None)


def directions(result, bias):
    out = {}
    for i, (up,dn) in enumerate(zip(result.ce,result.pe),1):
        out[f"C{i}"] = "CE" if up is True else "PE" if dn is True else "" if up is False and dn is False else None
    # Neutral, confirmed NIFTY Tide is not missing.
    if bias == 0:
        out["C9"] = ""
    if bias is None:
        out["C9"] = None
    out["C10"] = result.side() if bias is not None else None
    return out


def plans(points, day):
    """An initial qualifying state counts as an entry at the first eligible close."""
    final = at(day,SQUARE_OFF)
    opened, combined = {}, []
    previous = {}
    for boundary, spot, mapping in points:
        for strategy in GROUPS:
            direction = mapping[strategy]
            if direction is None:
                continue
            if strategy == "C10":
                if (direction and direction != previous.get(strategy) and boundary.time() <= ENTRY_LAST):
                    combined.append(TradePlan(strategy,direction,boundary,spot,final))
            else:
                old = opened.get(strategy)
                if old and direction and direction != old.side:
                    yield TradePlan(old.strategy,old.side,old.start,old.spot,boundary)
                    opened.pop(strategy)
                if direction and strategy not in opened and boundary.time() <= ENTRY_LAST:
                    opened[strategy] = TradePlan(strategy,direction,boundary,spot,final)
            previous[strategy] = direction
    yield from opened.values()
    yield from combined


def simulate(plan, contract, frame, day):
    base = dict(session=day.isoformat(),strategy=plan.strategy,side=plan.side,
                signal_ist=plan.start.isoformat(),underlying_close_at_signal=plan.spot,
                status="SKIPPED",price_source="Dhan contract 1-minute OHLCV; simulated fills")
    if not contract:
        return {**base,"reason":"NO_VERIFIED_LISTED_CONTRACT"}
    base.update(contract=contract.instrument.name,security_id=contract.instrument.sid,
                expiry=contract.expiry.isoformat(),strike=contract.strike,lot_size=contract.lot)
    entry = frame.get(plan.start)
    if not entry or entry.v <= 0:
        return {**base,"reason":"NO_TRADED_ENTRY_MINUTE"}
    base.update(entry_ist=entry.start.isoformat(),entry_premium=entry.o)
    final = at(day,SQUARE_OFF)
    if plan.strategy == "C10":
        upper, lower = round(entry.o*(1+TARGET_PCT),8), round(entry.o*(1-STOP_PCT),8)
        base.update(target_trigger=upper,stop_trigger=lower)
        t = plan.start
        while t < final:
            b = frame.get(t)
            if b is None or b.v <= 0:
                # No quote or tick reconstruction: the trigger path is unobservable.
                return {**base,"status":"UNRESOLVED","reason":"MISSING_TRADED_PREMIUM_MINUTE"}
            hit_target, hit_stop = b.h >= upper, b.l <= lower
            if hit_target or hit_stop:
                base.update(trigger_candle_ist=t.isoformat(),exit_signal_ist=(t+MINUTE).isoformat())
                if hit_target and hit_stop:
                    return {**base,"status":"UNRESOLVED","reason":"BOTH_TOUCHED_ORDER_UNKNOWN"}
                following = frame.get(t+MINUTE)
                if not following or following.v <= 0:
                    return {**base,"status":"UNRESOLVED","reason":"NO_TRADED_MINUTE_AFTER_TRIGGER"}
                return finish(base,following.o,following.start,t+MINUTE,entry.o,contract.lot,
                              "TARGET_20_TOUCHED" if hit_target else "STOP_10_TOUCHED")
            t += MINUTE
    exitbar = frame.get(plan.exit_at)
    if not exitbar or exitbar.v <= 0:
        return {**base,"status":"UNRESOLVED","reason":"NO_TRADED_EXIT_MINUTE",
                "exit_signal_ist":plan.exit_at.isoformat()}
    return finish(base,exitbar.o,exitbar.start,plan.exit_at,entry.o,contract.lot,
                  "SESSION_SQUARE_OFF" if plan.exit_at == final else "UNDERLYING_REVERSAL")


def finish(base,premium,when,signal,entry,lot,reason):
    return {**base,"status":"CLOSED","reason":reason,"exit_ist":when.isoformat(),
            "exit_signal_ist":signal.isoformat(),"exit_premium":premium,
            "return_pct_gross":round(100*(premium/entry-1),6),
            "gross_pnl_inr_one_lot":round((premium-entry)*lot,2)}


def chronological(frame, end):
    return [frame[t] for t in sorted(frame) if t < end]


def signal_points(frame,daily,nframe,ndaily,day,is_index=False):
    opening = at(day,cfg.OPEN)
    end = at(day,SQUARE_OFF)
    t = at(day,cfg.FIRST)
    while t <= end:
        if any(opening+i*cfg.FIVE not in frame for i in range((t-opening)//cfg.FIVE)):
            break
        tide_boundary = floor(t,cfg.TIDE_MINUTES)
        nbar = tide_boundary-timedelta(minutes=cfg.TIDE_MINUTES)
        bias = tide(chronological(nframe,tide_boundary),ndaily,day) if nbar in nframe else None
        bars = chronological(frame,t)
        result = score(bars,daily,0 if bias is None else bias,is_index=is_index)
        if result:
            yield t,bars[-1].c,directions(result,bias)
        t += cfg.FIVE


async def get_master(session):
    for attempt in range(3):
        try:
            async with session.get(MASTER,timeout=aiohttp.ClientTimeout(total=35)) as r:
                if r.status == 200:
                    return await r.text(encoding="utf-8-sig")
        except (aiohttp.ClientError,TimeoutError):
            pass
        if attempt < 2:
            await asyncio.sleep(2**attempt)
    raise APIError("Dhan instrument master unavailable")


async def replay(session,day,deadline):
    api = Dhan(session,os.getenv("DHAN_CLIENT_ID","").strip(),os.getenv("DHAN_ACCESS_TOKEN","").strip(),0)
    api.data = Slots(0,.34,lanes=1)
    raw = await get_master(session)
    subjects,_,_,unmapped = await asyncio.to_thread(discover,raw,day)
    if len(subjects) < 50:
        raise APIError("Unusually small Dhan F&O universe")
    contracts = await asyncio.to_thread(contract_book,raw,subjects,day)
    nifty = next(x for x in subjects.values() if x.name == "NIFTY")
    lo = at(day-timedelta(days=cfg.HISTORY_DAYS),cfg.OPEN)
    op = at(day,cfg.OPEN)
    end = at(day,cfg.SESSION_CLOSE)
    nframe,ndaily = await asyncio.gather(api.candles(nifty,lo,end,cfg.TIDE_MINUTES,deadline),
                                        api.candles(nifty,lo,op,None,deadline))
    if not nframe or len(ndaily)<2:
        raise APIError("Missing NIFTY Tide history")
    cap = asyncio.Semaphore(10)
    async def underlying(inst):
        async with cap:
            try:
                bars = await api.candles(inst,lo,end,5,deadline)
                daily = ndaily if inst == nifty else await api.candles(inst,lo,op,None,deadline)
                ready = list(signal_points(bars,daily,nframe,ndaily,day,inst.kind == "INDEX"))
                return inst,ready
            except CredentialsError:
                raise
            except APIError:
                return inst,[]
    insts = sorted(subjects.values(),key=lambda x:x.name)
    # Same equity-only symbol scope as stock ranking plus the scanner's NSE indices.
    async with asyncio.TaskGroup() as tg:
        jobs = [tg.create_task(underlying(i)) for i in insts]
    planned, excluded, partial, needs = [],[],[],{}
    for job in jobs:
        inst, points = job.result()
        if not points:
            excluded.append(inst.name)
            continue
        if points[-1][0] < at(day,SQUARE_OFF):
            # No fabricated reversal or re-entry after a missing underlying bar.
            partial.append(inst.name)
            continue
        for plan in plans(points,day):
            contract = choose(contracts[inst.key,plan.side],plan.spot)
            planned.append((inst,plan,contract))
            if contract:
                needs[contract.instrument.sid] = contract.instrument
    if len(excluded)+len(partial) == len(insts):
        raise APIError("No complete underlying sessions; no results reported")
    LOG.info("coverage total=%d excluded=%d partial=%d plans=%d contracts=%d",
             len(insts),len(excluded),len(partial),len(planned),len(needs))
    async def premiums(contract):
        async with cap:
            try:
                return contract.sid, await api.candles(contract,op,end,1,deadline)
            except CredentialsError:
                raise
            except APIError:
                return contract.sid,{}
    async with asyncio.TaskGroup() as tg:
        lookups = [tg.create_task(premiums(i)) for i in needs.values()]
    data = dict(j.result() for j in lookups)
    rows, active = [],{}
    for inst,plan,contract in sorted(planned,key=lambda x:(x[1].start,x[0].name,int(x[1].strategy[1:]))):
        key = (inst.key,plan.strategy)
        if plan.strategy == "C10" and active.get(key,op) > plan.start:
            continue
        r = simulate(plan,contract,data.get(contract.instrument.sid,{}) if contract else {},day)
        if plan.strategy == "C10" and "entry_ist" in r:
            active[key] = (datetime.fromisoformat(r["exit_ist"]) if r["status"] == "CLOSED" else at(day,SQUARE_OFF))
        rows.append({**r,"underlying":inst.name})
    return rows, (len(insts),excluded,partial,unmapped)


def render(rows,day,coverage):
    total,excluded,partial,unmapped = coverage
    header = [f"F&O Paper Replay | {day:%d %b %Y}",
              f"Full underlying sessions: {total-len(excluded)-len(partial)}/{total} | "
              f"Missing: {len(excluded)} | Partial: {len(partial)} | Master unmapped: {len(unmapped)}",
              "Dhan recorded premiums, simulated fills, one listed lot. Gross only, no orders."]
    for group in GROUPS:
        rr = [r for r in rows if r["strategy"]==group]
        closed = [r for r in rr if r["status"]=="CLOSED"]
        ce = [r for r in closed if r["side"]=="CE"]
        pe = [r for r in closed if r["side"]=="PE"]
        pn = lambda xs:sum(r["gross_pnl_inr_one_lot"] for r in xs)
        counts = Counter(r["status"] for r in rr)
        header.append(f"{group} {NAMES[int(group[1:])-1] if group!='C10' else 'Combined 7/9'} | "
                      f"CE {len(ce)} ₹{pn(ce):+,.2f} | PE {len(pe)} ₹{pn(pe):+,.2f} | "
                      f"Skipped {counts['SKIPPED']} | Unresolved {counts['UNRESOLVED']}")
    return "\n".join(header)


async def send_file(session,path,summary):
    token = os.getenv("TELEGRAM_BOT_TOKEN_1","").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID_1","").strip()
    if not token or not chat.lstrip("-").isdigit():
        raise APIError("Missing Telegram pair 1 credentials")
    # Avoid blind retries on uncertain delivery; the Actions artifact remains available.
    for attempt in range(3):
        form = aiohttp.FormData()
        form.add_field("chat_id",chat)
        form.add_field("caption","Nightly paper replay CSV. No orders.")
        form.add_field("document",path.read_bytes(),filename=path.name,content_type="text/csv")
        async with session.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                data=form,timeout=aiohttp.ClientTimeout(total=35)) as r:
            p = await r.json(content_type=None)
            if r.status == 200 and p.get("ok") is True:
                break
            if r.status != 429 or attempt == 2:
                raise APIError(f"Telegram CSV rejected (HTTP {r.status})")
            await asyncio.sleep(min(30,max(1,int(p.get("parameters",{}).get("retry_after",2)))))
    from telegram import Telegram
    sender = Telegram(session,lane=0,dry=False)
    sender.slots = Slots(0,1.3,lanes=1)
    if await sender.send([summary]):
        raise APIError("CSV delivered but Telegram summary failed")


async def main():
    mode = os.getenv("PAPER_MODE","preview")
    if mode not in {"preview","send"}:
        raise ValueError("Invalid paper mode")
    t = now()
    if wall(7,30) <= t.time() < cfg.SESSION_CLOSE and trading_day(t.date()):
        LOG.info("Skipped: keep Dhan rate limit available for morning and intraday jobs")
        return
    day = session_day(t,os.getenv("PAPER_EVENT")=="schedule")
    if day is None:
        LOG.info("Skipped: NSE closed")
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),trust_env=True) as s:
        rows,coverage = await replay(s,day,time.time()+1650)
        OUTPUT.mkdir(exist_ok=True)
        path = OUTPUT / f"paper-trades-{day}.csv"
        with path.open("w",encoding="utf-8-sig",newline="") as file:
            writer = csv.DictWriter(file,fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        summary = render(rows,day,coverage)
        (OUTPUT/"summary.txt").write_text(summary+"\n",encoding="utf-8")
        (OUTPUT/"coverage.txt").write_text("\n".join(f"{name}: {', '.join(values)}"
            for name,values in zip(("No data","Partial","Unmapped"),coverage[1:]))+"\n",encoding="utf-8")
        print(summary)
        if mode == "send":
            await send_file(s,path,summary)
            LOG.info("Telegram CSV and summary accepted")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except ExceptionGroup as e:
        for x in e.exceptions:
            LOG.error("%s",str(x) if isinstance(x,APIError) else type(x).__name__)
        raise SystemExit(1) from None
    except (APIError,ValueError,aiohttp.ClientError,TimeoutError) as e:
        LOG.error("%s",str(e) if isinstance(e,(APIError,ValueError)) else type(e).__name__)
        raise SystemExit(1) from None

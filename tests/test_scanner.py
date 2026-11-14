import asyncio
import csv
import io
import unittest
from datetime import date, time, timedelta
from unittest.mock import patch

import config as cfg
from clock import at, fresh, latest, trading_day
from data import APIError, Dhan, parse_bars
from instruments import Bar, discover, groups, option_levels
from scanner import Runner, complete_session
from state import transition
from strategy import Result, ema, ranges, rsi_st, score, tide
from telegram import batches

DAY = date(2026,11,16)
OPEN = at(DAY, cfg.OPEN)


def fake_master(n=23):
    raw = io.StringIO()
    w = csv.writer(raw)
    w.writerow(("EXCH_ID","SECURITY_ID","INSTRUMENT","UNDERLYING_SECURITY_ID",
                "UNDERLYING_SYMBOL","SM_EXPIRY_DATE","SYMBOL_NAME","OPTION_TYPE","STRIKE_PRICE"))
    w.writerow(("NSE",13,"INDEX","","","","NIFTY","",""))
    w.writerow(("NSE",250,"OPTIDX",26000,"NIFTY",DAY,"NIFTY-100-CE","CE",100))
    w.writerow(("NSE",251,"OPTIDX",26000,"NIFTY",DAY,"NIFTY-90-CE","CE",90))
    for i in reversed(range(n)):
        symbol = f"S{i:03}"
        w.writerow(("NSE",300+i,"EQUITY","","","",symbol,"",""))
        w.writerow(("NSE",1300+i,"OPTSTK",300+i,symbol,DAY,"OPT","PE",110))
    return raw.getvalue()


def bars():
    history, daily = [], {}
    for j in range(3):
        start = OPEN - timedelta(days=2-j)
        for i in range(75 if j < 2 else 4):
            p = 100 + j*10 + i/100
            history.append(Bar(start+i*cfg.FIVE,p-.1,p+.2,p-.2,p,1000))
        if j < 2:
            daily[start.date()] = Bar(start,100+j*10,102+j*10,99+j*10,101+j*10,0)
    return history, daily


class ScannerTests(unittest.TestCase):
    def test_timing_and_holidays(self):
        self.assertTrue(trading_day(DAY))
        self.assertFalse(trading_day(date(2026,11,24)))
        self.assertFalse(trading_day(date(2026,11,14)))
        with self.assertRaises(ValueError):
            trading_day(date(2099,1,2))
        t = at(DAY,time(9,35))
        self.assertFalse(fresh(t,t))
        self.assertEqual(latest(t + timedelta(seconds=7)),t)
        self.assertTrue(fresh(t + timedelta(seconds=7),t))
        self.assertFalse(fresh(t + timedelta(seconds=146),t))

    def test_universe_groups_and_listed_expiry(self):
        assets, _, chain, missing = discover(fake_master(), DAY)
        split = groups(assets)
        self.assertEqual(list(split),list(cfg.GROUPS))
        self.assertEqual([len(split[g]) for g in cfg.GROUPS[:-1]],[3,3,3,2,2,2,2,2,2,2])
        self.assertEqual([i.name for g in cfg.GROUPS[:-1] for i in split[g]], [f"S{i:03}" for i in range(23)])
        self.assertEqual(split["indices"][0].sid,13)
        self.assertEqual(missing,[])
        self.assertEqual(option_levels(chain[0,13],DAY,"CE",102),(DAY,100,100,90))
        self.assertIsNone(option_levels(chain[0,13],DAY+timedelta(days=1),"CE",102))
        self.assertEqual(option_levels({(DAY,"PE"):{90,100,110}},DAY,"PE",102),(DAY,100,110,110))

    def test_indicator_and_cpr(self):
        frame, daily = bars()
        self.assertEqual(ema([10,20],3),15)
        self.assertEqual(rsi_st(frame)[0],100)
        self.assertEqual(rsi_st(frame)[1],1)
        tc, bc, _, _, h3, h4, l3, l4 = ranges(daily,DAY)
        self.assertGreaterEqual(tc,bc)
        self.assertAlmostEqual(h3,111+3*1.1/4)
        self.assertAlmostEqual(l4,111-3*1.1/2)
        self.assertTrue(h4>h3>l3>l4)
        self.assertEqual(tide(frame,daily,DAY),1)
        self.assertIsNone(tide(frame,daily,DAY-timedelta(days=1)))

    def test_score_weight_and_unavailable(self):
        frame,daily = bars()
        result = score(frame,daily,1)
        self.assertEqual(result.ce,(True,True,True,True,True,False,True,False,True))
        self.assertEqual(result.weighted,(8,0))
        self.assertEqual(result.side(),"CE")
        self.assertIsNone(score(frame,daily,None))
        for b in frame:
            b.v = 0
        idx = score(frame,daily,1,is_index=True)
        self.assertIsNone(idx.ce[7])
        self.assertEqual(idx.weighted_max,9)

    def test_closed_only_and_no_missing_intraday(self):
        frame,_ = bars()
        b = frame[-1]
        sample = {"timestamp":[int(b.start.timestamp())],"open":[b.o],"high":[b.h],
                  "low":[b.l],"close":[b.c],"volume":[b.v]}
        self.assertEqual(parse_bars(sample,b.start,5),{})
        self.assertEqual(parse_bars(sample,b.end,5)[b.start],b)
        with self.assertRaises(APIError):
            parse_bars({**sample,"volume":[1,2]},b.end,5)
        data = {x.start:x for x in frame}
        self.assertTrue(complete_session(data,OPEN,b.end))
        data.pop(OPEN)
        self.assertFalse(complete_session(data,OPEN,b.end))

    def test_dedup_tier_reset_cooldown_exit(self):
        t = at(DAY,time(9,35))
        def make(n, st=1):
            return Result((True,)*n+(False,)*(9-n),(False,)*9,st,(0,0),10)
        state, ev = transition({},make(6),t)
        self.assertFalse(ev)
        t += cfg.FIVE
        state, ev = transition(state,make(7),t)
        self.assertEqual(ev,["BUY CE"])
        self.assertFalse(transition(state,make(8),t)[1])
        state,ev = transition(state,make(8),t+cfg.FIVE)
        self.assertEqual(ev,["BUY CE"])
        state,ev = transition(state,make(7),t+2*cfg.FIVE)
        self.assertFalse(ev)
        state,ev = transition(state,make(7),t+7*cfg.FIVE)
        self.assertEqual(ev,["BUY CE"])
        state,ev = transition(state,make(0,-1),t+8*cfg.FIVE)
        self.assertEqual(ev,["EXIT CE"])

    def test_batched_messages(self):
        self.assertEqual(list(batches(["abc","xyz","done"],limit=8)),["abc\n\nxyz","done"])

    def test_no_trading_endpoint(self):
        async def test():
            api = Dhan(None,"x","y",0)
            with self.assertRaises(ValueError):
                await api.request("/orders",{})
        asyncio.run(test())


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_round_trip_and_retry(self):
        import base64
        from state import State
        from unittest.mock import AsyncMock
        state = State(None,"private/repo","token",DAY,"g01",False)
        state.doc["symbols"]["NSE_EQ:1"] = {"bar": at(DAY,time(9,35)).isoformat()}
        state.get = AsyncMock(return_value=(200, {"sha":"known"}))
        state.api = AsyncMock(side_effect=[(409,{}),(200,{})])
        with patch("state.asyncio.sleep",new=AsyncMock()):
            await state.save()
        self.assertEqual(state.api.await_count,2)
        saved = state.api.await_args.kwargs["json"]
        self.assertEqual(saved["branch"],cfg.STATE_BRANCH)
        self.assertEqual(saved["sha"],"known")
        self.assertIn(b"NSE_EQ:1",base64.b64decode(saved["content"]))
        restarted = State(None,"private/repo","token",DAY,"g01",False)
        restarted.get = AsyncMock(return_value=(200,{"content":saved["content"]}))
        await restarted.load()
        self.assertEqual(restarted.doc,state.doc)

    async def test_mocked_completed_group_sends_then_dedupes(self):
        from state import State
        frame,daily = bars()
        boundary = frame[-1].end
        def sample(t):
            return {b.start:b for b in frame if b.end <= t}
        class MockDhan:
            async def candles(self, inst, start, stop, interval, deadline):
                if interval is None:
                    return daily
                return sample(stop)
            async def ltp(self, members, deadline):
                return {(m.segment,m.sid):122 for m in members}
        class Sender:
            def __init__(self):
                self.lines = []
            async def send(self, lines, deadline=None):
                self.lines.extend(lines)
                return 0
        sender = Sender()
        state = State(None,"","",DAY,"g01",True)
        runner = Runner(MockDhan(),sender,state,"g01",DAY,fake_master())
        with patch("scanner.now", return_value=boundary+timedelta(seconds=10)), patch(
                "scanner.time.time", return_value=(boundary+timedelta(seconds=10)).timestamp()):
            await runner.warm(10**10)
            await runner.scan(boundary)
            first = len(sender.lines)
            await runner.scan(boundary)
        self.assertGreater(first,0)
        self.assertEqual(len(sender.lines),first)
        self.assertIn("BUY CE",sender.lines[0])
        self.assertIn("Quote LTP 122.00",sender.lines[0])

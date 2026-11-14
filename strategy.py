"""Toolkit rules, scored as nine binary checks. No order logic."""
from dataclasses import dataclass
from statistics import mean

import config as cfg

NAMES = ("CPR Trend", "Price vs CPR", "Camarilla H3/H4 or L3/L4", "EMA 9/21",
         "Session VWAP", "RSI 14", "Supertrend (10,3)", "Volume spike + candle", "NIFTY Tide")


def ema(values, n):
    result = values[0]
    for v in values[1:]:
        result += (v - result) * (2 / (n + 1))
    return result


def ranges(daily, day):
    days = [daily[key] for key in sorted(daily) if key < day]
    if len(days) < 2:
        return None
    def cpr(bar):
        h, l, c = bar.h, bar.l, bar.c
        pivot, middle = (h + l + c) / 3, (h + l) / 2
        other = 2 * pivot - middle
        return max(middle, other), min(middle, other), c, (h - l) * cfg.CAM_FACTOR
    tc, bc, c, width = cpr(days[-1])
    ptc, pbc, _, _ = cpr(days[-2])
    return tc, bc, ptc, pbc, c + width / 4, c + width / 2, c - width / 4, c - width / 2


def tide(bars, daily, day):
    levels = ranges(daily, day)
    if len(bars) < cfg.TIDE_MIN_BARS or not levels:
        return None
    closes = [b.c for b in bars]
    tc, bc, *_ = levels
    fast, slow = ema(closes, cfg.EMA_FAST), ema(closes, cfg.EMA_SLOW)
    return int(closes[-1] > tc and fast > slow) - int(closes[-1] < bc and fast < slow)


def rsi_st(bars, length=cfg.SUPERTREND_LEN, multiplier=cfg.SUPERTREND_MULT):
    b = bars[0]
    atr = b.h - b.l
    middle = (b.h + b.l) / 2
    upper, lower = middle + multiplier * atr, middle - multiplier * atr
    st, prev, gain, loss = 1, b.c, None, None
    for b in bars[1:]:
        tr = max(b.h - b.l, abs(b.h - prev), abs(b.l - prev))
        atr += (tr - atr) / length
        middle = (b.h + b.l) / 2
        u, l = middle + multiplier * atr, middle - multiplier * atr
        upper = u if u < upper or prev > upper else upper
        lower = l if l > lower or prev < lower else lower
        st = (-1 if b.c < lower else 1) if st == 1 else (1 if b.c > upper else -1)
        up, down = max(b.c - prev, 0), max(prev - b.c, 0)
        gain = up if gain is None else gain + (up - gain) / cfg.RSI_LEN
        loss = down if loss is None else loss + (down - loss) / cfg.RSI_LEN
        prev = b.c
    return (100 if loss == 0 else 100 - 100 / (1 + gain / loss)), st


def supertrend_exit_flip(bars):
    """Return the new direction on an actual fast Supertrend flip, otherwise 0."""
    if len(bars) <= cfg.SUPERTREND_EXIT_LEN:
        return 0
    args = cfg.SUPERTREND_EXIT_LEN, cfg.SUPERTREND_EXIT_MULT
    previous = rsi_st(bars[:-1], *args)[1]
    current = rsi_st(bars, *args)[1]
    return current if current != previous else 0


@dataclass(frozen=True)
class Result:
    ce: tuple
    pe: tuple
    st: int
    weighted: tuple
    weighted_max: int

    def side(self):
        return "CE" if sum(x is True for x in self.ce) >= cfg.MIN_SCORE else (
            "PE" if sum(x is True for x in self.pe) >= cfg.MIN_SCORE else "")


def score(bars, daily, nifty, is_index=False):
    if len(bars) < cfg.MIN_BARS or nifty is None or not (levels := ranges(daily, bars[-1].start.date())):
        return None
    tc, bc, ptc, pbc, h3, h4, l3, l4 = levels
    b = bars[-1]
    closes = [x.c for x in bars]
    e9, e21 = ema(closes, cfg.EMA_FAST), ema(closes, cfg.EMA_SLOW)
    rsi, st = rsi_st(bars)
    session = [x for x in bars if x.start.date() == b.start.date()]
    volume = sum(x.v for x in session)
    vwap = (sum((x.h + x.l + x.c) / 3 * x.v for x in session) / volume if volume
            else mean((x.h + x.l + x.c) / 3 for x in session) if is_index else None)
    last = bars[-cfg.VOL_LEN:]
    baseline = mean(x.v for x in last)
    spike = b.v >= cfg.VOL_MULT * baseline if len(last) == cfg.VOL_LEN and baseline > 0 and volume > 0 else None
    cpr_up = 2 if bc > ptc else int(bc > pbc and tc > ptc)
    cpr_down = 2 if tc < pbc else int(bc < pbc and tc < ptc)
    cam_up = 2 if b.c > h4 else int(b.c > h3)
    cam_down = 2 if b.c < l4 else int(b.c < l3)
    ce = (bool(cpr_up), b.c > tc, bool(cam_up), e9 > e21 and b.c > e21,
          b.c > vwap if vwap is not None else None, cfg.RSI_CE[0] <= rsi <= cfg.RSI_CE[1], st > 0,
          spike and b.c > b.o if spike is not None else None, None if nifty == 0 else nifty == 1)
    pe = (bool(cpr_down), b.c < bc, bool(cam_down), e9 < e21 and b.c < e21,
          b.c < vwap if vwap is not None else None, cfg.RSI_PE[0] <= rsi <= cfg.RSI_PE[1], st < 0,
          spike and b.c < b.o if spike is not None else None, None if nifty == 0 else nifty == -1)
    weighted = tuple(sum(x is True for x in side[1:8]) + cpr + cam - int(side[2])
                     for side, cpr, cam in ((ce, cpr_up, cam_up), (pe, cpr_down, cam_down)))
    return Result(ce, pe, st, weighted, 10 - int(vwap is None) - int(spike is None))

"""Dhan instrument master and listed option strikes."""
import csv
import io
import math
from dataclasses import dataclass
from datetime import date, datetime

from config import FIVE, GROUPS

MASTER = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
SEGMENTS = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2}


@dataclass(frozen=True)
class Instrument:
    name: str
    segment: str
    sid: int
    kind: str

    @property
    def key(self):
        return SEGMENTS[self.segment], self.sid


def discover(raw, day):
    reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
    required = {"EXCH_ID", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SECURITY_ID",
                "UNDERLYING_SYMBOL", "SM_EXPIRY_DATE", "SYMBOL_NAME"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Dhan detailed master schema changed")
    equity, index, options, futures, strikes = {}, {}, {}, {}, {}
    for row in reader:
        def val(key):
            return (row.get(key) or "").strip().upper()

        kind, sid = val("INSTRUMENT"), val("SECURITY_ID")
        if not sid.isdigit():
            continue
        sid = int(sid)
        exch = val("EXCH_ID")
        if kind == "INDEX" and exch == "NSE":
            index[sid] = val("SYMBOL_NAME")
        if exch != "NSE":
            continue
        if kind == "EQUITY":
            equity[sid] = val("SYMBOL_NAME")
        if kind not in {"OPTSTK", "OPTIDX", "FUTIDX", "FUTSTK"}:
            continue
        under = val("UNDERLYING_SECURITY_ID")
        if not under.isdigit():
            continue
        under = int(under)
        try:
            expiry = date.fromisoformat(val("SM_EXPIRY_DATE")[:10])
        except ValueError:
            continue
        if expiry < day:
            continue
        name = val("UNDERLYING_SYMBOL")
        if kind == "FUTIDX":
            candidate = (expiry, sid, name)
            if under not in futures or candidate < futures[under]:
                futures[under] = candidate
        else:
            optkind = "OPTSTK" if kind == "FUTSTK" else kind
            options[(optkind, under)] = name or options.get((optkind, under), "")
            side = val("OPTION_TYPE")
            if side in {"CE", "PE"}:
                try:
                    strike = float(val("STRIKE_PRICE"))
                except ValueError:
                    continue
                if math.isfinite(strike) and strike > 0:
                    strikes.setdefault((kind, under), {}).setdefault((expiry, side), set()).add(strike)

    # Dhan uses different underlying IDs on some derivative rows than on IDX_I.
    # Match the published symbols to the actual spot rows, never guess a spot ID.
    norm = lambda s: "".join(c for c in s if c.isalnum())
    spot_index = {norm(name): sid for sid, name in index.items()}
    subjects, proxies, missing, chains = {}, {}, [], {}
    for (kind, under), name in sorted(options.items()):
        is_index = kind == "OPTIDX"
        lookup = index if is_index else equity
        alias = norm(name)
        if alias == "NIFTY50":
            alias = "NIFTY"
        spot_id = spot_index.get(alias, under) if is_index else under
        if spot_id not in lookup:
            if not (is_index and name and under in futures):
                missing.append(name or str(under))
                continue
            # Probe the published derivative-underlying ID as IDX_I at runtime.
            # Never pass a futures price off as the index spot.
            inst = Instrument(name, "IDX_I", under, "INDEX")
        else:
            name = name or lookup[spot_id]
            inst = Instrument(name, "IDX_I" if is_index else "NSE_EQ", spot_id,
                              "INDEX" if is_index else "EQUITY")
        if is_index and "".join(c for c in name if c.isalnum()) in {"NIFTY", "NIFTY50"}:
            inst = Instrument("NIFTY", inst.segment, inst.sid, inst.kind)
        subjects[inst.key] = inst
        contracts = chains.setdefault(inst.key, {})
        for pair, prices in strikes.get((kind, under), {}).items():
            contracts.setdefault(pair, set()).update(prices)
        if inst.kind == "INDEX" and under in futures:
            expiry, sid, _ = futures[under]
            proxies[inst.key] = Instrument(f"{inst.name} FUT {expiry}", "NSE_FNO", sid, "FUTIDX")
    return subjects, proxies, chains, sorted(set(missing))


def groups(subjects):
    stocks = sorted((s for s in subjects.values() if s.kind == "EQUITY"),
                    key=lambda s: (s.name, s.sid))
    size, remainder = divmod(len(stocks), len(GROUPS) - 1)
    result, pos = {}, 0
    for i, name in enumerate(GROUPS[:-1]):
        count = size + int(i < remainder)
        result[name] = stocks[pos:pos+count]
        pos += count
    result["indices"] = sorted((s for s in subjects.values() if s.kind == "INDEX"),
                               key=lambda s: s.name)
    return result


def option_levels(chain, day, side, spot):
    expiries = sorted(expiry for expiry, right in chain if expiry >= day and right == side)
    if not expiries:
        return None
    expiry = expiries[0]
    listed = chain[expiry, side]
    atm = min(listed, key=lambda k: (abs(k - spot), k))
    itm = (max((k for k in listed if k < spot), default=None) if side == "CE"
           else min((k for k in listed if k > spot), default=None))
    alternate = (max((k for k in listed if k < atm and k < spot), default=None) if side == "CE"
                 else min((k for k in listed if k > atm and k > spot), default=None))
    return expiry, atm, itm, alternate


@dataclass
class Bar:
    start: datetime
    o: float
    h: float
    l: float
    c: float
    v: int

    @property
    def end(self):
        return self.start + FIVE

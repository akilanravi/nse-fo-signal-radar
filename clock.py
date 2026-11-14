"""Exchange calendar and candle selection."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import config as cfg


def now():
    return datetime.now(cfg.IST)


def at(day, stamp):
    return datetime.combine(day, stamp, cfg.IST)


def trading_day(day, path=Path(__file__).with_name("holidays.json")):
    if day.weekday() >= 5:
        return False
    entries = json.loads(path.read_text())
    if str(day.year) not in entries:
        raise ValueError(f"NSE holiday calendar missing for {day.year}; update holidays.json")
    return day.isoformat() not in entries[str(day.year)]


def floor(t, minutes=5):
    return t.replace(minute=t.minute // minutes * minutes, second=0, microsecond=0)


def latest(t):
    boundary = floor(t - timedelta(seconds=cfg.FINALIZE_SECONDS))
    return min(boundary, at(t.date(), cfg.LAST))


def fresh(t, boundary):
    return (boundary.date() == t.date() and cfg.FIRST <= boundary.time() <= cfg.LAST
            and cfg.FINALIZE_SECONDS <= (t - boundary).total_seconds() <= cfg.MAX_STALENESS)

"""Strategy and operations knobs, all times in Asia/Kolkata."""
from datetime import time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)
STATUS_AT, FIRST, LAST = time(9, 30), time(9, 35), time(15, 15)
EXIT_BY = time(15, 18)
FIVE = timedelta(minutes=5)
FINALIZE_SECONDS = 7
MAX_STALENESS = 145
HISTORY_DAYS = 11
MIN_BARS = 45
TIDE_MIN_BARS = 21
TIDE_MINUTES = 15
EMA_FAST, EMA_SLOW = 9, 21
RSI_LEN = 14
RSI_CE, RSI_PE = (55, 75), (25, 45)
SUPERTREND_LEN, SUPERTREND_MULT = 10, 3.0
VOL_LEN, VOL_MULT = 20, 1.2
CAM_FACTOR = 1.1
MIN_SCORE = 7
COOLDOWN_MINUTES = 30  # Set to 0 to disable cooldown alerts.
STALE_GROUP_AFTER = 2  # consecutive affected candles before one warning
GROUPS = tuple(f"g{i:02}" for i in range(1, 11)) + ("indices",)
# Assign repeating UTC request slots per matrix index. Shared account limit:
# Data 5/s, 100,000/day. Quotes 1/s, max 1,000 instruments per call.
DATA_SLOT_SECONDS = 0.30
QUOTE_SLOT_SECONDS = 1.30
TELEGRAM_SLOT_SECONDS = 1.20
SLOT_LATE_TOLERANCE = 0.09
HTTP_TIMEOUT = 8
HTTP_ATTEMPTS = 4
STATE_BRANCH = "scanner-state"

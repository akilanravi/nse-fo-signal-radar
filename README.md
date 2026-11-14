# Dhan NSE F&O Telegram Alerts — GitHub Actions edition

A standalone, REST-only scanner for NSE F&O equities and indices. **Alerts only.** The only authenticated Dhan routes in the code are daily candles, intraday candles, and quote LTP. There is no order SDK, order route, WebSocket, cloud server, or self-hosted runner.

## Install in your repository

1. Create a new **private** GitHub repository named `dhan-fo-telegram-alerts`, or use your existing private repository of that name. Upload the contents of this ZIP to the repository root, keeping `.github/workflows/scanner.yml` exactly at that path and committing it to the default branch (usually `main`). GitHub's **Add file → Create new file** can be used to add that path if a browser upload drops the hidden folder. Replace the old cloud edition files. Remove the old `Dockerfile`, `compose.yaml`, `dhan_feed.py`, and `.env.example` if still present. Disable any older scheduled scanner workflows to prevent duplicate alerts or competition for the same API limits.
2. In **Settings → Secrets and variables → Actions → New repository secret**, add:

   | Name | Value |
   | --- | --- |
   | `DHAN_CLIENT_ID` | Numeric Dhan API Client ID, not UCC |
   | `DHAN_ACCESS_TOKEN` | Current Dhan access token, not API key |
   | `TELEGRAM_BOT_TOKEN` | BotFather token for one bot |
   | `TELEGRAM_CHAT_IDS` | Comma-separated numeric IDs, e.g. `111111111,222222222,333333333` |

   Each recipient must send `/start` to the bot first. Never put tokens in files, commits or screenshots. If a real token was committed before, revoke and replace it before using this edition. The workflow does not use `.env`.
3. Open **Settings → Actions → General** and make sure GitHub Actions and the four official GitHub actions used by this workflow are allowed. The YAML explicitly grants `contents: write` to its own jobs so their built-in token can write the `scanner-state` branch. If an org policy or branch rule blocks that, allow writes to the state branch or live runs will fail closed.
4. Open **Actions → Dhan NSE F&O Telegram Scanner → Run workflow**. Select **dry-run** first. On a trading day, this downloads the universe, warms history and checks at most the latest fresh candle. It never sends Telegram messages and never writes the state branch. A manual run outside market hours may not score a candle. The `live` option runs the remaining part of the day and sends actual messages. The weekday schedule uses live mode automatically.
5. Read **Actions → run → scan (g01)** (or any other group). Look for `universe`, `warmup`, `scan`, `candle_error`, `dhan_retry`, and `telegram_failed`. Logs use UTC, signal text uses IST. Compare a few candle closes, setups and listed strikes against Dhan before relying on the alerts.

Dhan documents web-generated access tokens as valid for 24 hours. Replace the repository secret before the scheduled run. Secret changes do not update an already running job. This code does not collect a Dhan PIN or TOTP and does not auto-renew tokens. To pick up a new token after an early failure, start a new manual live run after the previous run ends.

## Timing and cost

- Weekdays at **03:50 UTC / 09:20 IST**. An initial job fetches the Dhan master once per run, then the matrix starts 10 alphabetically balanced stock groups and one index group. Fail-fast is off. The longest step is the initial historical warm-up. The matrix has a **359-minute** limit because 350 minutes from 09:20 would end at 15:10. Jobs finish by design after the 15:15 close, normally before 15:18 IST. The consolidated report starts after the matrix finishes and may arrive a few minutes after 15:15. The 09:30 start message comes from the index job and shows its actual start time, not an all-groups health guarantee.
- Trade alerts begin with the **09:35** close and continue every five minutes through **15:15**, after an initial seven-second publication allowance. For delayed runners only the newest completed candle is eligible, and never more than 145 seconds after close. A missing candle is not synthesized or taken from a quote. This can skip signals when publication, retries, runner queues or delivery are slow.
- The matrix consumes roughly **78,000 runner-minutes per 20-day month** if all jobs run about 355 minutes. GitHub Free private repositories include 2,000 minutes, so **this is not a free full-month setup on a private repo**. With paid usage disabled, it will stop when the allowance is exhausted. Even with enough minutes, GitHub may delay or drop scheduled runs. Runner concurrency depends on your plan and other jobs. Set a GitHub Actions spending budget before enabling the schedule if you need a cost cap.
- `holidays.json` includes the 2026 NSE F&O holiday list, including the later January 15 amendment. Add each new year's official dates, or the scanner will stop rather than assume the exchange is open. Special weekend sessions are intentionally excluded.

## Data, strategy and alert behavior

- Dhan public detailed instrument master is fetched once and distributed to the matrix. Active NSE stock futures/options determine the universe. Active index derivatives resolve to actual cash rows where available. For an index such as NIFTYFPI that appears as a derivative underlying but has no cash row, the job probes its published underlying ID as `IDX_I`. If Dhan does not return valid spot history, the index is suppressed and reported, not replaced with a futures price.
- Dhan v2 data limits are **5 requests/second and 100,000/day**. Quote calls allow **1 request/second and 1,000 instruments/request**. Daily levels use `/charts/historical`. Each historical candle call on `/charts/intraday` takes one instrument, while `/marketfeed/ltp` batches a group's quotes. Independently scheduled jobs use distinct UTC request slots, 0.30 seconds apart for charts and 1.30 seconds apart for quotes. This assumes synchronized runner clocks and that another app is not consuming the same account's allowance. 429 and temporary errors retry with exponential backoff and stop at the freshness deadline.
- `strategy.py` ports the uploaded Toolkit's eight component formulas plus the dashboard's 15-minute NIFTY Tide (price beyond the CPR boundary and EMA9/21 aligned). The scanner applies your new binary **7/9** trigger instead of the Toolkit's trade-decision layer. CPR and Camarilla use the last two completed daily candles, EMA/RSI/Supertrend use completed intraday bars, VWAP resets each session, and the Toolkit defaults include RSI 55–75 for CE, 25–45 for PE, and volume at least 1.2× the rolling 20-bar average with candle direction. Provider data can differ from the old dashboard. For volume-less indices, the Toolkit's equal-weight session-price VWAP proxy is used and the volume spike is marked unavailable.
- The weighted score has a nominal maximum of ten, separately from the 7/9 entry test. C1 and C3 can each contribute two weighted points, C2 and C4–C8 one each, and C9 is binary only. The alert labels unavailable checks and shows the available weighted maximum if below ten.
- Entries alert on a fresh qualification, direction change, a new higher tier, or the configurable cooldown. EXIT is for a Supertrend reversal of a previously opened signal. No broker position is tracked. An alert contains the confirmed underlying close and a separate quote LTP snapshot. The nearest available expiry comes from actual listed contracts for that side. ATM is the closest listed strike to the close (lower strike on an exact tie). ITM is strictly below the underlying for CE or strictly above for PE. If ATM itself is ITM, a distinct deeper ITM is shown where available. No premium, spread or liquidity checks are performed.
- State checkpoints contain only generated signal metadata and aggregate counts on the `scanner-state` branch, never API secrets or Telegram IDs. State is written before delivery to avoid repeating alerts after a runner restart. A crash after checkpointing can therefore lose an alert, and Telegram delivery failures are counted rather than silently replaying expired signals. The branch grows over time. Keep it separate from `main` and do not merge it. If rerunning a previous workflow whose one-day master artifact has expired, start a new manual run rather than rerunning just failed jobs.
- Telegram messages are batched per group and use shared time slots to avoid per-chat bursts. Sustained stale data is reported separately. A Python job failure attempts an immediate error alert. The final report also lists failed or incomplete matrix coverage, including runner failures the Python process cannot see directly. A missing token, bot outage, runner cancellation or exhausted Actions allowance can prevent notifications.

## Tests

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Offline indicator, state, grouping and mocked scan tests were run. The public Dhan master was checked. Authenticated Dhan endpoints, Telegram delivery, GitHub state writes, and an actual Actions market session have not been verified in the build environment.

## References

- [Dhan data/quote rate limits](https://dhanhq.co/docs/v2/#rate-limit), [historical endpoints](https://dhanhq.co/docs/v2/historical-data/), [quote LTP](https://dhanhq.co/docs/v2/market-quote/), [instrument master](https://dhanhq.co/docs/v2/instruments/), [authentication](https://dhanhq.co/docs/v2/authentication/)
- [NSE 2026 F&O holidays](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf) and [January 15 amendment](https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf)
- [GitHub Actions limits](https://docs.github.com/en/actions/reference/limits), [billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [schedule reliability](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
- [Telegram bot FAQ and rate limits](https://core.telegram.org/bots/faq)

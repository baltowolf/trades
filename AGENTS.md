# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Senior Engineering & Financial Integrity Standards

- **Real Money & Absolute Financial Priority**: The code in this repository directly manages real personal capital. Correctness, financial safety, risk management, and mathematical soundness are the MAXIMUM PRIORITY. Zero tolerance for calculation bugs, unhandled exceptions, race conditions, missed stop-losses, or lot rounding errors.
- **Senior Developer Standards**: Think and code like a seasoned quantitative developer and systems engineer:
  - Deep verification before modifying logic. Every algorithmic decision must be rigorously analyzed for edge cases, drawdowns, and false signals.
  - Mathematical expectancy: R:R (Risk/Reward) must be strictly maintained (TP1 >= 1.0R to initial stop).
  - Defensive engineering: Zero external bloat (stdlib + `requests`), atomic disk I/O (`tmp` + `os.fsync()` + `os.replace()`), complete type annotations, clean and robust error boundaries.
  - Automated self-checks: Non-trivial logic must have runnable checks/assertions in `test_fixes.py` before finalizing changes.
- **Equity Strategy & Portfolio Management**:
  - **Buffett Business Quality**: Prioritize companies with wide economic moats, clean balance sheets (`Net Debt / EBITDA < 3.0x`), positive FCF, and sustainable `ROE >= 10%`. Regularly audit portfolio quality via `fundamentals.json`.
  - **Core/Satellite Integrity**: Core positions are untouchable long-term wealth generators. Satellite shares are actively traded via ATR targets with strict lot alignment. Core can never be sold by automated TP/stop signals.
  - **Capital Preservation**: Never average down on a losing active satellite. Only pyramid in profit or reload satellite at trend pullbacks when only core remains.
  - **Strict Git Automation**: Working tree must remain clean. All production improvements must be committed and pushed to the upstream remote repository (`https://github.com/baltowolf`).

## Project

Single-file MOEX stock tracker (`moex_tracker.py`, ~1800 lines). Python 3.14, sole dependency: `requests`. Runs as long-lived loop polling MOEX ISS API every 600s during MSK trading hours (10:00–23:50).

## Commands

- `python moex_tracker.py` — main loop (blocks, runs until Ctrl+C)
- `python moex_tracker.py --send-pending` — retry unsent Telegram messages
- `python moex_tracker.py --show` — print recent notifications
- `python moex_tracker.py --audit-buffett` — run Buffett fundamental audit
- `python test_fixes.py` — run regression and sanity tests

## Critical Patterns

- **Safe JSON writes**: all JSON saves use write-to-tmp + `os.fsync()` + `os.replace()` pattern. Never use plain `json.dump()` to state/notification files directly.
- **State compatibility**: `state.json` format is versioned. Deleting it triggers full re-initialization from `portfolio.json` — safe but resets peaks/sold counts.
- **Notifications dedup**: `notifications.json` deduplicates by `(key, date)` pair. Same signal won't fire twice per day.
- **Core/satellite model**: positions split into untouchable "core" (`core_count`) and tradable "satellite". Stop-loss and take-profit only act on satellite shares. Gold tickers force `core_count=0`.
- **Gold tickers hardcoded**: `{"GOLD", "SBGD", "AKGD", "TGLD", "BCSG"}` — separate buy/sell logic (SMA200+RSI vs Z-score for stocks).
- **Telegram config**: loaded via `.env` file (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) or OS environment variables. If empty or `"ВАШ_ТОКЕН_БОТА"` — test mode (no real sends, printed to stdout). Never commit real tokens to git!
- **Candle cache**: 30-min TTL in-memory (`_candles_cache`). Gold fetches 400 days of history, stocks fetch 120 days.
- **TP trigger price**: uses `max(LAST, BID)` — not just last price.
- **Pre-TP1 protection**: if price reached `buy + 2.0 ATR` but TP1 (2.5 ATR) hasn't triggered, stop moves to `buy + 0.5 ATR`.
- **Peak Tracking**: initialized with highest high over `TRAILING_LOOKBACK_DAYS` to preserve trailing stop integrity across script restarts.

## Code Style

- Russian comments and log messages throughout. Keep them Russian.
- Type hints on function signatures (`-> Optional[dict]`, `-> Tuple[...]`), not on locals.
- Constants as module-level ALL_CAPS, no config file — edit source directly.
- No classes — pure functions + module-level state dict.
- `statistics` stdlib for mean/stdev, manual implementations for RSI/ATR/EMA/SMA.

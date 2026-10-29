# Architect Mode Rules (Non-Obvious Only)

- **Senior Developer Standards & Financial Priority**: Real money management requires bulletproof architectural reliability, mathematical correctness, strict risk-to-reward (R:R >= 1.0 on TP1), and zero tolerance for data corruption or missed stops. All changes automatically committed and pushed to `https://github.com/baltowolf`.
- Single-file architecture is intentional. No refactoring into modules unless explicitly requested — the user runs this as a standalone script.
- No database — all persistence is three JSON files: `portfolio.json` (input), `state.json` (runtime), `notifications.json` (log). All writes use atomic tmp+fsync+replace.
- `requests` is the only external dependency. Indicator math (RSI/ATR/EMA/SMA) is hand-rolled using `statistics` stdlib. Adding numpy/pandas would break the minimal-dependency design.
- Concurrency: `ThreadPoolExecutor(max_workers=6)` for candle fetches only. Main loop is single-threaded sequential: stop-loss → take-profit → gold-sell → gold-buy → stock-buy.
- Signal evaluation order matters: stops checked before TPs. A position can trigger stop and TP in same iteration — stop wins because it's checked first and `notified_stop` blocks further processing.
- `state.json` reinit cascade: changing `buy_price`, increasing count, changing `core_count`, or count < core in `portfolio.json` all trigger full position reinit with ATR recalc and notification history wipe for that ticker.
- No graceful shutdown beyond KeyboardInterrupt handler. State saved on Ctrl+C and after each iteration.

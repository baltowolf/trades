# Code Mode Rules (Non-Obvious Only)

- **Senior Developer Standards & Financial Integrity**:
  - The script manages REAL CAPITAL: correctness, mathematical soundness, and risk management are absolute priority #1.
  - Zero tolerance for calculation errors, rounding bugs, unhandled exceptions, or missed stops.
  - All algorithm changes must preserve strictly positive mathematical expectancy (R:R >= 1.0 on TP1).
  - All non-trivial logic changes must be covered by self-checks in `test_fixes.py` before finalizing.
  - Automatically commit and push all production improvements to remote (`https://github.com/baltowolf`).
- All JSON file writes MUST use tmp + `os.fsync()` + `os.replace()` pattern (see `save_state()`, `save_notifications()`). Direct `json.dump()` to target file = corruption risk on power loss.
- Gold tickers set `GOLD_TICKERS` is hardcoded — adding a new gold ETF requires editing it AND verifying `core_count=0` enforcement in `init_position()`.
- `evaluate_profit_taking()` sells from satellite only (`original_count - core_count`). Never subtract from `core_count`. Lot-align all share quantities: `(shares // lot) * lot`.
- `_tp_trigger_price()` returns `max(LAST, BID)` — do not change to just `last`. This handles bid-above-last edge case.
- Indicator functions (`_rsi`, `_atr`, `_ema`, `_sma`) are manual implementations — do not replace with pandas/numpy; `requests` is the only external dependency.
- `sync_state_with_portfolio()` triggers reinit on buy_price change, count increase, core_count change, or core breach. Clearing `notified_profit`/`notified_stop` for that ticker is required on reinit.
- `dispatch_notification()` deduplicates by `(key, date)` via `notification_exists()`. Key format: `"{TICKER}_{TRIGGER}"` (e.g., `"SBER_LEVEL_0"`, `"YDEX_STOP"`).
- Keep all comments and log messages in Russian.

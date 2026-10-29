# Debug Mode Rules (Non-Obvious Only)

- **Senior Developer Standards & Financial Priority**: Real capital at stake. Zero tolerance for missed stops, calculation bugs, or lot sizing discrepancies. Non-trivial logic must be covered by runnable assertions in `test_fixes.py`. All improvements automatically committed and pushed to `https://github.com/baltowolf`.
- Run `python test_fixes.py` for regression verification and self-checks. Verify market logic by running `python moex_tracker.py` or inspecting runtime JSON output.
- Test mode activates when `TELEGRAM_BOT_TOKEN` is empty or equals `"ВАШ_ТОКЕН_БОТА"` — Telegram messages print to stdout instead. Check `is_test_mode()`.
- `_candles_cache` is in-memory dict with 30-min TTL. Stale indicator data during debugging = restart the process.
- If indicators return defaults (RSI=50, ATR=0, Z=0), candle fetch likely failed silently — `fetch_candles()` returns `[]` on any exception.
- `state.json` peak values only increase (never decrease). If peak looks wrong, delete `state.json` to reinit (safe but resets all sold counts and peaks).
- Notification dedup can hide bugs: same `(key, date)` pair won't fire twice. Delete the entry from `notifications.json` or change date to retest.
- stdout encoding forced to UTF-8 at startup (lines 34-39). If Russian text garbled, that reconfigure failed silently.

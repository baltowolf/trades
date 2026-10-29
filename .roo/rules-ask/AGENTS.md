# Ask Mode Rules (Non-Obvious Only)

- **Senior Developer Standards & Financial Priority**: The system controls real capital. Highest engineering, risk management, and mathematical soundness standards apply. Production changes automatically committed and pushed to `https://github.com/baltowolf`.
- Entire project is centered on `moex_tracker.py` (~1800 lines). Pure functions, no classes.
- Docstring at top says "v3.5" but `print_header()` says "v3.6" — header is authoritative.
- `portfolio.json` is user-edited input (positions + cash). `state.json` is computed runtime state (ATR, targets, peaks, sold counts). `notifications.json` is append-only notification log.
- "Core" vs "satellite" is the central position model. Core shares are never sold by the system. All TP/stop calculations exclude core from tradable quantity.
- Gold and stock paths have completely different indicator logic: gold uses SMA200+RSI for entry, stocks use Z-score+RSI+EMA50+day-drop. Different ATR multipliers for stops/TPs too.
- Constants are not in a config file — they're module-level `ALL_CAPS` in source. Changing parameters means editing `moex_tracker.py` directly.

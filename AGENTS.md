# AGENTS.md

This file guides agents working on this repository.

## Senior Engineering & Financial Integrity Standards

- **Real money, absolute priority.** The code manages real personal capital. Correctness, risk management, and mathematical soundness are priority #1. Zero tolerance for calculation errors, unhandled exceptions, missed exits, or lot-rounding mistakes.
- **Sell-only scope.** The tracker only recommends sells. It does not open positions, does not size new entries, and does not require fundamental data. Input is `portfolio.json`; output is Telegram sell alerts.
- **Fail-closed.** A malformed `portfolio.json` or `state.json` must never cause false “all clear” behavior. Skip the iteration and report the error.
- **Atomic disk I/O.** Every JSON write uses `tmp` + `os.fsync()` + `os.replace()`. Never write directly to state or notification files.
- **Self-checks.** Any non-trivial logic change must be covered in `test_fixes.py` before finalizing.

## Project

Single-file sell-only MOEX tracker (`moex_tracker.py`, ~1900 lines). Python 3.14, sole external dependency: `requests`. Runs as a long-lived loop polling MOEX ISS every 600 s during MSK trading hours (10:00–23:50).

Input `portfolio.json`:

```json
{
  "SBER": {"count": 14, "buy_price": 285.99},
  "LKOH": {"count": 1, "buy_price": 6065.5},
  "CASH": 10448
}
```

- `buy_price` is the entry price. The fixed grid and all stops are calculated from it.
- `target` is optional and kept for backward compatibility; it no longer affects levels.
- `stop` is optional. If absent or above entry, the initial stop is `-6%` from entry.
- If the current price is already above the final +22% level, the plan sells the whole position.

## Commands

- `python moex_tracker.py` — main loop (blocks until Ctrl+C)
- `python moex_tracker.py --once` — one diagnostic iteration
- `python moex_tracker.py --show` — recent notifications
- `python moex_tracker.py --send-pending` — retry unsent Telegram messages
- `python moex_tracker.py --stats` — closed-trade statistics
- `python moex_tracker.py --migrate` — backup and reset state to the sell-only format
- `python moex_tracker.py --reset-plan TICKER` — recompute a plan from the entry price
- `python test_fixes.py` — regression and sanity tests

`--screen` and `--ideas-check` are intentionally removed. `fundamentals.json` and `ideas.json` were removed from the project.

## Critical Patterns

- **State schema v4.** `state.json` requires `schema_version: 4`. A different schema is rejected until `--migrate` is run. `PLAN_LOGIC_VERSION = 2` marks the fixed-percentage plan; old ATR plans are upgraded on sync without losing `sold`, `realized_pnl`, or `opened_at`.
- **Atomic writes.** `atomic_write_json()` is the only writer for state and notifications.
- **Portfolio loader is strict.** Missing/invalid `portfolio.json` returns an error; the iteration is skipped. Never treat a broken portfolio as empty.
- **Entry anchor.** `plan_anchor = avg_cost = buy_price` (the entry price). All grid levels and percentage stops are calculated from it. `avg_cost` is used for P&L, tax, and break-even. `target` is informational only.
- **Fixed grid.** TP1 = `+7%` (sell 30%), TP2 = `+14%` (sell 30%), TP3 = `+22%` (sell the whole remainder, 40%). The final stage always sells the whole remainder. If the price gaps through several levels, sell the cumulative share of the reached levels; if already above TP3, sell everything.
- **Peak.** `peak = max(entry, last)`, then only increases. The 5% trailing stop is used only after the +14% milestone, so seeding `peak` with the entry does not cause a false pre-TP1 liquidation.
- **Initial stop.** `entry * (1 - 6%)`, or the user-provided `stop`. In the 5-6% range the wider 6% is used to absorb normal 3-5% blue-chip pullbacks.
- **Stop ratchet.** Before the `+7%` milestone the stop stays at the initial `-6%` and is not tightened (old narrower stops are widened on upgrade). After `+7%`: `+0.5%` from entry (true breakeven after commission and NDFL). After `+14%`: `+8%` from entry and a 5% trailing stop from the peak, but never below `+8%`. Near `+22%` the trail is about `+16%`. After TP3 the position is closed. Once a milestone is reached, `stop_current` never decreases.
- **Stop trigger price.** `min(last, bid)`; TP trigger price uses `bid` with `last` fallback.
- **Pending partial sale.** After a partial TP recommendation, `pending_sell_shares` prevents an unchanged `portfolio.json` from being mistaken for a manual buy. Do not reinitialize the plan while the sale is pending.
- **Time exit.** After 180 days from plan inception (medium term 3-6 months), recommend selling the whole position.
- **Closed positions.** After a full exit signal, `exit_alerted=True` suppresses repeated alerts until `portfolio.json` is updated or `--reset-plan` is run.
- **Telegram.** Dedup by `(key, date)`; key format `TICKER_TRIGGER`. If the token is empty or `ВАШ_ТОКЕН_БОТА`, run in test mode.
- **Russian comments and log messages.** Keep them Russian.

## Code Style

- Single file, pure functions, module-level state dict. No classes.
- Type hints on function signatures, not on locals.
- Constants are module-level ALL_CAPS; no config file.
- `requests` is the only external dependency; everything else is stdlib.
- Manual indicator implementations (`_rsi`, `_atr`, `_ema`, `_sma`) — do not replace with pandas/numpy.

## Git

- Working tree must remain clean.
- All production improvements must be committed and pushed to the upstream remote.
- Never commit real Telegram tokens or `.env`.
- `state.json` and `notifications.json` are runtime files and stay gitignored.

# AGENTS.md

This file guides agents working on this repository.

## Senior Engineering & Financial Integrity Standards

- **Real money, absolute priority.** The code manages real personal capital. Correctness, risk management, and mathematical soundness are priority #1. Zero tolerance for calculation errors, unhandled exceptions, missed exits, or lot-rounding mistakes.
- **Sell-only scope.** The tracker only recommends sells. It does not open positions, does not size new entries, and does not require fundamental data. Input is `portfolio.json`; output is Telegram sell alerts.
- **Fail-closed.** A malformed `portfolio.json` or `state.json` must never cause false “all clear” behavior. Skip the iteration and report the error.
- **Atomic disk I/O.** Every JSON write uses `tmp` + `os.fsync()` + `os.replace()`. Never write directly to state or notification files.
- **Self-checks.** Any non-trivial logic change must be covered in `test_fixes.py` before finalizing.

## Project

Single-file sell-only MOEX tracker (`moex_tracker.py`, ~1200 lines). Python 3.14, sole external dependency: `requests`. Runs as a long-lived loop polling MOEX ISS every 600 s during MSK trading hours (10:00–23:50).

Input `portfolio.json`:

```json
{
  "SBER": {"count": 14, "buy_price": 285.99, "target": 380.0},
  "LKOH": {"count": 1, "buy_price": 6065.5},
  "CASH": 10448
}
```

- `target` is optional. If absent, targets are ATR-based.
- `stop` is optional. If absent, stop is 3 ATR capped at 18%.
- If `target <= current price`, the plan immediately recommends selling the whole position.

## Commands

- `python moex_tracker.py` — main loop (blocks until Ctrl+C)
- `python moex_tracker.py --once` — one diagnostic iteration
- `python moex_tracker.py --show` — recent notifications
- `python moex_tracker.py --send-pending` — retry unsent Telegram messages
- `python moex_tracker.py --stats` — closed-trade statistics
- `python moex_tracker.py --migrate` — backup and reset state to the sell-only format
- `python moex_tracker.py --reset-plan TICKER` — recompute a plan from the current price
- `python test_fixes.py` — regression and sanity tests

`--screen` and `--ideas-check` are intentionally removed. `fundamentals.json` and `ideas.json` were removed from the project.

## Critical Patterns

- **State schema v4.** `state.json` requires `schema_version: 4`. A different schema is rejected until `--migrate` is run.
- **Atomic writes.** `atomic_write_json()` is the only writer for state and notifications.
- **Portfolio loader is strict.** Missing/invalid `portfolio.json` returns an error; the iteration is skipped. Never treat a broken portfolio as empty.
- **Plan anchor.** `plan_anchor` is the current price at plan inception. `avg_cost` is only for P&L, tax, and break-even. Never lower `stop_current`.
- **Peak starts at plan inception.** `peak = max(plan_anchor, last)`. Do not seed it from a pre-plan historical high; that can push the stop above the current price and trigger a false liquidation.
- **Initial stop.** `plan_anchor - min(3 ATR, 18% * plan_anchor)`, or the user-provided `stop`.
- **ATR ladder without target.** TP1 = +2 ATR (40%), TP2 = +4 ATR (30%), TP3 = +6 ATR (all remainder).
- **Target ladder.** TP1 = +2 ATR (40%), TP2 = +4 ATR (30%), final = target (remainder 30%). If target between TP1 and TP2: TP1 (50%), final = target (50%). The final stage always sells the whole remainder.
- **Target at or below current price.** Sell the whole position immediately.
- **Stop ratchet.** At Stage 0: initial stop holds without pre-TP1 tightening (no premature shakeouts). After TP1: break-even including commission and NDFL. After TP2: stop moves to TP1 level (`plan_anchor + 2 ATR`), and trailing chandelier (`peak - 3 ATR`) protects the free runner without dipping below TP1.
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

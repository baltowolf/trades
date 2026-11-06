# AGENTS.md

This file guides agents working on this repository.

## Senior Engineering & Financial Integrity Standards

- **Real money, absolute priority.** The code manages real personal capital. Correctness, risk management, and mathematical soundness are priority #1. Zero tolerance for calculation errors, unhandled exceptions, missed exits, or lot-rounding mistakes.
- **Positive expectancy only.** New entries must have `target_r >= 2.0` and `E_R >= MIN_EXPECTANCY_R` after commissions, slippage, and NDFL. TP1 is at `1.0R`, so R:R on the first partial is at least 1.0.
- **No black-box optimization.** Use established robust methods: cross-sectional factor z-scores, value + momentum, low-volatility, Bayesian win-rate shrinkage, half-Kelly, fixed-fractional sizing, volatility targeting, correlation clusters. Do not tune weights on a small historical sample.
- **Fail-closed.** A malformed `portfolio.json`, `state.json`, or `ideas.json` must never cause buys “from scratch”. Skip the iteration and keep exits running where possible.
- **Atomic disk I/O.** Every JSON write uses `tmp` + `os.fsync()` + `os.replace()`. Never write directly to state or notification files.
- **Self-checks.** Any non-trivial logic change must be covered in `test_fixes.py` before finalizing.

## Project

Single-file speculative MOEX tracker (`moex_tracker.py`, ~3300 lines). Python 3.14, sole external dependency: `requests`. Runs as a long-lived loop polling MOEX ISS every 600 s during MSK trading hours (10:00–23:50).

The portfolio is fully speculative:

- No core/satellite split. All shares are tradable.
- A stop, the final ladder stage, horizon expiry, or thesis invalidation exits the whole position.
- New entries only from active VTB Investment ideas in `ideas.json`.
- Holding horizon is weeks/months; `valid_until` and `MAX_LEGACY_HOLDING_DAYS` enforce it.
- Gold and other BPIFFs are legacy-only until exit; no new gold purchases.

## Commands

- `python moex_tracker.py` — main loop (blocks until Ctrl+C)
- `python moex_tracker.py --show` — recent notifications
- `python moex_tracker.py --send-pending` — retry unsent Telegram messages
- `python moex_tracker.py --screen` — quantitative cross-sectional screen
- `python moex_tracker.py --ideas-check` — validate `ideas.json` and `fundamentals.json`
- `python moex_tracker.py --stats` — paper trade statistics
- `python moex_tracker.py --migrate` — backup and migrate v3 → v4
- `python moex_tracker.py --reset-plan TICKER` — recompute a plan from the current price
- `python moex_tracker.py --once` — run one diagnostic iteration
- `python test_fixes.py` — regression and sanity tests

## Critical Patterns

- **State schema v4.** `state.json` requires `schema_version: 4`. A v3 state is rejected until `--migrate` is run. Never silently reset state on a corrupt file.
- **Atomic writes.** `atomic_write_json()` is the only writer for state and notifications.
- **Portfolio loader is strict.** Missing/invalid `portfolio.json` returns an error; the iteration is skipped. Never treat a broken portfolio as empty cash.
- **Ideas are manual.** `ideas.json` fields: `entry_low`, `entry_high`, `target`, `stop`, `published`, `valid_until`, `conviction`, `active`. One active idea per ticker. Horizon must be <= 180 days.
- **Fundamentals are manual.** `fundamentals.json` schema v2: `pe`, `pb`, `ev_ebitda`, `roe`, `fcf_yield`, `fcf_positive`, `net_debt_ebitda`, `eps_growth`, `revenue_growth`, `div_yield`, `earnings_stability`. Yields and growth are in percent. Stale data older than 180 days blocks new entries.
- **Plan anchor vs average cost.** `plan_anchor` is the forward-looking reference for R and targets; `avg_cost` is only for P&L and tax. This avoids the sunk-cost fallacy for legacy/adopted positions. Never lower `stop_current`.
- **Peak starts at plan inception.** `peak` must be `max(plan_anchor, last)` for a new plan. Do not seed it from a pre-plan historical high; that can push the stop above the current price and trigger a false liquidation.
- **Dynamic ladder.** `build_levels()` builds 2 or 3 stages from R and the VTB target. The final stage sells the whole remainder. Lot-align all quantities; warn when an odd remainder is smaller than a lot.
- **Cost-aware break-even.** `breakeven_price()` includes commission and NDFL. TP1 moves the stop to plan-anchor break-even, not to the raw entry price.
- **Risk engine.** `plan_entry_size()` enforces per-trade risk, max position, cash reserve, ADV share, half-Kelly, sector, correlation cluster, and portfolio heat. `portfolio_volatility()` enforces the 25% annualized portfolio-vol cap. Circuit breakers halt new entries only; exits still run.
- **No averaging down.** A manual buy reinitializes the plan as a new trade. Do not add to a losing active position.
- **Exit priority.** Stop → thesis invalidation → TP ladder → time exit. A stop wins over a TP. A thesis invalidation wins over a TP because old targets are invalid.
- **Closed positions.** After a full exit signal, `exit_alerted=True` suppresses repeated alerts until `portfolio.json` is updated or `--reset-plan` is run. Never reinitialize a closed position just because the user has not updated the file yet.
- **Screener.** Universe = `ideas.json` + `fundamentals.json` + holdings, excluding gold tickers. Factor scores use winsorized, sector-neutral z-scores. It only ranks and explains; entries are initiated only by the entry engine.
- **Telegram.** Dedup by `(key, date)`; key format `TICKER_TRIGGER`. HTML-escape user-provided names and theses. If the token is empty or `ВАШ_ТОКЕН_БОТА`, run in test mode.
- **Russian comments and log messages.** Keep them Russian.

## Code Style

- Single file, pure functions, module-level state dict. No classes.
- Type hints on function signatures, not on locals.
- Constants are module-level ALL_CAPS; no config file.
- `requests` is the only external dependency; everything else is stdlib.
- Manual indicator implementations (`_rsi`, `_atr`, `_ema`, `_sma`) — do not replace with pandas/numpy.
- Keep the `ideas.example.json` template in sync with the loader schema.

## Git

- Working tree must remain clean.
- All production improvements must be committed and pushed to the upstream remote.
- Never commit real Telegram tokens or `.env`.
- `state.json` and `notifications.json` are runtime files and stay gitignored.

"""Comprehensive self-checks for moex_tracker v4."""
import json
import math
import os
import tempfile
from datetime import datetime, timedelta

import moex_tracker as m

TODAY = datetime.now(m.MSK_TZ).date()
D_MINUS_10 = (TODAY - timedelta(days=10)).isoformat()
D_MINUS_30 = (TODAY - timedelta(days=30)).isoformat()
D_MINUS_91 = (TODAY - timedelta(days=91)).isoformat()
D_PLUS_60 = (TODAY + timedelta(days=60)).isoformat()


def _stock(ticker="AAA", last=100.0, atr=5.0, lot=1, **kwargs):
    data = {
        "ticker": ticker,
        "last": last,
        "high": last,
        "bid": last,
        "ask": last,
        "change_pct": 0.0,
        "volume": 1_000_000_000.0,
        "lot_size": lot,
        "name": ticker,
        "listing_level": 1,
        "is_gold": False,
        "atr": atr,
        "atr_pct": (atr / last * 100.0) if last > 0 else 0.0,
        "rsi": 50.0,
        "z20": 0.0,
        "trend_ok": True,
        "above_sma200": True,
        "trend_up": True,
        "sma50": last * 0.95,
        "sma200": last * 0.90,
        "sma200_slope_pct": 0.1,
        "ema50": last * 0.95,
        "momentum_12_1": 0.10,
        "momentum_6_1": 0.05,
        "vol60": 0.25,
        "max_dd_252": 0.15,
        "beta": 1.0,
        "ou_half_life": 20.0,
        "avg_value_20": 1_000_000_000.0,
        "highest_high": last,
        "returns_60": [0.01, -0.01] * 30,
    }
    data.update(kwargs)
    return data


def _idea(entry_low=95.0, entry_high=105.0, target=140.0, stop=90.0,
          conviction=4, active=True, published=None, valid_until=None):
    return {
        "name": "AAA",
        "sector": "Банки",
        "source": "VTB My Investments",
        "published": published or D_MINUS_10,
        "valid_until": valid_until or D_PLUS_60,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "target": target,
        "stop": stop,
        "conviction": conviction,
        "active": active,
        "thesis": "тестовая идея",
    }


# ==============================================================================
# 1. ИНДИКАТОРЫ
# ==============================================================================

assert m._rsi([100.0] * 20, 14) == 50.0, "Flat RSI must be 50.0"
assert m._rsi([float(x) for x in range(1, 30)], 14) == 100.0, "Up RSI must be 100.0"
assert m._rsi([float(x) for x in range(30, 0, -1)], 14) == 0.0, "Down RSI must be 0.0"
assert m._sma([1, 2, 3, 4, 5], 3) == 4.0
assert m._ema([1, 2, 3, 4, 5], 3) > 3.0

candles = [[100.0, 100.0, 110.0, 90.0, 0.0, 0.0] for _ in range(20)]
assert abs(m._atr(candles, 14) - 20.0) < 1e-9, "ATR must be 20.0"
assert abs(m._linreg_slope([1.0, 2.0, 3.0, 4.0, 5.0]) - 1.0) < 1e-9
assert abs(m._max_drawdown([100.0, 120.0, 90.0, 110.0]) - 0.25) < 1e-9
assert m._momentum([100.0] * 22 + [122.0], 21, 0) > 0.21
assert m._realized_vol([100.0, 101.0, 99.0, 102.0, 98.0] * 10) > 0.0
assert abs(m._correlation([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) - 1.0) < 1e-9
assert abs(m._beta([0.01, -0.01] * 30, [0.01, -0.01] * 30) - 1.0) < 1e-9
mean_reverting = [100.0 * math.exp(0.5 * math.sin(i / 5.0)) for i in range(150)]
assert m._ou_half_life(mean_reverting) is not None

# ==============================================================================
# 2. ИЗДЕРЖКИ, БЕЗУБЫТОК, ОЖИДАНИЕ
# ==============================================================================

be = m.breakeven_price(100.0)
assert be > 100.0
assert abs(m.net_proceeds(10, be, 100.0)["net_profit"]) < 0.01, \
    "Net profit at breakeven must be ~0"
loss = m.net_proceeds(10, 90.0, 100.0)
assert loss["tax"] == 0.0, "No tax on loss"
assert loss["net_profit"] < 0

exp = m.trade_expectancy(100.0, 90.0, 140.0, 0.4)
assert exp is not None
assert abs(exp["target_r"] - 4.0) < 1e-9
assert exp["expectancy_r"] > m.MIN_EXPECTANCY_R

assert m.bayesian_win_rate({}) == 0.40
assert m.bayesian_win_rate({"wins": 100, "losses": 0}) == m.P_WIN_MAX
assert m.bayesian_win_rate({"wins": 0, "losses": 100}) == m.P_WIN_MIN

# ==============================================================================
# 3. ЛАДДЕР
# ==============================================================================

levels3 = m.build_levels(100.0, 90.0, 140.0, 100, 1)
assert levels3 is not None and len(levels3) == 3
assert [l["cum_count"] for l in levels3] == [40, 70, 100]
assert levels3[-1]["is_final"] is True
assert levels3[0]["price"] == 110.0 and levels3[1]["price"] == 120.0

levels2 = m.build_levels(100.0, 90.0, 122.0, 100, 1)
assert levels2 is not None and len(levels2) == 2
assert [l["cum_count"] for l in levels2] == [50, 100]

assert m.build_levels(100.0, 90.0, 114.0, 100, 1) is None, \
    "target_r < MIN_RR must be rejected"

small = m.build_levels(100.0, 90.0, 140.0, 1, 1)
assert small is not None and len(small) == 1 and small[0]["is_final"]

two_lots = m.build_levels(100.0, 90.0, 140.0, 2, 1)
assert two_lots is not None and len(two_lots) == 2
assert two_lots[0]["cum_count"] == 1

legacy = m.build_legacy_levels(100.0, 5.0, 100, 1)
assert legacy is not None and len(legacy) == 3
assert legacy[-1]["cum_count"] == 100 and legacy[-1]["is_final"]

# ==============================================================================
# 4. СТОПЫ И ПЛАНЫ
# ==============================================================================

assert m.initial_stop_for_idea(_idea(), 100.0, 5.0) == 90.0
assert m.initial_stop_for_idea(_idea(stop=96.0), 100.0, 5.0) == 96.0
assert m.initial_stop_for_idea(_idea(entry_low=80.0, stop=70.0), 100.0, 5.0) is None
assert m.initial_stop_for_legacy(100.0, 5.0) == 85.0

plan_legacy = m.build_position_plan(
    "AAA", {"count": 100, "buy_price": 95.0}, _stock(), None, TODAY
)
assert plan_legacy is not None and plan_legacy["legacy"] is True
assert plan_legacy["initial_stop"] == 85.0
assert plan_legacy["targets"] == [110.0, 120.0, 130.0]

plan_idea = m.build_position_plan(
    "AAA", {"count": 100, "buy_price": 95.0}, _stock(), _idea(), TODAY
)
assert plan_idea is not None and plan_idea["legacy"] is False
assert plan_idea["initial_stop"] == 90.0
assert plan_idea["targets"] == [110.0, 120.0, 140.0]

plan_invalid = m.build_position_plan(
    "AAA", {"count": 100, "buy_price": 95.0}, _stock(),
    _idea(target=80.0), TODAY
)
assert plan_invalid is not None
assert plan_invalid["legacy"] is False and plan_invalid["idea_invalid"] is True

state_exit = {"positions": {"AAA": plan_invalid}}
thesis = m.evaluate_thesis_exit(
    "AAA", {"count": 100, "buy_price": 95.0},
    _stock(last=100.0, bid=99.0), state_exit,
    {"AAA": _idea(target=80.0)}, True
)
assert thesis is not None and "недействителен" in thesis["reason"]

pstate_stop = {
    "plan_anchor": 100.0,
    "initial_stop": 85.0,
    "stop_current": 85.0,
    "risk_per_share": 15.0,
    "initial_risk_rub": 1500.0,
    "entry_atr": 5.0,
    "current_atr": 5.0,
    "peak": 100.0,
    "avg_cost": 100.0,
    "levels": legacy,
    "stage": 0,
    "sold": 0,
    "original_count": 100,
    "exit_alerted": False,
}
state_stop = {"positions": {"AAA": pstate_stop}}
stop_plan = m.evaluate_stop_loss(
    "AAA", {"count": 25, "buy_price": 100.0},
    {"last": 70.0, "bid": 69.0, "lot_size": 10}, state_stop
)
assert stop_plan is not None
assert stop_plan["shares"] == 20, f"Stop must be lot-aligned to 20, got {stop_plan['shares']}"
assert stop_plan["lots"] == 2
assert stop_plan["is_final"] is True

pstate_stop["exit_alerted"] = True
assert m.evaluate_stop_loss(
    "AAA", {"count": 25, "buy_price": 100.0},
    {"last": 70.0, "bid": 69.0, "lot_size": 10}, state_stop
) is None
pstate_stop["exit_alerted"] = False

# ==============================================================================
# 5. ФИКСАЦИЯ ПРИБЫЛИ
# ==============================================================================

pstate_tp = dict(plan_idea)
pstate_tp["levels"] = levels3
pstate_tp["sold"] = 0
pstate_tp["stage"] = 0
state_tp = {"positions": {"AAA": pstate_tp}}

tp1 = m.evaluate_profit_taking(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 111.0, "bid": 111.0, "lot_size": 1}, state_tp
)
assert tp1 is not None
assert tp1["shares"] == 40 and tp1["level_name"] == "TP1"
assert tp1["new_stage"] == 1
assert tp1["stop_after"] >= 102.5

pstate_tp["sold"] = 70
pstate_tp["stage"] = 2
tp3 = m.evaluate_profit_taking(
    "AAA", {"count": 30, "buy_price": 95.0},
    {"last": 141.0, "bid": 141.0, "lot_size": 1}, state_tp
)
assert tp3 is not None and tp3["is_final"] is True and tp3["shares"] == 30

pstate_tp["sold"] = 0
pstate_tp["stage"] = 0
gap = m.evaluate_profit_taking(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 141.0, "bid": 141.0, "lot_size": 1}, state_tp
)
assert gap is not None and gap["is_final"] is True and gap["shares"] == 100

plan_small = m.build_position_plan(
    "AAA", {"count": 1, "buy_price": 95.0}, _stock(lot=1), _idea(), TODAY
)
state_small = {"positions": {"AAA": plan_small}}
small_tp = m.evaluate_profit_taking(
    "AAA", {"count": 1, "buy_price": 95.0},
    {"last": 141.0, "bid": 141.0, "lot_size": 1}, state_small
)
assert small_tp is not None and small_tp["shares"] == 1

# ==============================================================================
# 6. TIME EXIT
# ==============================================================================

pstate_time = dict(plan_legacy)
pstate_time["opened_at"] = D_MINUS_91
state_time = {"positions": {"AAA": pstate_time}}
time_plan = m.evaluate_time_exit(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 100.0, "bid": 99.0, "lot_size": 1}, state_time, TODAY
)
assert time_plan is not None and time_plan["is_final"] is True

pstate_idea_time = dict(plan_idea)
pstate_idea_time["valid_until"] = (TODAY - timedelta(days=10)).isoformat()
state_idea_time = {"positions": {"AAA": pstate_idea_time}}
idea_time = m.evaluate_time_exit(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 100.0, "bid": 99.0, "lot_size": 1}, state_idea_time, TODAY
)
assert idea_time is not None

pstate_idea_time["valid_until"] = (TODAY - timedelta(days=2)).isoformat()
assert m.evaluate_time_exit(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 100.0, "bid": 99.0, "lot_size": 1}, state_idea_time, TODAY
) is None

# ==============================================================================
# 7. РИСК-ДВИЖОК
# ==============================================================================

shares, details = m.plan_entry_size(
    anchor=100.0, stop=90.0, target=140.0, p=0.4,
    equity=300_000.0, cash=300_000.0, adv=1_000_000_000.0, lot=1,
    sector_value=0.0, cluster_value=0.0, open_risk=0.0,
    pending_value=0.0, pending_risk=0.0, n_positions=0, n_pending=0,
)
assert shares == 225, f"Risk sizing must be 225, got {shares}"
assert abs(details["risk_rub"] - 2250.0) < 1e-9

shares_sector, _ = m.plan_entry_size(
    anchor=100.0, stop=90.0, target=140.0, p=0.4,
    equity=300_000.0, cash=300_000.0, adv=1_000_000_000.0, lot=1,
    sector_value=89_000.0, cluster_value=0.0, open_risk=0.0,
    pending_value=0.0, pending_risk=0.0, n_positions=0, n_pending=0,
)
assert shares_sector == 10

shares_heat, _ = m.plan_entry_size(
    anchor=100.0, stop=90.0, target=140.0, p=0.4,
    equity=300_000.0, cash=300_000.0, adv=1_000_000_000.0, lot=1,
    sector_value=0.0, cluster_value=0.0, open_risk=14_900.0,
    pending_value=0.0, pending_risk=0.0, n_positions=0, n_pending=0,
)
assert shares_heat == 10

shares_cash, details_cash = m.plan_entry_size(
    anchor=100.0, stop=90.0, target=140.0, p=0.4,
    equity=300_000.0, cash=10_000.0, adv=1_000_000_000.0, lot=1,
    sector_value=0.0, cluster_value=0.0, open_risk=0.0,
    pending_value=0.0, pending_risk=0.0, n_positions=0, n_pending=0,
)
assert shares_cash == 0 and "reason" in details_cash

shares_max, _ = m.plan_entry_size(
    anchor=100.0, stop=90.0, target=140.0, p=0.4,
    equity=300_000.0, cash=300_000.0, adv=1_000_000_000.0, lot=1,
    sector_value=0.0, cluster_value=0.0, open_risk=0.0,
    pending_value=0.0, pending_risk=0.0,
    n_positions=m.MAX_POSITIONS, n_pending=0,
)
assert shares_max == 0

portfolio_risk = {"AAA": {"count": 10, "buy_price": 100.0}}
tracked_risk = {"AAA": _stock(last=100.0)}
state_risk = {"positions": {"AAA": {"stop_current": 90.0}}}
assert m.open_portfolio_heat(state_risk, portfolio_risk, tracked_risk) == 100.0

funds_risk = {"AAA": {"sector": "Банки"}}
assert m.sector_exposure(portfolio_risk, tracked_risk, funds_risk, "Банки") == 1000.0

returns = [0.01, -0.01] * 30
cluster_value, max_corr, correlated = m.correlation_cluster(
    returns, {"BBB": 1000.0}, {"BBB": returns}
)
assert cluster_value == 1000.0 and max_corr > 0.99 and "BBB" in correlated

vol = m.portfolio_volatility(
    {"AAA": 150_000.0, "BBB": 150_000.0},
    {"AAA": returns, "BBB": returns},
    300_000.0,
)
assert vol > 0.0

state_cb = m.default_state()
state_cb["equity_peak"] = 1000.0
state_cb["day_date"] = TODAY.isoformat()
state_cb["day_start_equity"] = 1000.0
cb = m.update_circuit_breaker(state_cb, 850.0, TODAY)
assert cb["halt"] is True
assert len(cb["reasons"]) >= 1

# ==============================================================================
# 8. СИНХРОНИЗАЦИЯ ПОРТФЕЛЯ
# ==============================================================================

state_sync = m.default_state()
portfolio_sync = {"AAA": {"count": 100, "buy_price": 95.0}}
tracked_sync = {"AAA": _stock()}
changes = m.sync_state_with_portfolio(
    state_sync, portfolio_sync, tracked_sync, {}, {}, True, TODAY
)
assert "AAA" in state_sync["positions"]
assert state_sync["positions"]["AAA"]["legacy"] is True
assert changes

portfolio_sync["AAA"]["count"] = 60
m.sync_state_with_portfolio(
    state_sync, portfolio_sync, tracked_sync, {}, {}, True, TODAY
)
assert state_sync["positions"]["AAA"]["sold"] == 40
assert state_sync["positions"]["AAA"]["stage"] == 1

portfolio_sync["AAA"]["count"] = 80
m.sync_state_with_portfolio(
    state_sync, portfolio_sync, tracked_sync, {}, {}, True, TODAY
)
assert state_sync["positions"]["AAA"]["original_count"] == 80
assert state_sync["positions"]["AAA"]["sold"] == 0

state_adopt = m.default_state()
portfolio_adopt = {"AAA": {"count": 100, "buy_price": 95.0}}
m.sync_state_with_portfolio(
    state_adopt, portfolio_adopt, tracked_sync,
    {"AAA": _idea()}, {}, True, TODAY
)
assert state_adopt["positions"]["AAA"]["legacy"] is False

# Неисполненная частичная продажа не должна выглядеть как докупка.
state_pending = m.default_state()
portfolio_pending = {"AAA": {"count": 100, "buy_price": 95.0}}
m.sync_state_with_portfolio(
    state_pending, portfolio_pending, tracked_sync, {}, {}, True, TODAY
)
pstate_pending = state_pending["positions"]["AAA"]
pstate_pending["sold"] = 40
pstate_pending["stage"] = 1
pstate_pending["pending_sell_shares"] = 40
m.sync_state_with_portfolio(
    state_pending, portfolio_pending, tracked_sync, {}, {}, True, TODAY
)
assert state_pending["positions"]["AAA"]["original_count"] == 100
assert state_pending["positions"]["AAA"]["sold"] == 40
assert state_pending["positions"]["AAA"]["pending_sell_shares"] == 40
portfolio_pending["AAA"]["count"] = 60
m.sync_state_with_portfolio(
    state_pending, portfolio_pending, tracked_sync, {}, {}, True, TODAY
)
assert state_pending["positions"]["AAA"]["pending_sell_shares"] == 0
assert state_pending["positions"]["AAA"]["sold"] == 40

# Секторный лимит должен видеть все позиции сектора, а не только кандидата.
portfolio_sector = {
    "AAA": {"count": 10, "buy_price": 100.0},
    "BBB": {"count": 20, "buy_price": 50.0},
}
tracked_sector = {
    "AAA": _stock(last=100.0),
    "BBB": _stock(ticker="BBB", last=50.0),
}
funds_sector = {"AAA": {"sector": "Банки"}, "BBB": {"sector": "Банки"}}
assert m.sector_exposure(portfolio_sector, tracked_sector, funds_sector, "Банки") == 2000.0

# ==============================================================================
# 9. ЗАГРУЗЧИКИ
# ==============================================================================

with tempfile.TemporaryDirectory() as tmp:
    pf_path = os.path.join(tmp, "portfolio.json")
    with open(pf_path, "w", encoding="utf-8") as f:
        json.dump({"AAA": {"count": 10, "buy_price": 100.0, "core_count": 5},
                   "CASH": 1000.0}, f)
    positions, cash, err = m.load_portfolio(pf_path)
    assert err is None and cash == 1000.0
    assert positions["AAA"] == {"count": 10, "buy_price": 100.0}
    assert "core_count" not in positions["AAA"]

    bad_path = os.path.join(tmp, "bad.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write("{bad json")
    _, _, bad_err = m.load_portfolio(bad_path)
    assert bad_err is not None

    ideas_path = os.path.join(tmp, "ideas.json")
    with open(ideas_path, "w", encoding="utf-8") as f:
        json.dump({"_meta": {"schema_version": 1}, "AAA": _idea()}, f)
    old_ideas_file = m.IDEAS_FILE
    try:
        m.IDEAS_FILE = ideas_path
        ideas, warnings, available = m.load_ideas()
        assert available is True and "AAA" in ideas
        assert m.active_idea_for("AAA", ideas, TODAY) is not None
        assert m.validate_idea("AAA", ideas["AAA"]) == []
        assert m.validate_idea("AAA", _idea(target=90.0))
    finally:
        m.IDEAS_FILE = old_ideas_file

    fund_old_path = os.path.join(tmp, "fundamentals_old.json")
    with open(fund_old_path, "w", encoding="utf-8") as f:
        json.dump({"AAA": {"name": "A", "sector": "Финтех / Банки",
                           "roe_pct": 20.0, "net_debt_ebitda": 0.0,
                           "fcf_positive": True, "moat": "x",
                           "status": "EXCELLENT", "red_flags": []}}, f)
    old_fund_file = m.FUNDAMENTALS_FILE
    try:
        m.FUNDAMENTALS_FILE = fund_old_path
        funds, _, available, stale = m.load_fundamentals()
        assert available is True
        assert funds["AAA"]["roe"] == 20.0
        assert funds["AAA"]["sector"] == "Банки"
        assert stale is None

        fund_new_path = os.path.join(tmp, "fundamentals_new.json")
        with open(fund_new_path, "w", encoding="utf-8") as f:
            json.dump({"_meta": {"schema_version": 2,
                                 "updated": TODAY.isoformat()},
                       "AAA": {"name": "A", "sector": "Банки", "pe": 5.0,
                               "pb": 1.0, "roe": 20.0, "fcf_positive": True}}, f)
        m.FUNDAMENTALS_FILE = fund_new_path
        _, _, available_new, stale_new = m.load_fundamentals()
        assert available_new is True and stale_new == 0
    finally:
        m.FUNDAMENTALS_FILE = old_fund_file

# ==============================================================================
# 10. СКРИНЕР
# ==============================================================================

screen_stocks = {}
screen_funds = {}
for i, ticker in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")):
    screen_stocks[ticker] = _stock(ticker=ticker, last=100.0 + i * 10)
    screen_funds[ticker] = {
        "name": ticker,
        "sector": "Банки" if i < 3 else "Нефть и газ",
        "listing_level": 1,
        "pe": 4.0 + i,
        "pb": 1.0 + i * 0.2,
        "ev_ebitda": None,
        "roe": 20.0 - i,
        "fcf_yield": 10.0 - i,
        "fcf_positive": True,
        "net_debt_ebitda": 0.5,
        "eps_growth": 8.0 + i,
        "revenue_growth": 5.0 + i,
        "div_yield": 5.0,
        "earnings_stability": 0.8,
    }

screen_map, screen_rows, screen_warnings = m.build_screen_map(
    screen_stocks, screen_funds, {"AAA": _idea()}, {"AAA": {"count": 10, "buy_price": 100.0}},
    m.default_state(), [100.0 + i for i in range(300)], TODAY
)
assert len(screen_rows) == 6
assert set(screen_map) == set(screen_stocks)
assert screen_map["AAA"]["target_r"] == 4.0
assert all(math.isfinite(float(r["score"])) for r in screen_rows)
assert all(r["verdict"] in ("BUY NOW", "WATCH", "SCREEN ONLY", "AVOID")
           for r in screen_rows)

# ==============================================================================
# 11. УВЕДОМЛЕНИЯ
# ==============================================================================

with tempfile.TemporaryDirectory() as tmp:
    notif_path = os.path.join(tmp, "notifications.json")
    old_notif = m.NOTIFICATIONS_FILE
    old_token = m.TELEGRAM_BOT_TOKEN
    try:
        m.NOTIFICATIONS_FILE = notif_path
        m.TELEGRAM_BOT_TOKEN = ""
        first = m.dispatch_notification("AAA_ENTRY", "ENTRY", "AAA", "<b>test</b>")
        second = m.dispatch_notification("AAA_ENTRY", "ENTRY", "AAA", "<b>test</b>")
        assert first is True and second is False
        notifs = m.load_notifications()
        assert len(notifs) == 1
        assert m.notification_exists("AAA_ENTRY", TODAY.isoformat(), notifs)
    finally:
        m.NOTIFICATIONS_FILE = old_notif
        m.TELEGRAM_BOT_TOKEN = old_token

# ==============================================================================
# 12. TRADE LOG
# ==============================================================================

state_trade = m.default_state()
pstate_trade = m.build_position_plan(
    "AAA", {"count": 100, "buy_price": 95.0}, _stock(), None, TODAY
)
assert pstate_trade is not None
state_trade["positions"]["AAA"] = pstate_trade
pstate_trade["realized_pnl"] = 500.0
m.apply_exit(state_trade, "AAA", pstate_trade, {
    "shares": 100, "net_profit": 500.0, "price": 110.0,
    "reason": "тест", "is_final": True, "stop_after": None,
}, TODAY)
assert len(state_trade["closed_trades"]) == 1
assert state_trade["closed_trades"][0]["ticker"] == "AAA"
assert state_trade["stats"]["wins"] == 1
assert state_trade["positions"]["AAA"]["exit_alerted"] is True

# ==============================================================================
# 13. МИГРАЦИЯ
# ==============================================================================

with tempfile.TemporaryDirectory() as tmp:
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        with open("portfolio.json", "w", encoding="utf-8") as f:
            json.dump({"AAA": {"count": 10, "buy_price": 100.0,
                               "core_count": 5}, "CASH": 1000.0}, f)
        with open("fundamentals.json", "w", encoding="utf-8") as f:
            json.dump({"AAA": {"name": "A", "sector": "Банки",
                               "roe_pct": 20.0, "net_debt_ebitda": 0.0,
                               "fcf_positive": True, "moat": "x",
                               "status": "EXCELLENT", "red_flags": []}}, f)
        with open("state.json", "w", encoding="utf-8") as f:
            json.dump({"positions": {}, "notified_buffett": []}, f)
        with open("ideas.example.json", "w", encoding="utf-8") as f:
            json.dump({"_meta": {"schema_version": 1}, "AAA": _idea()}, f)

        m.run_migration()

        with open("portfolio.json", "r", encoding="utf-8") as f:
            migrated_pf = json.load(f)
        assert "core_count" not in migrated_pf["AAA"]
        assert migrated_pf["CASH"] == 1000.0

        with open("fundamentals.json", "r", encoding="utf-8") as f:
            migrated_fund = json.load(f)
        assert migrated_fund["_meta"]["schema_version"] == 2
        assert migrated_fund["AAA"]["roe"] == 20.0
        assert "moat" not in migrated_fund["AAA"]

        with open("state.json", "r", encoding="utf-8") as f:
            migrated_state = json.load(f)
        assert migrated_state["schema_version"] == m.STATE_SCHEMA_VERSION
        assert os.path.exists("state.json.v3.bak")
        assert os.path.exists("portfolio.json.v3.bak")
        assert os.path.exists("ideas.json")
    finally:
        os.chdir(old_cwd)

# ==============================================================================
# 14. ГИГИЕНА ИСХОДНИКА
# ==============================================================================

with open("moex_tracker.py", "r", encoding="utf-8") as f:
    source = f.read()
assert "core_count" not in source.lower(), "Core logic must be fully removed"
assert "buffett" not in source.lower(), "Buffett logic must be fully removed"
assert "evaluate_buffett_health" not in source

print("All comprehensive v4 tests passed cleanly.")

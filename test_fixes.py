"""Self-checks for moex_tracker v6 sell-only (fixed % grid)."""
import json
import os
import tempfile
from datetime import datetime, timedelta

import moex_tracker as m

TODAY = datetime.now(m.MSK_TZ).date()


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
        "atr": atr,
        "atr_pct": (atr / last * 100.0) if last > 0 else 0.0,
        "rsi": 50.0,
        "sma50": last * 0.95,
        "ema50": last * 0.95,
        "highest_high": last,
    }
    data.update(kwargs)
    return data


def _pos(count=100, buy=95.0, target=None, stop=None):
    return {
        "count": count,
        "buy_price": buy,
        "target": target,
        "stop": stop,
    }


def _prices(values):
    return [round(float(v), 6) for v in values]


# ==============================================================================
# 1. ИНДИКАТОРЫ
# ==============================================================================

assert m._rsi([100.0] * 20, 14) == 50.0, "Flat RSI must be 50.0"
assert m._rsi([float(x) for x in range(1, 30)], 14) == 100.0
assert m._rsi([float(x) for x in range(30, 0, -1)], 14) == 0.0
assert m._sma([1, 2, 3, 4, 5], 3) == 4.0
assert m._ema([1, 2, 3, 4, 5], 3) > 3.0

candles = [[100.0, 100.0, 110.0, 90.0, 0.0, 0.0] for _ in range(20)]
assert abs(m._atr(candles, 14) - 20.0) < 1e-9

# ATR больше не участвует в планировании, но остаётся для дашборда.
assert m.TP1_PCT == 7.0
assert m.TP2_PCT == 14.0
assert m.TP3_PCT == 22.0
assert m.TP1_SELL_PCT == 0.30
assert m.TP2_SELL_PCT == 0.30
assert m.INITIAL_STOP_PCT == 6.0
assert m.STOP_AFTER_TP1_PCT == 0.5
assert m.STOP_AFTER_TP2_PCT == 8.0
assert m.TRAILING_FROM_PEAK_PCT == 5.0

# ==============================================================================
# 2. ИЗДЕРЖКИ
# ==============================================================================

be = m.breakeven_price(100.0)
assert be > 100.0
assert abs(m.net_proceeds(10, be, 100.0)["net_profit"]) < 0.01
loss = m.net_proceeds(10, 90.0, 100.0)
assert loss["tax"] == 0.0 and loss["net_profit"] < 0

# ==============================================================================
# 3. ПОРТФЕЛЬ
# ==============================================================================

with tempfile.TemporaryDirectory() as tmp:
    pf_path = os.path.join(tmp, "portfolio.json")
    with open(pf_path, "w", encoding="utf-8") as f:
        json.dump({
            "AAA": {"count": 10, "buy_price": 100.0, "target": 140.0,
                    "core_count": 5},
            "BBB": {"count": 20, "buy_price": 50.0},
            "CASH": 1000.0,
        }, f)
    positions, cash, err = m.load_portfolio(pf_path)
    assert err is None and cash == 1000.0
    assert positions["AAA"]["target"] == 140.0
    assert "core_count" not in positions["AAA"]
    assert positions["BBB"]["target"] is None

    bad_path = os.path.join(tmp, "bad.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write("{bad json")
    _, _, bad_err = m.load_portfolio(bad_path)
    assert bad_err is not None

# ==============================================================================
# 4. СЕТКА УРОВНЕЙ
# ==============================================================================

levels = m._build_pct_levels(100.0, 100, 1)
assert levels is not None and len(levels) == 3
assert _prices([l["price"] for l in levels]) == [107.0, 114.0, 122.0]
assert [l["cum_count"] for l in levels] == [30, 60, 100]
assert levels[-1]["is_final"] is True

# Округление до лотов: 14 акций -> 4 / 4 / 6.
levels14 = m._build_pct_levels(100.0, 14, 1)
assert [l["cum_count"] for l in levels14] == [4, 8, 14]

# Одна акция: продать 30% нельзя — остаётся только финальная ступень.
levels1 = m._build_pct_levels(100.0, 1, 1)
assert len(levels1) == 1
assert levels1[0]["name"] == "TP3"
assert levels1[0]["price"] == 122.0
assert levels1[0]["is_final"] is True

# Две акции: TP1 пропускается, TP2 продаёт один лот.
levels2 = m._build_pct_levels(100.0, 2, 1)
assert [l["name"] for l in levels2] == ["TP2", "TP3"]
assert [l["cum_count"] for l in levels2] == [1, 2]

# Лоты по 10: 100 акций -> 30 / 30 / 40.
levels_lot = m._build_pct_levels(100.0, 100, 10)
assert [l["cum_count"] for l in levels_lot] == [30, 60, 100]

# ==============================================================================
# 5. ПЛАНЫ
# ==============================================================================

plan = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0), _stock(last=100.0), TODAY
)
assert plan is not None
assert plan["plan_anchor"] == 100.0
assert plan["avg_cost"] == 100.0
assert plan["initial_stop"] == 94.0
assert _prices(plan["targets"]) == [107.0, 114.0, 122.0]
assert plan["risk_per_share"] == 6.0
assert plan["initial_risk_rub"] == 600.0
assert plan["logic_version"] == m.PLAN_LOGIC_VERSION
assert plan["peak"] == 100.0
assert plan["target"] is None

# Цена уже выше входа — пик сразу учитывается, но сетка от входа.
plan_above = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0), _stock(last=120.0), TODAY
)
assert plan_above is not None
assert plan_above["plan_anchor"] == 100.0
assert _prices(plan_above["targets"]) == [107.0, 114.0, 122.0]
assert plan_above["peak"] == 120.0

# target больше не меняет сетку.
plan_target = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0, target=140.0), _stock(), TODAY
)
assert plan_target is not None
assert _prices(plan_target["targets"]) == [107.0, 114.0, 122.0]
assert plan_target["target"] == 140.0

# Ручной стоп из portfolio.json важнее -6%.
plan_stop = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0, stop=95.0), _stock(), TODAY
)
assert plan_stop is not None and plan_stop["initial_stop"] == 95.0

# Некорректный ручной стоп (выше входа) игнорируется.
plan_bad_stop = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0, stop=101.0), _stock(), TODAY
)
assert plan_bad_stop is not None and plan_bad_stop["initial_stop"] == 94.0

# Для построения плана больше не нужен ATR.
plan_no_atr = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0), _stock(last=100.0, atr=0.0), TODAY
)
assert plan_no_atr is not None

# ==============================================================================
# 6. СТОПЫ
# ==============================================================================

pstate_stop = m.build_position_plan(
    "AAA", _pos(count=25, buy=100.0), _stock(), TODAY
)
assert pstate_stop is not None
state_stop = {"positions": {"AAA": pstate_stop}}
stop_plan = m.evaluate_stop(
    "AAA", {"count": 25, "buy_price": 100.0},
    {"last": 80.0, "bid": 79.0, "lot_size": 10, "atr": 5.0},
    state_stop,
)
assert stop_plan is not None
assert stop_plan["shares"] == 20, \
    f"Stop must be lot-aligned to 20, got {stop_plan['shares']}"
assert stop_plan["lots"] == 2
assert stop_plan["is_final"] is True
assert "-6%" in stop_plan["reason"]
assert abs(stop_plan["stop_pct"] + 6.0) < 1e-9

pstate_stop["exit_alerted"] = True
assert m.evaluate_stop(
    "AAA", {"count": 25, "buy_price": 100.0},
    {"last": 80.0, "bid": 79.0, "lot_size": 10, "atr": 5.0},
    state_stop,
) is None
pstate_stop["exit_alerted"] = False

# До +7% стоп не поджимается: откат 3-5% — это шум.
pstate_stop["stop_current"] = 94.0
pstate_stop["peak"] = 105.0
pstate_stop["stage"] = 0
pstate_stop["sold"] = 0
m.update_stops(state_stop, {"AAA": _stock(last=105.0, atr=5.0)})
assert pstate_stop["stop_current"] == 94.0, \
    "До TP1 стоп должен оставаться широким (-6%)"

# После +7% стоп переводится в безубыток +0.5%.
pstate_stop["peak"] = 108.0
pstate_stop["stop_current"] = 94.0
m.update_stops(state_stop, {"AAA": _stock(last=108.0, atr=5.0)})
assert abs(pstate_stop["stop_current"] - 100.5) < 1e-9, \
    f"После TP1 ожидали 100.5, получили {pstate_stop['stop_current']}"

# После +14% стоп не ниже +8%, плюс трейлинг 5% от пика.
pstate_stop["peak"] = 115.0
pstate_stop["stop_current"] = 100.5
m.update_stops(state_stop, {"AAA": _stock(last=115.0, atr=5.0)})
assert abs(pstate_stop["stop_current"] - 109.25) < 1e-9, \
    f"После TP2 ожидали 109.25, получили {pstate_stop['stop_current']}"

# К +22% трейлинг 5% даёт примерно +16%.
assert abs(m._pct_stop_floor(100.0, 122.0) - 115.9) < 1e-9

# Уже заработанный стоп никогда не понижается.
pstate_stop["stop_current"] = 120.0
pstate_stop["peak"] = 108.0
pstate_stop["sold"] = 5
m.update_stops(state_stop, {"AAA": _stock(last=108.0, atr=5.0)})
assert pstate_stop["stop_current"] == 120.0

# На стадии 0 старый узкий стоп расширяется до -6%.
pstate_stop["stop_current"] = 99.0
pstate_stop["peak"] = 105.0
pstate_stop["sold"] = 0
m.update_stops(state_stop, {"AAA": _stock(last=105.0, atr=5.0)})
assert pstate_stop["stop_current"] == 94.0

# Пользовательский стоп в сообщении не выдаётся за -6%.
pstate_stop["initial_stop"] = 95.0
pstate_stop["stop_current"] = 95.0
assert "portfolio.json" in m._stop_reason(pstate_stop, 95.0)

# ==============================================================================
# 7. ФИКСАЦИЯ ПРИБЫЛИ
# ==============================================================================

pstate_tp = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0), _stock(), TODAY
)
assert pstate_tp is not None
state_tp = {"positions": {"AAA": pstate_tp}}

# Тейк считается по bid: если last выше уровня, а bid ниже — не продаём.
assert m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 100.0},
    {"last": 107.1, "bid": 106.9, "lot_size": 1}, state_tp
) is None

tp1 = m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 100.0},
    {"last": 107.1, "bid": 107.1, "lot_size": 1}, state_tp
)
assert tp1 is not None
assert tp1["shares"] == 30 and tp1["level_name"] == "TP1"
assert abs(tp1["stop_after"] - 100.5) < 1e-9
assert abs(tp1["stop_after_pct"] - 0.5) < 1e-9

pstate_tp["sold"] = 30
pstate_tp["stage"] = 1
tp2 = m.evaluate_profit(
    "AAA", {"count": 70, "buy_price": 100.0},
    {"last": 114.1, "bid": 114.1, "lot_size": 1}, state_tp
)
assert tp2 is not None
assert tp2["shares"] == 30 and tp2["level_name"] == "TP2"
assert abs(tp2["stop_after"] - 114.1 * 0.95) < 1e-9
assert tp2["stop_after"] >= 108.0

pstate_tp["sold"] = 60
pstate_tp["stage"] = 2
tp3 = m.evaluate_profit(
    "AAA", {"count": 40, "buy_price": 100.0},
    {"last": 122.1, "bid": 122.1, "lot_size": 1}, state_tp
)
assert tp3 is not None and tp3["is_final"] is True and tp3["shares"] == 40
assert tp3["stop_after"] is None

# Гэп сразу к финальной цели закрывает всю позицию.
pstate_tp["sold"] = 0
pstate_tp["stage"] = 0
gap = m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 100.0},
    {"last": 125.0, "bid": 125.0, "lot_size": 1}, state_tp
)
assert gap is not None and gap["is_final"] is True and gap["shares"] == 100

# Гэп к +15% догоняет обе первые ступени: продаём 60%.
gap_mid = m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 100.0},
    {"last": 115.0, "bid": 115.0, "lot_size": 1}, state_tp
)
assert gap_mid is not None
assert gap_mid["level_name"] == "TP2"
assert gap_mid["shares"] == 60
assert abs(gap_mid["stop_after"] - 109.25) < 1e-9

# target больше не влияет: при +25% продаётся всё, а не только до target.
pstate_target_tp = m.build_position_plan(
    "AAA", _pos(count=100, buy=100.0, target=140.0), _stock(), TODAY
)
state_target_tp = {"positions": {"AAA": pstate_target_tp}}
above_target = m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 100.0},
    {"last": 125.0, "bid": 125.0, "lot_size": 1}, state_target_tp
)
assert above_target is not None and above_target["is_final"] is True
assert above_target["shares"] == 100

# Округление 14 акций: 4 / 4 / 6.
pstate14 = m.build_position_plan(
    "AAA", _pos(count=14, buy=100.0), _stock(), TODAY
)
state14 = {"positions": {"AAA": pstate14}}
tp1_14 = m.evaluate_profit(
    "AAA", {"count": 14, "buy_price": 100.0},
    {"last": 107.1, "bid": 107.1, "lot_size": 1}, state14
)
assert tp1_14 is not None and tp1_14["shares"] == 4
pstate14["sold"] = 4
pstate14["stage"] = 1
tp2_14 = m.evaluate_profit(
    "AAA", {"count": 10, "buy_price": 100.0},
    {"last": 114.1, "bid": 114.1, "lot_size": 1}, state14
)
assert tp2_14 is not None and tp2_14["shares"] == 4
pstate14["sold"] = 8
pstate14["stage"] = 2
tp3_14 = m.evaluate_profit(
    "AAA", {"count": 6, "buy_price": 100.0},
    {"last": 122.1, "bid": 122.1, "lot_size": 1}, state14
)
assert tp3_14 is not None and tp3_14["is_final"] is True
assert tp3_14["shares"] == 6

# Одна акция: промежуточных продаж нет, но стоп всё равно подтягивается.
pstate1 = m.build_position_plan(
    "AAA", _pos(count=1, buy=100.0), _stock(), TODAY
)
state1 = {"positions": {"AAA": pstate1}}
assert m.evaluate_profit(
    "AAA", {"count": 1, "buy_price": 100.0},
    {"last": 107.1, "bid": 107.1, "lot_size": 1}, state1
) is None
pstate1["peak"] = 107.1
m.update_stops(state1, {"AAA": _stock(last=107.1)})
assert abs(pstate1["stop_current"] - 100.5) < 1e-9
tp3_1 = m.evaluate_profit(
    "AAA", {"count": 1, "buy_price": 100.0},
    {"last": 122.1, "bid": 122.1, "lot_size": 1}, state1
)
assert tp3_1 is not None and tp3_1["is_final"] is True
assert tp3_1["shares"] == 1

# ==============================================================================
# 8. TIME EXIT
# ==============================================================================

pstate_time = m.build_position_plan("AAA", _pos(), _stock(), TODAY)
pstate_time["opened_at"] = (TODAY - timedelta(days=181)).isoformat()
state_time = {"positions": {"AAA": pstate_time}}
time_plan = m.evaluate_time(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 100.0, "bid": 99.0, "lot_size": 1}, state_time, TODAY
)
assert time_plan is not None and time_plan["is_final"] is True

# При 121 дне (ранее лимит 120) теперь позиция еще жива:
pstate_time["opened_at"] = (TODAY - timedelta(days=121)).isoformat()
assert m.evaluate_time(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 100.0, "bid": 99.0, "lot_size": 1}, state_time, TODAY
) is None

pstate_time["opened_at"] = TODAY.isoformat()
assert m.evaluate_time(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 100.0, "bid": 99.0, "lot_size": 1}, state_time, TODAY
) is None

# ==============================================================================
# 9. СИНХРОНИЗАЦИЯ
# ==============================================================================

state_sync = m.default_state()
portfolio_sync = {"AAA": _pos(buy=100.0, target=None)}
tracked_sync = {"AAA": _stock()}
changes = m.sync_state_with_portfolio(
    state_sync, portfolio_sync, tracked_sync, TODAY
)
assert "AAA" in state_sync["positions"] and changes
assert state_sync["positions"]["AAA"]["plan_anchor"] == 100.0
assert _prices(state_sync["positions"]["AAA"]["targets"]) == [107.0, 114.0, 122.0]
assert state_sync["positions"]["AAA"]["logic_version"] == m.PLAN_LOGIC_VERSION

portfolio_sync["AAA"]["count"] = 60
m.sync_state_with_portfolio(state_sync, portfolio_sync, tracked_sync, TODAY)
assert state_sync["positions"]["AAA"]["sold"] == 40
assert state_sync["positions"]["AAA"]["stage"] == 1

portfolio_sync["AAA"]["count"] = 80
m.sync_state_with_portfolio(state_sync, portfolio_sync, tracked_sync, TODAY)
assert state_sync["positions"]["AAA"]["original_count"] == 80
assert state_sync["positions"]["AAA"]["sold"] == 0

# Неисполненная частичная продажа не должна выглядеть как докупка.
state_pending = m.default_state()
portfolio_pending = {"AAA": _pos(buy=100.0, target=None)}
m.sync_state_with_portfolio(
    state_pending, portfolio_pending, tracked_sync, TODAY
)
pstate_pending = state_pending["positions"]["AAA"]
pstate_pending["sold"] = 40
pstate_pending["stage"] = 1
pstate_pending["pending_sell_shares"] = 40
m.sync_state_with_portfolio(
    state_pending, portfolio_pending, tracked_sync, TODAY
)
assert state_pending["positions"]["AAA"]["original_count"] == 100
assert state_pending["positions"]["AAA"]["sold"] == 40
assert state_pending["positions"]["AAA"]["pending_sell_shares"] == 40
portfolio_pending["AAA"]["count"] = 60
m.sync_state_with_portfolio(
    state_pending, portfolio_pending, tracked_sync, TODAY
)
assert state_pending["positions"]["AAA"]["pending_sell_shares"] == 0
assert state_pending["positions"]["AAA"]["sold"] == 40

# target обновляется справочно, но сетка остаётся фиксированной.
state_target = m.default_state()
portfolio_target = {"AAA": _pos(buy=100.0, target=None)}
m.sync_state_with_portfolio(
    state_target, portfolio_target, tracked_sync, TODAY
)
portfolio_target["AAA"]["target"] = 140.0
m.sync_state_with_portfolio(
    state_target, portfolio_target, tracked_sync, TODAY
)
assert state_target["positions"]["AAA"]["target"] == 140.0
assert _prices(state_target["positions"]["AAA"]["targets"]) == [107.0, 114.0, 122.0]


def _old_atr_plan(**overrides):
    data = {
        "avg_cost": 100.0,
        "plan_anchor": 95.0,
        "entry_atr": 5.0,
        "current_atr": 5.0,
        "initial_stop": 85.0,
        "stop_current": 85.0,
        "risk_per_share": 10.0,
        "initial_risk_rub": 500.0,
        "levels": [
            {"name": "TP1", "price": 105.0, "cum_pct": 0.4,
             "cum_count": 20, "is_final": False},
            {"name": "TP2", "price": 115.0, "cum_pct": 0.7,
             "cum_count": 35, "is_final": False},
            {"name": "TP3", "price": 130.0, "cum_pct": 1.0,
             "cum_count": 50, "is_final": True},
        ],
        "targets": [105.0, 115.0, 130.0],
        "target": None,
        "stage": 0,
        "sold": 0,
        "original_count": 50,
        "peak": 96.0,
        "opened_at": TODAY.isoformat(),
        "exit_alerted": False,
        "archived": False,
        "pending_sell_shares": 0,
        "realized_pnl": 0.0,
        "realized_r": 0.0,
        "closed_at": None,
    }
    data.update(overrides)
    return data


# Старый ATR-план без прогресса переводится на -6% и новую сетку.
state_old = m.default_state()
state_old["positions"]["AAA"] = _old_atr_plan()
m.sync_state_with_portfolio(
    state_old, {"AAA": _pos(count=50, buy=100.0)}, tracked_sync, TODAY
)
old = state_old["positions"]["AAA"]
assert old["logic_version"] == m.PLAN_LOGIC_VERSION
assert old["plan_anchor"] == 100.0
assert _prices(old["targets"]) == [107.0, 114.0, 122.0]
assert old["initial_stop"] == 94.0
assert old["stop_current"] == 94.0

# Старый план с уже зафиксированной прибылью не понижает стоп.
state_old2 = m.default_state()
state_old2["positions"]["AAA"] = _old_atr_plan(
    sold=20, stage=1, peak=108.0, stop_current=99.0
)
m.sync_state_with_portfolio(
    state_old2, {"AAA": _pos(count=30, buy=100.0)}, tracked_sync, TODAY
)
old2 = state_old2["positions"]["AAA"]
assert old2["logic_version"] == m.PLAN_LOGIC_VERSION
assert old2["stage"] == 1
assert abs(old2["stop_current"] - 100.5) < 1e-9

# Даже при откате пика старый высокий стоп сохраняется.
state_old3 = m.default_state()
state_old3["positions"]["AAA"] = _old_atr_plan(
    sold=20, stage=1, peak=108.0, stop_current=120.0
)
m.sync_state_with_portfolio(
    state_old3, {"AAA": _pos(count=30, buy=100.0)}, tracked_sync, TODAY
)
assert state_old3["positions"]["AAA"]["stop_current"] == 120.0

# ==============================================================================
# 10. TRADE LOG
# ==============================================================================

state_trade = m.default_state()
pstate_trade = m.build_position_plan("AAA", _pos(), _stock(), TODAY)
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
# 11. МИГРАЦИЯ
# ==============================================================================

with tempfile.TemporaryDirectory() as tmp:
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        with open("portfolio.json", "w", encoding="utf-8") as f:
            json.dump({
                "AAA": {"count": 10, "buy_price": 100.0,
                        "target": 140.0, "core_count": 5},
                "CASH": 1000.0,
            }, f)
        with open("state.json", "w", encoding="utf-8") as f:
            json.dump({"schema_version": 4, "positions": {}}, f)

        m.run_migration()

        with open("portfolio.json", "r", encoding="utf-8") as f:
            migrated = json.load(f)
        assert "core_count" not in migrated["AAA"]
        assert migrated["AAA"]["target"] == 140.0
        assert migrated["CASH"] == 1000.0

        with open("state.json", "r", encoding="utf-8") as f:
            migrated_state = json.load(f)
        assert migrated_state["schema_version"] == m.STATE_SCHEMA_VERSION
        assert os.path.exists("portfolio.json.v4.bak")
        assert os.path.exists("state.json.v4.bak")
    finally:
        os.chdir(old_cwd)

# ==============================================================================
# 12. НАЗВАНИЯ КОМПАНИЙ И ФОРМАТИРОВАНИЕ
# ==============================================================================

assert m._display_ticker("SBER", "Сбербанк") == "SBER (Сбербанк)"
assert m._display_ticker("SBER", "SBER") == "SBER"
assert m._display_ticker("SBER", None) == "SBER"
assert m._display_ticker("SBER", "") == "SBER"

stop_msg = m.format_stop_message({
    "ticker": "PRMD", "name": "Промомед", "price": 400.0, "stop": 396.16,
    "stop_pct": -6.0, "reason": "начальный стоп -6%", "avg_cost": 421.45,
    "gain_pct": -5.09, "shares": 6, "lots": 6, "net_profit": -120.0,
    "lot_warning": False,
})
assert "PRMD (Промомед)" in stop_msg
assert "-6.0% от входа" in stop_msg

profit_msg = m.format_profit_message({
    "ticker": "PRMD", "name": "Промомед", "level_name": "TP1",
    "price": 451.0, "target_price": 451.0, "avg_cost": 421.45,
    "gain_pct": 7.01, "shares": 5, "lots": 5, "sell_ratio": 29.4,
    "gross": 2255.0, "commission": 5.0, "tax": 10.0,
    "net_profit": 63.0, "is_final": False, "remaining_after": 12,
    "stop_after": 423.56, "stop_after_pct": 0.5, "lot_warning": False,
})
assert "PRMD (Промомед)" in profit_msg
assert "+0.5% от входа" in profit_msg

# Старый формат без процентных полей тоже не должен падать.
stop_msg_old = m.format_stop_message({
    "ticker": "AAA", "price": 90.0, "stop": 94.0, "reason": "стоп",
    "avg_cost": 100.0, "gain_pct": -10.0, "shares": 1, "lots": 1,
    "net_profit": -10.0, "lot_warning": False,
})
assert "AAA" in stop_msg_old

time_msg = m.format_time_message({
    "ticker": "PRMD", "name": "Промомед", "reason": "истёк горизонт",
    "price": 430.0, "avg_cost": 421.0, "gain_pct": 2.14,
    "shares": 6, "lots": 6, "net_profit": 50.0, "lot_warning": False,
})
assert "PRMD (Промомед)" in time_msg

# ==============================================================================
# 13. ПОВТОРНАЯ ОТПРАВКА PENDING УВЕДОМЛЕНИЙ
# ==============================================================================

with tempfile.TemporaryDirectory() as tmp:
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        yesterday = (datetime.now(m.MSK_TZ).date() - timedelta(days=2)).strftime("%Y-%m-%d")
        initial_notifs = [
            {
                "key": "OLD_TP1",
                "date": yesterday,
                "time": "12:00:00",
                "timestamp": f"{yesterday}T12:00:00+03:00",
                "kind": "PROFIT",
                "ticker": "AAA",
                "name": "ААА Компани",
                "message": "тестовое сообщение",
                "telegram_sent": False,
            }
        ]
        with open("notifications.json", "w", encoding="utf-8") as f:
            json.dump(initial_notifs, f)

        orig_send = m.send_telegram_message
        orig_is_test = m.is_test_mode
        sent_messages = []
        try:
            m.is_test_mode = lambda: False
            m.send_telegram_message = lambda msg: (sent_messages.append(msg) or True)

            m.try_send_pending_silent()

            assert len(sent_messages) == 1
            with open("notifications.json", "r", encoding="utf-8") as f:
                saved = json.load(f)
            assert saved[0]["telegram_sent"] is True

            sent_messages.clear()
            m.try_send_pending_silent()
            assert len(sent_messages) == 0
        finally:
            m.send_telegram_message = orig_send
            m.is_test_mode = orig_is_test
    finally:
        os.chdir(old_cwd)

# ==============================================================================
# 14. ГИГИЕНА
# ==============================================================================

with open("moex_tracker.py", "r", encoding="utf-8") as f:
    source = f.read()
assert "_build_atr_levels" not in source
assert "_build_target_levels" not in source
assert "ATR_TP_MULTS" not in source
assert "INITIAL_STOP_ATR" not in source
assert "TRAILING_ATR_MULT" not in source
assert "_build_pct_levels" in source
assert "PLAN_LOGIC_VERSION" in source
assert "build_screen_map" not in source
assert "load_fundamentals" not in source
assert "IDEAS_FILE" not in source
assert "FUNDAMENTALS_FILE" not in source
assert "core_count" not in source.lower()
assert "buffett" not in source.lower()

print("All sell-only v6 tests passed cleanly.")

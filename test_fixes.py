"""Self-checks for moex_tracker v5 sell-only."""
import json
import math
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
# 4. УРОВНИ
# ==============================================================================

no_target = m._build_atr_levels(100.0, 5.0, 100, 1)
assert no_target is not None and len(no_target) == 3
assert [l["price"] for l in no_target] == [110.0, 120.0, 130.0]
assert [l["cum_count"] for l in no_target] == [40, 70, 100]
assert no_target[-1]["is_final"] is True

small = m._build_atr_levels(100.0, 5.0, 1, 1)
assert small is not None and len(small) == 1 and small[0]["is_final"]

target3 = m._build_target_levels(100.0, 90.0, 140.0, 100, 1)
assert target3 is not None and len(target3) == 3
assert [l["price"] for l in target3] == [110.0, 120.0, 140.0]
assert [l["cum_count"] for l in target3] == [40, 70, 100]

target2 = m._build_target_levels(100.0, 90.0, 120.0, 100, 1)
assert target2 is not None and len(target2) == 2
assert [l["cum_count"] for l in target2] == [50, 100]

target1 = m._build_target_levels(100.0, 90.0, 105.0, 100, 1)
assert target1 is not None and len(target1) == 1
assert target1[0]["price"] == 105.0 and target1[0]["is_final"]

target_below = m._build_target_levels(100.0, 90.0, 95.0, 100, 1)
assert target_below is not None and len(target_below) == 1
assert target_below[0]["price"] == 95.0

# ==============================================================================
# 5. ПЛАНЫ
# ==============================================================================

plan_atr = m.build_position_plan("AAA", _pos(target=None), _stock(), TODAY)
assert plan_atr is not None
assert plan_atr["initial_stop"] == 85.0
assert plan_atr["targets"] == [110.0, 120.0, 130.0]
assert plan_atr["target"] is None

plan_target = m.build_position_plan(
    "AAA", _pos(target=140.0, stop=90.0), _stock(), TODAY
)
assert plan_target is not None
assert plan_target["initial_stop"] == 90.0
assert plan_target["targets"] == [110.0, 120.0, 140.0]
assert plan_target["target"] == 140.0

# ==============================================================================
# 6. СТОП
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

pstate_stop["exit_alerted"] = True
assert m.evaluate_stop(
    "AAA", {"count": 25, "buy_price": 100.0},
    {"last": 80.0, "bid": 79.0, "lot_size": 10, "atr": 5.0},
    state_stop,
) is None
pstate_stop["exit_alerted"] = False

# Проверка поджима стопа:
# На стадии 0 стоп НЕ поджимается при peak = anchor + 0.8R (дает свободу колебаниям)
pstate_stop["stop_current"] = pstate_stop["initial_stop"]
pstate_stop["peak"] = 108.0
pstate_stop["stage"] = 0
m.update_stops(state_stop, {"AAA": _stock(last=105.0, atr=5.0)})
assert pstate_stop["stop_current"] == pstate_stop["initial_stop"], "На стадии 0 стоп не должен поджиматься до TP1"

# На стадии 1 (после TP1) стоп переносится в безубыток
pstate_stop["stage"] = 1
m.update_stops(state_stop, {"AAA": _stock(last=115.0, atr=5.0)})
assert pstate_stop["stop_current"] >= 100.0, "На стадии 1 стоп должен быть не ниже безубытка"

# На стадии 2 (после TP2) стоп переносится на TP1 и трейлит пик
pstate_stop["stage"] = 2
pstate_stop["peak"] = 130.0
m.update_stops(state_stop, {"AAA": _stock(last=130.0, atr=5.0)})
assert pstate_stop["stop_current"] >= 110.0, "На стадии 2 стоп должен быть не ниже уровня TP1"
# Чендлер: 130 - 3*5 = 115.0 >= 110.0
assert pstate_stop["stop_current"] >= 115.0, "На стадии 2 трейлинг Chandelier защищает остаток"

# ==============================================================================
# 7. ФИКСАЦИЯ ПРИБЫЛИ
# ==============================================================================

pstate_tp = m.build_position_plan(
    "AAA", _pos(target=140.0, stop=90.0), _stock(), TODAY
)
state_tp = {"positions": {"AAA": pstate_tp}}

tp1 = m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 111.0, "bid": 111.0, "lot_size": 1}, state_tp
)
assert tp1 is not None
assert tp1["shares"] == 40 and tp1["level_name"] == "TP1"
assert tp1["stop_after"] >= 100.0

pstate_tp["sold"] = 40
pstate_tp["stage"] = 1
tp2 = m.evaluate_profit(
    "AAA", {"count": 60, "buy_price": 95.0},
    {"last": 121.0, "bid": 121.0, "lot_size": 1}, state_tp
)
assert tp2 is not None
assert tp2["shares"] == 30 and tp2["level_name"] == "TP2"
assert tp2["stop_after"] >= 110.0

pstate_tp["sold"] = 70
pstate_tp["stage"] = 2
tp3 = m.evaluate_profit(
    "AAA", {"count": 30, "buy_price": 95.0},
    {"last": 141.0, "bid": 141.0, "lot_size": 1}, state_tp
)
assert tp3 is not None and tp3["is_final"] is True and tp3["shares"] == 30

pstate_tp["sold"] = 0
pstate_tp["stage"] = 0
gap = m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 141.0, "bid": 141.0, "lot_size": 1}, state_tp
)
assert gap is not None and gap["is_final"] is True and gap["shares"] == 100

plan_target_below = m.build_position_plan(
    "AAA", _pos(target=95.0, stop=90.0), _stock(), TODAY
)
state_below = {"positions": {"AAA": plan_target_below}}
below = m.evaluate_profit(
    "AAA", {"count": 100, "buy_price": 95.0},
    {"last": 96.0, "bid": 96.0, "lot_size": 1}, state_below
)
assert below is not None and below["is_final"] is True

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
portfolio_sync = {"AAA": _pos(target=None)}
tracked_sync = {"AAA": _stock()}
changes = m.sync_state_with_portfolio(
    state_sync, portfolio_sync, tracked_sync, TODAY
)
assert "AAA" in state_sync["positions"] and changes

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
portfolio_pending = {"AAA": _pos(target=None)}
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

# Изменение target пересчитывает уровни.
state_target = m.default_state()
portfolio_target = {"AAA": _pos(target=None)}
m.sync_state_with_portfolio(
    state_target, portfolio_target, tracked_sync, TODAY
)
portfolio_target["AAA"]["target"] = 140.0
m.sync_state_with_portfolio(
    state_target, portfolio_target, tracked_sync, TODAY
)
assert state_target["positions"]["AAA"]["target"] == 140.0
assert 140.0 in state_target["positions"]["AAA"]["targets"]

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
# 12. ГИГИЕНА
# ==============================================================================

with open("moex_tracker.py", "r", encoding="utf-8") as f:
    source = f.read()
assert "build_screen_map" not in source
assert "load_fundamentals" not in source
assert "IDEAS_FILE" not in source
assert "FUNDAMENTALS_FILE" not in source
assert "core_count" not in source.lower()
assert "buffett" not in source.lower()

print("All sell-only v5 tests passed cleanly.")

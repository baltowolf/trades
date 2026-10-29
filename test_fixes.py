"""Comprehensive self-checks for moex_tracker.py."""
import moex_tracker

# 1. RSI self-check (Wilder's smoothing)
flat = [100.0] * 20
assert moex_tracker._rsi(flat, 14) == 50.0, "Flat RSI must be 50.0"

monotone_up = [float(x) for x in range(1, 30)]
rsi_up = moex_tracker._rsi(monotone_up, 14)
assert rsi_up == 100.0, f"Monotone up RSI must be 100.0, got {rsi_up}"

monotone_down = [float(x) for x in range(30, 0, -1)]
rsi_down = moex_tracker._rsi(monotone_down, 14)
assert rsi_down == 0.0, f"Monotone down RSI must be 0.0, got {rsi_down}"

# 2. Risk/Reward parameters
assert moex_tracker.TAKE_PROFIT_ATR_LEVELS[0]["atr_mult"] == 2.5
assert moex_tracker.INITIAL_STOP_ATR == 2.5
assert moex_tracker.PRE_TP1_PROTECT_ATR == 2.0

# 3. Environment & Telegram
assert moex_tracker.TELEGRAM_BOT_TOKEN != ""
assert moex_tracker.TELEGRAM_CHAT_ID != ""
assert not moex_tracker.is_test_mode()

# 4. Buffett health check
fund_map = moex_tracker.load_fundamentals()
assert "HEAD" in fund_map, "HEAD must be in fundamentals.json"
assert "SBER" in fund_map, "SBER must be in fundamentals.json"

status_head, reasons_head = moex_tracker.evaluate_buffett_health("HEAD", {}, {}, fund_map)
assert status_head == "GOOD", f"HEAD should be GOOD, got {status_head}"

# 5. Buy Signal Logic & Reload
stock_dip = {
    "ticker": "SBER",
    "last": 290.0,
    "change_pct": -2.2,
    "z_score": -1.4,
    "rsi": 32.0,
    "trend_up": True,
    "above_sma200": True,
    "atr": 4.5,
    "lot_size": 1
}
sig = moex_tracker.evaluate_buy_signal(stock_dip)
assert sig is not None, "Oversold pullback in trend must trigger buy signal"

# 6. Portfolio and Core check
portfolio, cash = moex_tracker.load_portfolio("portfolio.json")
assert "HEAD" in portfolio
assert "MOEX" in portfolio
assert portfolio["MOEX"]["core_count"] == 20
assert portfolio["HEAD"]["core_count"] == 1

# 7. Buffett message dynamic alternatives check (must not include self)
msg_chmf = moex_tracker.format_buffett_warning_message("CHMF", "BAD", ["Тест"], fund_map)
assert "CHMF" not in msg_chmf.split("перекладку в первоклассные компании с чистым балансом (")[1]

# 8. Small satellite Take-Profit check
state_test = {
    "positions": {
        "TEST": {
            "entry_atr": 10.0,
            "entry_price": 100.0,
            "buy_price": 100.0,
            "original_count": 2,
            "core_count": 1,
            "targets": [125.0, 140.0, 160.0],
            "initial_stop": 75.0,
            "tp1_hit": False,
            "sold": 0,
            "peak": 100.0,
            "is_gold": False,
        }
    }
}
# Satellite is 1 share. At TP1 (125.0), target_sold is 0 (holds for bigger run)
res_tp1 = moex_tracker.evaluate_profit_taking("TEST", {"count": 2, "buy_price": 100.0}, {"last": 126.0, "bid": 126.0, "lot_size": 1}, state_test)
assert res_tp1 is None, "1-share satellite should hold through TP1"
# At TP2 (140.0), target_sold rounds to 1 (sells 1 share)
res_tp2 = moex_tracker.evaluate_profit_taking("TEST", {"count": 2, "buy_price": 100.0}, {"last": 141.0, "bid": 141.0, "lot_size": 1}, state_test)
assert res_tp2 is not None and res_tp2["shares"] == 1, "1-share satellite must trigger sell at TP2"

# 9. Stop-loss lot alignment check
stock_lot10 = {"last": 70.0, "lot_size": 10}
state_stop = {
    "positions": {
        "TEST10": {
            "entry_atr": 10.0,
            "buy_price": 100.0,
            "core_count": 0,
            "initial_stop": 75.0,
            "peak": 100.0,
        }
    }
}
# Position has 25 shares (2.5 lots), lot size is 10. Tradable stop must be 20 shares (2 lots), never 25!
stop_plan = moex_tracker.evaluate_stop_loss("TEST10", {"count": 25, "buy_price": 100.0}, stock_lot10, state_stop)
assert stop_plan is not None and stop_plan["shares"] == 20, f"Stop shares must be lot-aligned to 20, got {stop_plan['shares']}"
assert stop_plan["lots"] == 2

# 10. State schema includes notified_buffett
st_default = moex_tracker.load_state()
assert "notified_buffett" in st_default

print("All comprehensive tests passed cleanly.")

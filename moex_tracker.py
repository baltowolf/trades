"""
MOEX Stock Tracker v5.0 — sell-only трекер.

Вход:
    portfolio.json:
        {
          "SBER": {"count": 14, "buy_price": 285.99, "target": 380.0},
          "LKOH": {"count": 1, "buy_price": 6065.5},
          "CASH": 10448
        }

    target (необязательно) — ожидаемая прогнозная цена.
    Если target нет, цели строятся по ATR.

Выход:
    Telegram-уведомления только о продаже:
      • стоп-лосс / защита прибыли;
      • TP1 / TP2 / цель;
      • истечение горизонта.

Формулы:
    R = plan_anchor - initial_stop
    Стоп: plan_anchor - min(3 ATR, 18% цены)
    Без target: TP на 2 / 4 / 6 ATR, доли 40% / 30% / остаток.
    С target: TP1 на 2 ATR (40%), TP2 на 4 ATR (30%),
              финал — target; последняя ступень продаёт всё.
    После TP1 стоп в безубыток с учётом комиссии и НДФЛ.
    После TP2 стоп на уровень TP1.
    Chandelier: peak - 3 ATR (для остатка).
    Горизонт: 180 дней от старта плана.

Команды:
    python moex_tracker.py                 — основной цикл
    python moex_tracker.py --once          — одна диагностическая итерация
    python moex_tracker.py --show          — последние уведомления
    python moex_tracker.py --send-pending  — дослать неотправленное в Telegram
    python moex_tracker.py --stats         — статистика сделок
    python moex_tracker.py --migrate       — backup и сброс state
    python moex_tracker.py --reset-plan TICKER — пересчитать план от текущей цены

fundamentals.json и ideas.json удалены из проекта.
state.json требует schema_version: 4.
"""

import html
import json
import math
import os
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ------------------------------------------------------------------------------
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ==============================================================================
# КОНФИГУРАЦИЯ
# ==============================================================================

VERSION = "5.1.0"
STATE_SCHEMA_VERSION = 4

if os.path.exists(".env"):
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PORTFOLIO_FILE = "portfolio.json"
STATE_FILE = "state.json"
NOTIFICATIONS_FILE = "notifications.json"

MAX_NOTIFICATIONS_KEPT = 500
RECENT_NOTIFICATIONS_SHOWN = 10
MAX_TRADE_LOG = 500

# --- Стопы и цели ---
INITIAL_STOP_ATR = 3.0
MAX_STOP_DISTANCE_PCT = 18.0
TRAILING_ATR_MULT = 3.0

PRE_TP1_TRIGGER_R = 0.75
PRE_TP1_LOCK_R = 0.25
TP1_R = 1.0
TP2_R = 2.0
TP1_SELL_PCT_3 = 0.40
TP2_SELL_PCT_3 = 0.30
TP1_SELL_PCT_2 = 0.50

# Без прогнозной цели: TP на 2 / 4 / 6 ATR.
ATR_TP_MULTS = (2.0, 4.0, 6.0)
ATR_TP_PCTS = (0.40, 0.70, 1.00)

MAX_HOLDING_DAYS = 180

# --- Издержки ---
COMMISSION_PCT = 0.1
TAX_PCT = 13.0
INCLUDE_TAX = True

# --- Данные ---
CANDLES_DAYS = 150
CANDLES_CACHE_TTL = 1800
ATR_WINDOW_DAYS = 14
RSI_WINDOW_DAYS = 14
TREND_EMA_PERIOD = 50
TRAILING_LOOKBACK_DAYS = 22

# --- Технические ---
CHECK_INTERVAL_SECONDS = 600
RETRY_DELAY_SECONDS = 30
MOEX_CONNECT_TIMEOUT = 10
MOEX_READ_TIMEOUT = 25
MOEX_MAX_RETRIES = 3

MSK_TZ = timezone(timedelta(hours=3))
SESSION_START = dtime(10, 0)
SESSION_END = dtime(23, 50)

_candles_cache: Dict[str, Tuple[float, list]] = {}


# ==============================================================================
# 1. АТОМАРНЫЙ I/O
# ==============================================================================

def atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_json_file(path: str) -> Tuple[Any, Optional[str]]:
    if not os.path.exists(path):
        return None, f"файл {path} не найден"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"не удалось прочитать {path}: {e}"


def _parse_iso_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ==============================================================================
# 2. СОСТОЯНИЕ
# ==============================================================================

def default_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "positions": {},
        "closed_trades": [],
        "stats": {"wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0},
        "notified_date": "",
        "notified_profit": [],
        "notified_stop": [],
    }


def load_state() -> Tuple[dict, Optional[str]]:
    if not os.path.exists(STATE_FILE):
        return default_state(), None
    raw, err = _load_json_file(STATE_FILE)
    if err:
        return default_state(), err
    if not isinstance(raw, dict):
        return default_state(), "state.json: корневой элемент не объект"
    if raw.get("schema_version") != STATE_SCHEMA_VERSION:
        return default_state(), (
            "state.json: схема не v4. Выполните `python moex_tracker.py --migrate`"
        )
    state = default_state()
    for key, value in raw.items():
        state[key] = value
    if not isinstance(state.get("positions"), dict):
        return default_state(), "state.json: positions повреждён"
    if not isinstance(state.get("stats"), dict):
        state["stats"] = {"wins": 0, "losses": 0,
                          "gross_profit": 0.0, "gross_loss": 0.0}
    if not isinstance(state.get("closed_trades"), list):
        state["closed_trades"] = []
    for key in ("notified_profit", "notified_stop"):
        if not isinstance(state.get(key), list):
            state[key] = []
    return state, None


def save_state(state: dict) -> None:
    try:
        atomic_write_json(STATE_FILE, state)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось сохранить состояние: {e}")
        raise


# ==============================================================================
# 3. УВЕДОМЛЕНИЯ
# ==============================================================================

def load_notifications() -> list:
    raw, err = _load_json_file(NOTIFICATIONS_FILE)
    if err:
        if os.path.exists(NOTIFICATIONS_FILE):
            print(f"[ПРЕДУПРЕЖДЕНИЕ] {err}")
        return []
    return raw if isinstance(raw, list) else []


def save_notifications(notifs: list) -> None:
    try:
        atomic_write_json(NOTIFICATIONS_FILE, notifs)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось сохранить {NOTIFICATIONS_FILE}: {e}")


def _strip_html(text: str) -> str:
    return (text.replace("<b>", "").replace("</b>", "")
            .replace("<i>", "").replace("</i>", ""))


def is_test_mode() -> bool:
    return (not TELEGRAM_BOT_TOKEN
            or TELEGRAM_BOT_TOKEN == "ВАШ_ТОКЕН_БОТА")


def notification_exists(key: str, date_str: str,
                        notifs: Optional[list] = None) -> bool:
    if notifs is None:
        notifs = load_notifications()
    return any(n.get("key") == key and n.get("date") == date_str for n in notifs)


def send_telegram_message(html_text: str) -> bool:
    if is_test_mode():
        print(f"\n[TELEGRAM ТЕСТ — токен не задан]\n{_strip_html(html_text)}\n"
              + "-" * 60)
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": html_text,
               "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        err_msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            err_msg += f" | {e.response.text}"
        print(f"[ОШИБКА TELEGRAM] {err_msg}")
        return False


def dispatch_notification(key: str, kind: str, ticker: str,
                          message: str) -> bool:
    ts = datetime.now(MSK_TZ)
    date_str = ts.strftime("%Y-%m-%d")
    notifs = load_notifications()
    if notification_exists(key, date_str, notifs):
        return False

    telegram_ok = send_telegram_message(message)
    really_sent = telegram_ok and not is_test_mode()
    notifs.append({
        "key": key,
        "date": date_str,
        "time": ts.strftime("%H:%M:%S"),
        "timestamp": ts.isoformat(),
        "kind": kind,
        "ticker": ticker,
        "message": message,
        "telegram_sent": really_sent,
    })
    notifs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    save_notifications(notifs[:MAX_NOTIFICATIONS_KEPT])

    if not really_sent and not is_test_mode():
        print("\n" + "=" * 70)
        print(f"[УВЕДОМЛЕНИЕ — Telegram недоступен] "
              f"{kind} {ticker} {ts.strftime('%H:%M:%S')}")
        print("-" * 70)
        print(_strip_html(message))
        print("=" * 70)
    return True


def show_recent_notifications(n: int = RECENT_NOTIFICATIONS_SHOWN) -> None:
    notifs = load_notifications()
    if not notifs:
        print("[УВЕДОМЛЕНИЯ] Журнал пуст.")
        return
    print("\n" + "=" * 70)
    print(f"ПОСЛЕДНИЕ {min(n, len(notifs))} УВЕДОМЛЕНИЙ (новые сверху):")
    print("=" * 70)
    for notif in notifs[:n]:
        sent_mark = "✓ TG" if notif.get("telegram_sent") else "✗ TG"
        print(f"[{notif.get('date')} {notif.get('time')}] "
              f"{notif.get('kind')} {notif.get('ticker')}  [{sent_mark}]")
        print("-" * 70)
        print(_strip_html(notif.get("message", "")))
        print("=" * 70)


def send_pending_notifications() -> None:
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    notifs = load_notifications()
    pending = [n for n in notifs
               if n.get("date") == today and not n.get("telegram_sent")]
    if not pending:
        print(f"[PENDING] Нет неотправленных за {today}.")
        return
    if is_test_mode():
        print("[PENDING] Telegram не настроен. Уведомления только в файле.")
        return
    print(f"[PENDING] Найдено {len(pending)} неотправленных за {today}.")
    sent = 0
    for n in pending:
        if send_telegram_message(n["message"]):
            n["telegram_sent"] = True
            sent += 1
            print(f"[PENDING] OK: {n['kind']} {n['ticker']} {n['time']}")
        else:
            print(f"[PENDING] FAIL: {n['kind']} {n['ticker']} {n['time']}")
    if sent > 0:
        save_notifications(notifs)
    print(f"[PENDING] Отправлено {sent} из {len(pending)}.")


def try_send_pending_silent() -> None:
    if is_test_mode():
        return
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    notifs = load_notifications()
    pending = [n for n in notifs
               if n.get("date") == today and not n.get("telegram_sent")]
    if not pending:
        return
    print(f"[STARTUP] Найдено {len(pending)} неотправленных за сегодня. Пробую...")
    sent = 0
    for n in pending:
        if send_telegram_message(n["message"]):
            n["telegram_sent"] = True
            sent += 1
    if sent > 0:
        save_notifications(notifs)
        print(f"[STARTUP] Дослано {sent} из {len(pending)}.")
    else:
        print(f"[STARTUP] Telegram недоступен. Смотри {NOTIFICATIONS_FILE}.")


# ==============================================================================
# 4. ПОРТФЕЛЬ
# ==============================================================================

def _to_float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if result > 0 else None
    except Exception:
        return None


def load_portfolio(filepath: str = PORTFOLIO_FILE
                   ) -> Tuple[Dict[str, dict], float, Optional[str]]:
    raw, err = _load_json_file(filepath)
    if err:
        return {}, 0.0, err
    if not isinstance(raw, dict):
        return {}, 0.0, f"{filepath}: корневой элемент не объект"
    try:
        cash = float(raw.get("CASH", 0.0) or 0.0)
    except Exception:
        return {}, 0.0, f"{filepath}: CASH не число"

    positions: Dict[str, dict] = {}
    for key, value in raw.items():
        if key.upper() == "CASH":
            continue
        if not isinstance(value, dict):
            return {}, 0.0, f"{filepath}: позиция {key} не объект"
        if "count" not in value or "buy_price" not in value:
            return {}, 0.0, f"{filepath}: позиция {key} без count/buy_price"
        try:
            count = int(value["count"])
            buy_price = float(value["buy_price"])
        except Exception:
            return {}, 0.0, f"{filepath}: позиция {key} содержит нечисловые поля"
        if count < 0 or buy_price <= 0:
            return {}, 0.0, (
                f"{filepath}: позиция {key} имеет count<0 или buy_price<=0"
            )
        if count == 0:
            continue
        target = _to_float_or_none(
            value.get("target",
                      value.get("forecast_price",
                                value.get("expected_price")))
        )
        stop = _to_float_or_none(value.get("stop"))
        positions[key.upper()] = {
            "count": count,
            "buy_price": buy_price,
            "target": target,
            "stop": stop,
        }
    return positions, cash, None


# ==============================================================================
# 5. MOEX ISS
# ==============================================================================

def create_moex_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=25, pool_maxsize=25)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    return session


def get_moex_market_data() -> Dict[str, dict]:
    url = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
           "securities.json?iss.meta=off&iss.only=securities,marketdata")
    session = create_moex_session()
    payload = None
    last_err = None
    for attempt in range(1, MOEX_MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=(MOEX_CONNECT_TIMEOUT, MOEX_READ_TIMEOUT))
            r.raise_for_status()
            payload = r.json()
            break
        except requests.RequestException as e:
            last_err = e
            if attempt < MOEX_MAX_RETRIES:
                sleep_time = attempt * 3
                print(f"[СЕТЬ] Попытка {attempt}/{MOEX_MAX_RETRIES}: {e}. "
                      f"Ждём {sleep_time}с...")
                time.sleep(sleep_time)
    if payload is None:
        raise last_err if last_err else RuntimeError("Не удалось получить данные MOEX")

    sec_cols = payload["securities"]["columns"]
    sec_map = {}
    for row in payload["securities"]["data"]:
        t = row[sec_cols.index("SECID")]
        sec_map[t] = {
            "lot_size": int(row[sec_cols.index("LOTSIZE")] or 1),
            "name": row[sec_cols.index("SHORTNAME")],
            "sec_type": str(row[sec_cols.index("SECTYPE")]),
        }

    m_cols = payload["marketdata"]["columns"]
    i_sec = m_cols.index("SECID")
    i_last = m_cols.index("LAST")
    i_high = m_cols.index("HIGH") if "HIGH" in m_cols else None
    i_chg = m_cols.index("LASTTOPREVPRICE")
    i_vol = m_cols.index("VALTODAY")
    i_bid = m_cols.index("BID") if "BID" in m_cols else None
    i_ask = m_cols.index("OFFER") if "OFFER" in m_cols else None

    all_stocks: Dict[str, dict] = {}
    for row in payload["marketdata"]["data"]:
        t = row[i_sec]
        last = row[i_last]
        chg = row[i_chg]
        vol = row[i_vol] or 0.0
        if last is None or t not in sec_map:
            continue
        info = sec_map[t]
        # Не фильтруем по SECTYPE: в TQBR есть акции, ETF и БПИФ (например, GOLD).

        high_val = row[i_high] if i_high is not None else None
        bid_val = row[i_bid] if i_bid is not None else None
        ask_val = row[i_ask] if i_ask is not None else None
        all_stocks[t] = {
            "ticker": t,
            "last": float(last),
            "high": float(high_val) if high_val is not None else float(last),
            "bid": float(bid_val) if bid_val is not None else float(last),
            "ask": float(ask_val) if ask_val is not None else float(last),
            "change_pct": float(chg or 0.0),
            "volume": float(vol),
            "lot_size": info["lot_size"],
            "name": info["name"],
        }
    return all_stocks


def fetch_candles(ticker: str, session: requests.Session,
                  days: int = CANDLES_DAYS) -> list:
    now = time.time()
    cached = _candles_cache.get(ticker)
    if cached and (now - cached[0]) < CANDLES_CACHE_TTL:
        return cached[1]
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
           f"securities/{ticker}/candles.json?interval=24&from={from_date}"
           f"&iss.meta=off")
    try:
        res = session.get(url, timeout=(MOEX_CONNECT_TIMEOUT, 15)).json()
        candles = res.get("candles", {}).get("data", [])
        _candles_cache[ticker] = (now, candles)
        return candles
    except Exception:
        return []


# ==============================================================================
# 6. ИНДИКАТОРЫ
# ==============================================================================

def _rsi(closes: List[float], period: int = RSI_WINDOW_DAYS) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(candles: List[list], period: int = ATR_WINDOW_DAYS) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        prev_close = candles[i - 1][1]
        if high is None or low is None or prev_close is None:
            continue
        trs.append(max(high - low, abs(high - prev_close),
                       abs(low - prev_close)))
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _ema(closes: List[float], period: int) -> float:
    if not closes:
        return 0.0
    if len(closes) < period:
        return sum(closes) / len(closes)
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _sma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def enrich_with_indicators(stocks: List[dict], max_workers: int = 6) -> None:
    def worker(stock: dict) -> None:
        session = create_moex_session()
        ticker = stock["ticker"]
        try:
            candles = fetch_candles(ticker, session)
        except Exception:
            candles = []

        stock.setdefault("atr", 0.0)
        stock.setdefault("atr_pct", 0.0)
        stock.setdefault("rsi", 50.0)
        stock.setdefault("sma50", None)
        stock.setdefault("ema50", stock["last"])
        stock.setdefault("highest_high", stock["last"])

        if len(candles) < 2:
            return
        closes = [float(c[1]) for c in candles if c[1] is not None]
        highs = [float(c[2]) for c in candles if c[2] is not None]
        if len(closes) < 2:
            return

        last = stock["last"] if stock.get("last", 0) > 0 else closes[-1]
        stock["last"] = last
        atr = _atr(candles, ATR_WINDOW_DAYS)
        stock["atr"] = atr
        stock["atr_pct"] = (atr / last * 100.0) if last > 0 else 0.0
        stock["rsi"] = _rsi(closes + [last], RSI_WINDOW_DAYS)
        stock["sma50"] = _sma(closes, 50)
        stock["ema50"] = _ema(closes, TREND_EMA_PERIOD)
        n = min(TRAILING_LOOKBACK_DAYS, len(highs))
        stock["highest_high"] = max(highs[-n:]) if n > 0 else last

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(worker, stocks))


# ==============================================================================
# 7. ИЗДЕРЖКИ
# ==============================================================================

def net_proceeds(shares: int, sell_price: float, buy_price: float) -> dict:
    shares = max(0, int(shares))
    gross = shares * sell_price
    comm_sell = gross * COMMISSION_PCT / 100.0
    comm_buy = shares * buy_price * COMMISSION_PCT / 100.0
    gross_profit = shares * (sell_price - buy_price)
    tax = max(0.0, gross_profit) * TAX_PCT / 100.0 if INCLUDE_TAX else 0.0
    return {
        "gross": gross,
        "commission": comm_sell + comm_buy,
        "tax": tax,
        "net_profit": gross_profit - comm_sell - comm_buy - tax,
        "net_received": gross - comm_sell - comm_buy - tax,
    }


def breakeven_price(buy_price: float) -> float:
    if buy_price <= 0:
        return buy_price
    c = COMMISSION_PCT / 100.0
    tau = TAX_PCT / 100.0 if INCLUDE_TAX else 0.0
    den = 1.0 - tau - c
    if den <= 0:
        return buy_price
    return buy_price * (1.0 - tau + c) / den


# ==============================================================================
# 8. ПЛАНЫ И УРОВНИ
# ==============================================================================

def _stage_from_sold(levels: List[dict], sold: int) -> int:
    stage = 0
    for level in levels:
        if sold >= int(level.get("cum_count", 0)):
            stage += 1
        else:
            break
    return stage


def _make_levels(specs: List[Tuple[str, float, float, bool]],
                 original_count: int, lot: int) -> Optional[List[dict]]:
    if lot <= 0 or original_count <= 0:
        return None
    levels: List[dict] = []
    for name, price, cum_pct, is_final in specs:
        if price <= 0:
            return None
        if is_final:
            cum_count = original_count
        else:
            cum_count = int(original_count * cum_pct / lot) * lot
        if cum_count < lot and not is_final:
            continue
        levels.append({
            "name": name,
            "price": float(price),
            "cum_pct": float(cum_pct),
            "cum_count": int(cum_count),
            "is_final": bool(is_final),
        })
    if not levels:
        return None
    for i in range(1, len(levels)):
        if levels[i]["cum_count"] <= levels[i - 1]["cum_count"]:
            levels[i]["cum_count"] = min(
                original_count, levels[i - 1]["cum_count"] + lot
            )
    levels[-1]["cum_count"] = original_count
    levels[-1]["is_final"] = True
    return levels


def _build_atr_levels(anchor: float, atr: float,
                      original_count: int, lot: int) -> Optional[List[dict]]:
    if atr <= 0:
        return None
    if original_count <= lot:
        return _make_levels(
            [("TP1", anchor + ATR_TP_MULTS[-1] * atr, 1.0, True)],
            original_count, lot,
        )
    if original_count < 3 * lot:
        return _make_levels(
            [("TP1", anchor + ATR_TP_MULTS[0] * atr,
              lot / original_count, False),
             ("TP2", anchor + ATR_TP_MULTS[-1] * atr, 1.0, True)],
            original_count, lot,
        )
    return _make_levels(
        [("TP1", anchor + ATR_TP_MULTS[0] * atr, ATR_TP_PCTS[0], False),
         ("TP2", anchor + ATR_TP_MULTS[1] * atr, ATR_TP_PCTS[1], False),
         ("TP3", anchor + ATR_TP_MULTS[2] * atr, 1.0, True)],
        original_count, lot,
    )


def _build_target_levels(anchor: float, stop: float, target: float,
                         original_count: int, lot: int,
                         atr: float = 0.0) -> Optional[List[dict]]:
    if target <= 0:
        return None
    if target <= anchor:
        return _make_levels(
            [("ЦЕЛЬ", target, 1.0, True)], original_count, lot
        )
    if original_count <= lot:
        return _make_levels(
            [("ЦЕЛЬ", target, 1.0, True)], original_count, lot
        )

    if atr > 0:
        tp1_price = anchor + 2.0 * atr
        tp2_price = anchor + 4.0 * atr
    else:
        r = anchor - stop
        if r <= 0:
            return None
        tp1_price = anchor + TP1_R * r
        tp2_price = anchor + TP2_R * r

    if target <= tp1_price:
        specs = [("ЦЕЛЬ", target, 1.0, True)]
    elif original_count < 3 * lot:
        specs = [
            ("TP1", tp1_price, lot / original_count, False),
            ("ЦЕЛЬ", target, 1.0, True),
        ]
    elif target <= tp2_price:
        specs = [
            ("TP1", tp1_price, TP1_SELL_PCT_2, False),
            ("ЦЕЛЬ", target, 1.0, True),
        ]
    else:
        specs = [
            ("TP1", tp1_price, TP1_SELL_PCT_3, False),
            ("TP2", tp2_price,
             TP1_SELL_PCT_3 + TP2_SELL_PCT_3, False),
            ("ЦЕЛЬ", target, 1.0, True),
        ]
    return _make_levels(specs, original_count, lot)


def initial_stop(pos: dict, anchor: float, atr: float) -> Optional[float]:
    if anchor <= 0 or atr <= 0:
        return None
    provided = pos.get("stop")
    if provided is not None and 0 < float(provided) < anchor:
        stop = float(provided)
    else:
        distance = min(INITIAL_STOP_ATR * atr,
                       anchor * MAX_STOP_DISTANCE_PCT / 100.0)
        stop = anchor - distance
    if stop <= 0 or stop >= anchor:
        return None
    return stop


def build_position_plan(ticker: str, pos: dict, stock: dict,
                        today: date) -> Optional[dict]:
    atr = float(stock.get("atr", 0.0) or 0.0)
    lot = int(stock.get("lot_size", 1) or 1)
    count = int(pos["count"])
    if atr <= 0 or count <= 0 or lot <= 0:
        return None

    anchor = float(stock["last"])
    if anchor <= 0:
        return None
    stop = initial_stop(pos, anchor, atr)
    if stop is None:
        return None

    target = pos.get("target")
    if target is not None:
        levels = _build_target_levels(
            anchor, stop, float(target), count, lot, atr=atr
        )
    else:
        levels = _build_atr_levels(anchor, atr, count, lot)
    if not levels:
        return None

    risk_per_share = anchor - stop
    return {
        "avg_cost": float(pos["buy_price"]),
        "plan_anchor": float(anchor),
        "entry_atr": atr,
        "current_atr": atr,
        "initial_stop": float(stop),
        "stop_current": float(stop),
        "risk_per_share": float(risk_per_share),
        "initial_risk_rub": float(risk_per_share * count),
        "levels": levels,
        "targets": [float(l["price"]) for l in levels],
        "target": float(target) if target is not None else None,
        "stage": 0,
        "sold": 0,
        "original_count": count,
        "peak": float(max(anchor, stock["last"])),
        "opened_at": today.isoformat(),
        "exit_alerted": False,
        "archived": False,
        "pending_sell_shares": 0,
        "realized_pnl": 0.0,
        "realized_r": 0.0,
        "closed_at": None,
    }


def rebuild_levels(pstate: dict, pos: dict, stock: dict,
                   lot: int) -> bool:
    anchor = float(pstate["plan_anchor"])
    stop = float(pstate["initial_stop"])
    count = int(pstate["original_count"])
    target = pos.get("target")
    atr = float(stock.get("atr", 0.0) or 0.0)
    if target is not None:
        levels = _build_target_levels(
            anchor, stop, float(target), count, lot, atr=atr
        )
    else:
        levels = _build_atr_levels(anchor, atr, count, lot)
    if not levels:
        return False
    pstate["levels"] = levels
    pstate["targets"] = [float(l["price"]) for l in levels]
    pstate["target"] = float(target) if target is not None else None
    pstate["stage"] = _stage_from_sold(levels, int(pstate.get("sold", 0)))
    return True


# ==============================================================================
# 9. СИНХРОНИЗАЦИЯ
# ==============================================================================

def sync_state_with_portfolio(state: dict, portfolio: Dict[str, dict],
                              tracked: Dict[str, dict], today: date) -> List[str]:
    changes: List[str] = []
    pos_state = state["positions"]

    for ticker in list(pos_state.keys()):
        if ticker not in portfolio:
            p = pos_state[ticker]
            if p.get("exit_alerted") or int(p.get("sold", 0)) >= int(
                    p.get("original_count", 0)):
                changes.append(f"{ticker}: закрыта, удалена из state")
            else:
                changes.append(f"{ticker}: удалена из portfolio вручную")
            pos_state.pop(ticker, None)

    for ticker, pos in portfolio.items():
        stock = tracked.get(ticker)
        if not stock:
            changes.append(f"{ticker}: нет рыночных данных, пропуск")
            continue
        atr = float(stock.get("atr", 0.0) or 0.0)
        if atr <= 0:
            changes.append(f"{ticker}: ATR=0, пропуск")
            continue
        pstate = pos_state.get(ticker)

        if pstate is None:
            plan = build_position_plan(ticker, pos, stock, today)
            if plan:
                pos_state[ticker] = plan
                changes.append(
                    f"{ticker}: новый план, anchor={plan['plan_anchor']:.3f}, "
                    f"стоп={plan['initial_stop']:.3f}, "
                    f"цели={[round(x, 2) for x in plan['targets']]}"
                )
            else:
                changes.append(f"{ticker}: не удалось построить план")
            continue

        pstate["current_atr"] = atr
        pstate["peak"] = max(float(pstate.get("peak", stock["last"])),
                             float(stock["last"]))

        expected = int(pstate.get("original_count", 0)) - int(
            pstate.get("sold", 0)
        )
        count = int(pos["count"])
        buy_changed = abs(float(pos["buy_price"]) -
                          float(pstate.get("avg_cost", 0.0))) > 0.001
        target_changed = abs(
            (float(pos["target"]) if pos.get("target") is not None else 0.0)
            - (float(pstate.get("target"))
               if pstate.get("target") is not None else 0.0)
        ) > 0.001

        pending_sell = int(pstate.get("pending_sell_shares", 0) or 0)
        if pending_sell > 0 and not buy_changed:
            if count <= expected:
                pstate["pending_sell_shares"] = 0
            elif count <= expected + pending_sell:
                continue
            else:
                pstate["pending_sell_shares"] = 0

        if pstate.get("exit_alerted") and count <= max(0, expected):
            if count < max(0, expected):
                pstate["sold"] = int(pstate["original_count"]) - count
                pstate["stage"] = _stage_from_sold(
                    pstate.get("levels", []), int(pstate["sold"])
                )
                changes.append(f"{ticker}: ручная продажа после exit, "
                               f"sold={pstate['sold']}")
            continue

        if count > expected or buy_changed:
            old = (f"было count={expected}, buy={pstate.get('avg_cost')}, "
                   f"sold={pstate.get('sold')}")
            plan = build_position_plan(ticker, pos, stock, today)
            if plan:
                pos_state[ticker] = plan
                reason = "докупка" if count > expected else "смена buy_price"
                changes.append(
                    f"{ticker}: переинициализация ({reason}) [{old}] → "
                    f"count={count}, buy={pos['buy_price']:.3f}, "
                    f"anchor={plan['plan_anchor']:.3f}"
                )
            continue

        if count < expected:
            pstate["sold"] = int(pstate["original_count"]) - count
            pstate["stage"] = _stage_from_sold(
                pstate.get("levels", []), int(pstate["sold"])
            )
            changes.append(
                f"{ticker}: ручная продажа {expected - count} шт., "
                f"sold={pstate['sold']}, остаток={count}"
            )

        if target_changed:
            lot = int(stock.get("lot_size", 1) or 1)
            if rebuild_levels(pstate, pos, stock, lot):
                changes.append(
                    f"{ticker}: target обновлён, "
                    f"цели={[round(x, 2) for x in pstate['targets']]}"
                )
            else:
                changes.append(f"{ticker}: не удалось пересчитать уровни")

    return changes


# ==============================================================================
# 10. ВЫХОДЫ
# ==============================================================================

def _executable_sell_price(stock: dict) -> float:
    bid = float(stock.get("bid", stock["last"]) or stock["last"])
    return min(float(stock["last"]), bid)


def _executable_tp_price(stock: dict) -> float:
    bid = float(stock.get("bid", stock["last"]) or stock["last"])
    return max(float(stock["last"]), bid)


def _stop_reason(pstate: dict, stop: float, chandelier: float) -> str:
    r = float(pstate.get("risk_per_share", 0.0) or 0.0)
    anchor = float(pstate.get("plan_anchor", 0.0) or 0.0)
    avg_cost = float(pstate.get("avg_cost", anchor))
    stage = int(pstate.get("stage", 0) or 0)
    levels = pstate.get("levels", [])
    tp1_price = float(levels[0]["price"]) if levels else (anchor + 1.0 * r)
    plan_be = max(breakeven_price(avg_cost), breakeven_price(anchor))
    if abs(stop - float(pstate.get("initial_stop", 0.0))) < 1e-6:
        return "начальный стоп"
    if stage >= 2 and abs(stop - chandelier) < 1e-6 and chandelier > tp1_price:
        return "Chandelier Exit (трейлинг)"
    if stage >= 2 and stop >= tp1_price - 1e-6:
        return "защита прибыли (уровень TP1)"
    if stop >= plan_be - 1e-6:
        return "безубыток с учётом издержек"
    return "трейлинг-стоп"


def _lot_aligned_shares(shares: int, lot: int) -> Tuple[int, bool]:
    if lot <= 0:
        return 0, False
    aligned = (shares // lot) * lot
    return aligned, aligned < shares


def update_stops(state: dict, tracked: Dict[str, dict]) -> None:
    for ticker, pstate in state.get("positions", {}).items():
        stock = tracked.get(ticker)
        if not stock or pstate.get("exit_alerted"):
            continue
        anchor = float(pstate.get("plan_anchor", 0.0) or 0.0)
        r = float(pstate.get("risk_per_share", 0.0) or 0.0)
        if anchor <= 0 or r <= 0:
            continue
        entry_atr = float(pstate.get("entry_atr", 0.0) or 0.0)
        curr_atr = float(pstate.get("current_atr", entry_atr) or entry_atr)
        trail_atr = (max(entry_atr, curr_atr)
                     if entry_atr > 0 and curr_atr > 0 else (entry_atr or curr_atr))
        peak = float(pstate.get("peak", stock["last"]))
        chandelier = peak - TRAILING_ATR_MULT * trail_atr
        stage = int(pstate.get("stage", 0) or 0)
        levels = pstate.get("levels", [])
        avg_cost = float(pstate.get("avg_cost", anchor))

        components = [
            float(pstate.get("initial_stop", 0.0)),
            float(pstate.get("stop_current", 0.0)),
        ]
        if stage >= 1:
            components.append(breakeven_price(avg_cost))
            components.append(breakeven_price(anchor))
        if stage >= 2:
            tp1_price = float(levels[0]["price"]) if levels else (anchor + 1.0 * r)
            components.append(tp1_price)
            components.append(chandelier)
        pstate["stop_current"] = max(
            float(pstate.get("stop_current", 0.0)), max(components)
        )


def evaluate_stop(ticker: str, pos: dict, stock: dict,
                  state: dict) -> Optional[dict]:
    pstate = state["positions"].get(ticker)
    if not pstate or pstate.get("exit_alerted"):
        return None
    stop = float(pstate.get("stop_current", 0.0) or 0.0)
    if stop <= 0:
        return None
    price = _executable_sell_price(stock)
    if price > stop:
        return None

    count = int(pos["count"])
    lot = int(stock.get("lot_size", 1) or 1)
    shares, odd = _lot_aligned_shares(count, lot)
    if shares < lot:
        shares = count
        odd = False
    if shares <= 0:
        return None

    atr = float(pstate.get("current_atr",
                           pstate.get("entry_atr", 0.0)) or 0.0)
    entry_atr = float(pstate.get("entry_atr", atr) or atr)
    trail_atr = max(entry_atr, atr) if (entry_atr > 0 and atr > 0) else (entry_atr or atr)
    chandelier = float(pstate.get("peak", price)) - TRAILING_ATR_MULT * trail_atr
    money = net_proceeds(shares, price,
                         float(pstate.get("avg_cost", pos["buy_price"])))
    return {
        "ticker": ticker,
        "trigger": "STOP",
        "is_final": True,
        "price": price,
        "stop": stop,
        "stop_after": None,
        "reason": _stop_reason(pstate, stop, chandelier),
        "chandelier": chandelier,
        "avg_cost": float(pstate.get("avg_cost", pos["buy_price"])),
        "gain_pct": (price / float(pstate.get("avg_cost", pos["buy_price"]))
                     - 1.0) * 100.0,
        "shares": shares,
        "lots": shares // lot if lot > 0 else 0,
        "lot_warning": odd,
        "remaining_after": max(0, count - shares),
        "levels": pstate.get("levels", []),
        "stage": int(pstate.get("stage", 0) or 0),
        "peak": float(pstate.get("peak", price)),
        **money,
    }


def evaluate_profit(ticker: str, pos: dict, stock: dict,
                    state: dict) -> Optional[dict]:
    pstate = state["positions"].get(ticker)
    if not pstate or pstate.get("exit_alerted"):
        return None
    levels = pstate.get("levels", [])
    if not levels:
        return None

    price = _executable_tp_price(stock)
    sold = int(pstate.get("sold", 0) or 0)
    selected_idx = -1
    for i, level in enumerate(levels):
        if price >= float(level["price"]) and int(level["cum_count"]) > sold:
            selected_idx = i
    if selected_idx < 0:
        return None

    level = levels[selected_idx]
    count = int(pos["count"])
    lot = int(stock.get("lot_size", 1) or 1)
    is_final = bool(level.get("is_final"))
    if is_final:
        shares = count
        odd = False
    else:
        shares, odd = _lot_aligned_shares(
            int(level["cum_count"]) - sold, lot
        )
    if is_final:
        aligned, odd = _lot_aligned_shares(shares, lot)
        if aligned >= lot:
            shares = aligned
        elif shares > 0:
            shares = count
            odd = False
    if shares <= 0:
        return None
    shares = min(shares, count)

    new_stage = _stage_from_sold(levels, sold + shares)
    anchor = float(pstate.get("plan_anchor", 0.0) or 0.0)
    avg_cost = float(pstate.get("avg_cost", pos["buy_price"]))
    r = float(pstate.get("risk_per_share", 0.0) or 0.0)
    stop_after = max(breakeven_price(avg_cost), breakeven_price(anchor)) if anchor > 0 else None
    if stop_after is not None and new_stage >= 2:
        tp1_price = float(levels[0]["price"]) if levels else (anchor + 1.0 * r)
        stop_after = max(stop_after, tp1_price)

    avg_cost = float(pstate.get("avg_cost", pos["buy_price"]))
    money = net_proceeds(shares, price, avg_cost)
    return {
        "ticker": ticker,
        "trigger": str(level["name"]),
        "level_idx": selected_idx,
        "level_name": str(level["name"]),
        "is_final": is_final,
        "price": price,
        "target_price": float(level["price"]),
        "stop_after": stop_after,
        "avg_cost": avg_cost,
        "gain_pct": (price / avg_cost - 1.0) * 100.0,
        "shares": shares,
        "lots": shares // lot if lot > 0 else 0,
        "lot_warning": odd,
        "sell_ratio": (shares / count * 100.0) if count > 0 else 0.0,
        "remaining_after": max(0, count - shares),
        "levels": levels,
        "new_stage": new_stage,
        "stage": int(pstate.get("stage", 0) or 0),
        "peak": float(pstate.get("peak", price)),
        **money,
    }


def evaluate_time(ticker: str, pos: dict, stock: dict, state: dict,
                  today: date) -> Optional[dict]:
    pstate = state["positions"].get(ticker)
    if not pstate or pstate.get("exit_alerted"):
        return None
    opened = _parse_iso_date(pstate.get("opened_at"))
    if opened is None:
        return None
    limit = opened + timedelta(days=MAX_HOLDING_DAYS)
    if today < limit:
        return None

    price = _executable_sell_price(stock)
    count = int(pos["count"])
    lot = int(stock.get("lot_size", 1) or 1)
    shares, odd = _lot_aligned_shares(count, lot)
    if shares < lot:
        shares = count
        odd = False
    avg_cost = float(pstate.get("avg_cost", pos["buy_price"]))
    money = net_proceeds(shares, price, avg_cost)
    return {
        "ticker": ticker,
        "trigger": "TIME",
        "is_final": True,
        "price": price,
        "stop": None,
        "stop_after": None,
        "reason": f"истёк горизонт {MAX_HOLDING_DAYS} дн. с начала плана",
        "avg_cost": avg_cost,
        "gain_pct": (price / avg_cost - 1.0) * 100.0,
        "shares": shares,
        "lots": shares // lot if lot > 0 else 0,
        "lot_warning": odd,
        "remaining_after": max(0, count - shares),
        "levels": pstate.get("levels", []),
        "stage": int(pstate.get("stage", 0) or 0),
        "peak": float(pstate.get("peak", price)),
        **money,
    }


# ==============================================================================
# 11. СДЕЛКИ И СТАТИСТИКА
# ==============================================================================

def _record_closed_trade(state: dict, ticker: str, pstate: dict,
                         exit_price: float, exit_reason: str,
                         today: date) -> None:
    if pstate.get("archived"):
        return
    pstate["archived"] = True
    pstate["closed_at"] = today.isoformat()
    opened = _parse_iso_date(pstate.get("opened_at"))
    holding_days = (today - opened).days if opened else 0
    net_profit = float(pstate.get("realized_pnl", 0.0))
    risk = float(pstate.get("initial_risk_rub", 0.0))
    r_multiple = (net_profit / risk) if risk > 0 else 0.0
    trade = {
        "ticker": ticker,
        "avg_cost": float(pstate.get("avg_cost", 0.0)),
        "plan_anchor": float(pstate.get("plan_anchor", 0.0)),
        "exit_price": float(exit_price),
        "shares": int(pstate.get("original_count", 0)),
        "net_profit": net_profit,
        "r_multiple": r_multiple,
        "reason": exit_reason,
        "opened_at": pstate.get("opened_at"),
        "closed_at": today.isoformat(),
        "holding_days": holding_days,
    }
    stats = state.setdefault("stats", {"wins": 0, "losses": 0,
                                       "gross_profit": 0.0, "gross_loss": 0.0})
    if net_profit > 0:
        stats["wins"] = int(stats.get("wins", 0)) + 1
        stats["gross_profit"] = float(stats.get("gross_profit", 0.0)) + net_profit
    else:
        stats["losses"] = int(stats.get("losses", 0)) + 1
        stats["gross_loss"] = float(stats.get("gross_loss", 0.0)) + net_profit
    trades = state.setdefault("closed_trades", [])
    trades.append(trade)
    state["closed_trades"] = trades[-MAX_TRADE_LOG:]


def apply_exit(state: dict, ticker: str, pstate: dict, plan: dict,
               today: date) -> None:
    pstate["sold"] = int(pstate.get("sold", 0)) + int(plan["shares"])
    pstate["realized_pnl"] = (float(pstate.get("realized_pnl", 0.0))
                              + float(plan["net_profit"]))
    risk = float(pstate.get("initial_risk_rub", 0.0))
    if risk > 0:
        pstate["realized_r"] = (float(pstate.get("realized_r", 0.0))
                                + float(plan["net_profit"]) / risk)
    pstate["stage"] = _stage_from_sold(pstate.get("levels", []),
                                       int(pstate["sold"]))
    if plan.get("stop_after") is not None:
        pstate["stop_current"] = max(
            float(pstate.get("stop_current", 0.0)),
            float(plan["stop_after"]),
        )
    if plan.get("is_final"):
        pstate["pending_sell_shares"] = 0
        pstate["exit_alerted"] = True
        _record_closed_trade(
            state, ticker, pstate, float(plan["price"]),
            str(plan.get("reason") or plan.get("trigger")), today,
        )
    else:
        pstate["pending_sell_shares"] = (
            int(pstate.get("pending_sell_shares", 0) or 0)
            + int(plan["shares"])
        )


def compute_trade_stats(state: dict) -> dict:
    trades = state.get("closed_trades", [])
    if not trades:
        return {"count": 0}
    wins = [t for t in trades if float(t.get("net_profit", 0.0)) > 0]
    losses = [t for t in trades if float(t.get("net_profit", 0.0)) <= 0]
    gross_profit = sum(float(t.get("net_profit", 0.0)) for t in wins)
    gross_loss = sum(float(t.get("net_profit", 0.0)) for t in losses)
    r_values = [float(t.get("r_multiple", 0.0)) for t in trades]
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_values:
        cumulative += r
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return {
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100.0,
        "avg_r": sum(r_values) / len(r_values),
        "profit_factor": (gross_profit / abs(gross_loss)
                          if gross_loss < 0 else float("inf")),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "max_dd_r": max_dd,
        "avg_holding_days": (sum(int(t.get("holding_days", 0))
                                 for t in trades) / len(trades)),
        "by_reason": _stats_by_reason(trades),
    }


def _stats_by_reason(trades: List[dict]) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    for t in trades:
        reason = str(t.get("reason", "?"))
        bucket = result.setdefault(reason, {"count": 0,
                                            "net_profit": 0.0, "r": 0.0})
        bucket["count"] += 1
        bucket["net_profit"] += float(t.get("net_profit", 0.0))
        bucket["r"] += float(t.get("r_multiple", 0.0))
    return result


# ==============================================================================
# 12. ФОРМАТИРОВАНИЕ
# ==============================================================================

def _fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ")


def format_stop_message(p: dict) -> str:
    lot_line = ("⚠️ Остаток меньше лота — продайте вручную.\n"
                if p.get("lot_warning") else "")
    return (
        f"🛑 <b>СТОП — {p['ticker']}</b>\n"
        f"Цена: {p['price']:.2f} ₽ | Стоп: {p['stop']:.2f} ₽\n"
        f"Причина: {p['reason']}\n"
        f"Покупка: {p['avg_cost']:.2f} ₽ | P&L: {p['gain_pct']:+.2f}%\n"
        f"💼 Продать всю позицию: <b>{p['shares']} шт.</b> "
        f"({p['lots']} лот.)\n"
        f"Чистый результат: {_fmt_money(p['net_profit'])} ₽\n"
        f"{lot_line}"
        f"⚠️ Обновите portfolio.json после исполнения."
    )


def format_profit_message(p: dict) -> str:
    final_line = ("🏁 Позиция закрыта полностью.\n" if p["is_final"]
                  else f"Остаток: {p['remaining_after']} шт.\n")
    stop_line = (f"🛡️ Стоп после: {p['stop_after']:.2f} ₽\n"
                 if p.get("stop_after") is not None else "")
    lot_line = ("⚠️ Остаток меньше лота — продайте вручную.\n"
                if p.get("lot_warning") else "")
    return (
        f"🎯 <b>{p['level_name']} — {p['ticker']}</b>\n"
        f"Цена: {p['price']:.2f} ₽ | Покупка: {p['avg_cost']:.2f} ₽ | "
        f"P&L: {p['gain_pct']:+.2f}%\n"
        f"Уровень: {p['target_price']:.2f} ₽\n"
        f"💼 Продать: <b>{p['shares']} шт.</b> "
        f"({p['lots']} лот., {p['sell_ratio']:.1f}% позиции)\n"
        f"💵 Выручка: {_fmt_money(p['gross'])} ₽\n"
        f"📉 Комиссия: {_fmt_money(p['commission'])} ₽ | "
        f"Налог: {_fmt_money(p['tax'])} ₽\n"
        f"✅ Чистая прибыль: <b>{_fmt_money(p['net_profit'])} ₽</b>\n"
        f"{stop_line}{final_line}{lot_line}"
        f"⚠️ Обновите portfolio.json после исполнения."
    )


def format_time_message(p: dict) -> str:
    lot_line = ("⚠️ Остаток меньше лота — продайте вручную.\n"
                if p.get("lot_warning") else "")
    return (
        f"🚪 <b>ГОРИЗОНТ — {p['ticker']}</b>\n"
        f"Причина: {p['reason']}\n"
        f"Цена: {p['price']:.2f} ₽ | Покупка: {p['avg_cost']:.2f} ₽ | "
        f"P&L: {p['gain_pct']:+.2f}%\n"
        f"💼 Продать всю позицию: <b>{p['shares']} шт.</b> "
        f"({p['lots']} лот.)\n"
        f"Чистый результат: {_fmt_money(p['net_profit'])} ₽\n"
        f"{lot_line}"
        f"⚠️ Обновите portfolio.json после исполнения."
    )


# ==============================================================================
# 13. ДАШБОРД
# ==============================================================================

def print_dashboard(portfolio: Dict[str, dict], tracked: Dict[str, dict],
                    cash: float, state: dict) -> None:
    print("\n" + "=" * 158)
    print(f"{'Тикер':<6} | {'Всего':>7} | {'Покупка':>9} | {'Рынок':>9} | "
          f"{'День%':>7} | {'Доход%':>7} | {'P&L,₽':>10} | {'ATR%':>6} | "
          f"{'RSI':>5} | {'Стоп':>9} | {'TP1':>9} | {'TP2':>9} | "
          f"{'TP3':>9} | {'Стадия':>6} | {'Дней':>5} | {'Статус'}")
    print("-" * 158)

    total_val = 0.0
    total_cost = 0.0
    today = datetime.now(MSK_TZ).date()
    for ticker, pos in portfolio.items():
        stock = tracked.get(ticker)
        if not stock:
            print(f"{ticker:<6} | {pos['count']:>7} | НЕТ ДАННЫХ")
            continue
        cur = float(stock["last"])
        buy = float(pos["buy_price"])
        cnt = int(pos["count"])
        gain = (cur - buy) / buy * 100.0 if buy > 0 else 0.0
        pnl = (cur - buy) * cnt
        total_val += cur * cnt
        total_cost += buy * cnt

        pstate = state.get("positions", {}).get(ticker, {})
        levels = pstate.get("levels", [])
        stop = float(pstate.get("stop_current", 0.0) or 0.0)
        tp = [float(l["price"]) for l in levels]
        tp1 = tp[0] if len(tp) > 0 else 0.0
        tp2 = tp[1] if len(tp) > 1 else 0.0
        tp3 = tp[2] if len(tp) > 2 else 0.0
        stage = int(pstate.get("stage", 0) or 0)
        opened = _parse_iso_date(pstate.get("opened_at"))
        hold_days = (today - opened).days if opened else 0
        if pstate.get("exit_alerted"):
            status = "ЖДЁТ ИСПОЛНЕНИЯ"
        elif stage > 0:
            status = f"TP{stage}"
        else:
            status = "OK"

        print(f"{ticker:<6} | {cnt:>7} | {buy:>9.2f} | {cur:>9.2f} | "
              f"{stock.get('change_pct', 0.0):>+6.2f}% | {gain:>+6.2f}% | "
              f"{pnl:>+9.2f} | {stock.get('atr_pct', 0.0):>6.2f} | "
              f"{stock.get('rsi', 50.0):>5.1f} | {stop:>9.2f} | "
              f"{tp1:>9.2f} | {tp2:>9.2f} | {tp3:>9.2f} | "
              f"{stage:>3}/{len(levels):<2} | {hold_days:>5} | {status}")

    print("-" * 158)
    total_pnl = total_val - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
    print(f"Активы: {_fmt_money(total_val)} ₽ | Кэш: {_fmt_money(cash)} ₽ | "
          f"Капитал: {_fmt_money(total_val + cash)} ₽")
    print(f"P&L: {total_pnl:+,.2f} ₽ ({total_pnl_pct:+.2f}%)")
    print("=" * 158 + "\n")


def print_trade_stats(state: dict, portfolio: Optional[Dict[str, dict]] = None,
                      tracked: Optional[Dict[str, dict]] = None) -> None:
    stats = compute_trade_stats(state)
    print("\n" + "=" * 78)
    print("СТАТИСТИКА ЗАКРЫТЫХ СДЕЛОК")
    print("=" * 78)
    if stats.get("count", 0) == 0:
        print("Закрытых сделок пока нет.")
    else:
        print(f"Сделок: {stats['count']} | Побед: {stats['wins']} | "
              f"Поражений: {stats['losses']} | Win rate: {stats['win_rate']:.1f}%")
        print(f"Средний R: {stats['avg_r']:+.3f} | "
              f"Profit factor: {stats['profit_factor']:.2f}")
        print(f"Gross profit: {_fmt_money(stats['gross_profit'])} ₽ | "
              f"Gross loss: {_fmt_money(stats['gross_loss'])} ₽")
        print(f"Max drawdown по R: {stats['max_dd_r']:.2f}R | "
              f"Средний hold: {stats['avg_holding_days']:.1f} дн.")
        print("-" * 78)
        for reason, bucket in sorted(stats["by_reason"].items()):
            print(f"{reason:<28} n={bucket['count']:<3} "
                  f"P&L={_fmt_money(bucket['net_profit']):>12} ₽  "
                  f"R={bucket['r']:+.2f}")
    if portfolio and tracked:
        open_rows = []
        for ticker, pos in portfolio.items():
            pstate = state.get("positions", {}).get(ticker)
            stock = tracked.get(ticker)
            if not pstate or not stock:
                continue
            anchor = float(pstate.get("plan_anchor", 0.0))
            r = float(pstate.get("risk_per_share", 0.0))
            unrealized_r = ((float(stock["last"]) - anchor) / r) if r > 0 else 0.0
            pnl = (float(stock["last"]) - float(pos["buy_price"])) * int(pos["count"])
            open_rows.append((ticker, pnl, unrealized_r))
        if open_rows:
            print("-" * 78)
            print("ОТКРЫТЫЕ ПОЗИЦИИ (unrealized):")
            for ticker, pnl, r_mult in open_rows:
                print(f"{ticker:<7} P&L={_fmt_money(pnl):>12} ₽  R={r_mult:+.2f}")
    print("=" * 78 + "\n")


# ==============================================================================
# 14. МИГРАЦИЯ
# ==============================================================================

def _backup_file(path: str, suffix: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    backup = f"{path}.{suffix}.bak"
    if os.path.exists(backup):
        stamp = datetime.now(MSK_TZ).strftime("%Y%m%d%H%M%S")
        backup = f"{path}.{suffix}.{stamp}.bak"
    shutil.copy2(path, backup)
    return backup


def run_migration() -> None:
    print("\n" + "=" * 78)
    print("МИГРАЦИЯ / СБРОС STATE")
    print("=" * 78)

    portfolio, cash, err = load_portfolio()
    if err:
        print(f"✗ {err}")
        print("Миграция остановлена: сначала исправьте portfolio.json.")
        return

    backup_portfolio = _backup_file(PORTFOLIO_FILE, "v4")
    new_portfolio: Dict[str, Any] = {}
    for ticker, pos in portfolio.items():
        new_portfolio[ticker] = {
            "count": int(pos["count"]),
            "buy_price": float(pos["buy_price"]),
        }
        if pos.get("target") is not None:
            new_portfolio[ticker]["target"] = float(pos["target"])
        if pos.get("stop") is not None:
            new_portfolio[ticker]["stop"] = float(pos["stop"])
    new_portfolio["CASH"] = float(cash)
    atomic_write_json(PORTFOLIO_FILE, new_portfolio)
    print(f"✓ portfolio.json приведён к sell-only формату"
          f"{' (backup: ' + backup_portfolio + ')' if backup_portfolio else ''}")

    backup_state = _backup_file(STATE_FILE, "v4")
    atomic_write_json(STATE_FILE, default_state())
    print(f"✓ state.json сброшен"
          f"{' (backup: ' + backup_state + ')' if backup_state else ''}")

    print("\nfundamentals.json и ideas.json удалены из проекта.")
    print("Достаточно portfolio.json: count, buy_price, необязательный target.")
    print("=" * 78 + "\n")


# ==============================================================================
# 15. ИТЕРАЦИЯ
# ==============================================================================

def is_market_open() -> bool:
    now = datetime.now(MSK_TZ)
    if now.weekday() >= 5:
        return False
    return SESSION_START <= now.time() <= SESSION_END


def refresh_peaks(state: dict, tracked: Dict[str, dict]) -> None:
    for ticker, pstate in state.get("positions", {}).items():
        stock = tracked.get(ticker)
        if not stock:
            continue
        pstate["peak"] = max(float(pstate.get("peak", stock["last"])),
                             float(stock["last"]))


def _dispatch_full_exit(state: dict, ticker: str, plan: dict,
                        message: str, kind: str) -> bool:
    pstate = state["positions"].get(ticker)
    if not pstate:
        return False
    key = f"{ticker}_{plan['trigger']}"
    if not dispatch_notification(key, kind, ticker, message):
        return False
    apply_exit(state, ticker, pstate, plan, plan["today"])
    return True


def _dispatch_partial_exit(state: dict, ticker: str, plan: dict) -> bool:
    pstate = state["positions"].get(ticker)
    if not pstate:
        return False
    key = f"{ticker}_{plan['trigger']}"
    if not dispatch_notification(key, "PROFIT", ticker,
                                 format_profit_message(plan)):
        return False
    apply_exit(state, ticker, pstate, plan, plan["today"])
    return True


def run_iteration(state: dict) -> bool:
    now = datetime.now(MSK_TZ)
    ts = now.strftime("%H:%M:%S")
    today = now.date()
    today_str = today.isoformat()

    if state.get("notified_date") != today_str:
        state["notified_date"] = today_str
        state["notified_profit"] = []
        state["notified_stop"] = []
        print(f"[{ts}] Новый день — дневные списки уведомлений сброшены.")

    portfolio, cash, pf_err = load_portfolio()
    if pf_err:
        print(f"[{ts}] [ОСТАНОВ] {pf_err}")
        return False

    if not portfolio:
        print_dashboard(portfolio, {}, cash, state)
        save_state(state)
        return True

    print(f"[{ts}] Запрос рыночных данных MOEX "
          f"({len(portfolio)} тикеров)...")
    try:
        all_stocks = get_moex_market_data()
    except Exception as e:
        print(f"[{ts}] Ошибка получения данных: {e}")
        return False
    if not all_stocks:
        print(f"[{ts}] Пустой ответ MOEX.")
        return False

    tracked: Dict[str, dict] = {}
    for ticker in portfolio:
        if ticker in all_stocks:
            tracked[ticker] = all_stocks[ticker]
        else:
            print(f"[{ts}] [ВНИМАНИЕ] {ticker} нет в TQBR.")

    enrich_with_indicators(list(tracked.values()))

    changes = sync_state_with_portfolio(state, portfolio, tracked, today)
    for ch in changes:
        print(f"[{ts}] [SYNC] {ch}")

    refresh_peaks(state, tracked)
    update_stops(state, tracked)
    print_dashboard(portfolio, tracked, cash, state)

    for ticker, pos in portfolio.items():
        stock = tracked.get(ticker)
        pstate = state["positions"].get(ticker)
        if not stock or not pstate:
            continue

        stop_plan = evaluate_stop(ticker, pos, stock, state)
        if stop_plan:
            stop_plan["today"] = today
            if _dispatch_full_exit(state, ticker, stop_plan,
                                   format_stop_message(stop_plan), "STOP"):
                print(f"[{ts}] [СТОП] {ticker}: {stop_plan['price']:.2f} ≤ "
                      f"{stop_plan['stop']:.2f}, продано "
                      f"{stop_plan['shares']} шт.")
            continue

        profit_plan = evaluate_profit(ticker, pos, stock, state)
        if profit_plan:
            profit_plan["today"] = today
            if _dispatch_partial_exit(state, ticker, profit_plan):
                print(f"[{ts}] [ФИКСАЦИЯ] {ticker}: "
                      f"{profit_plan['trigger']}, "
                      f"{profit_plan['shares']} шт., "
                      f"чистая {profit_plan['net_profit']:.2f} ₽")
                if not profit_plan["is_final"]:
                    continue

        time_plan = evaluate_time(ticker, pos, stock, state, today)
        if time_plan:
            time_plan["today"] = today
            if _dispatch_full_exit(state, ticker, time_plan,
                                   format_time_message(time_plan), "TIME"):
                print(f"[{ts}] [ГОРИЗОНТ] {ticker}: {time_plan['reason']}")

    save_state(state)
    return True


# ==============================================================================
# 16. CLI
# ==============================================================================

def print_header() -> None:
    print("=" * 82)
    print(f" MOEX Stock Tracker v{VERSION} — sell-only ")
    print("=" * 82)
    print("Вход: portfolio.json — count, buy_price, необязательный target.")
    print("Выход: только уведомления о продаже.")
    print(f"Стоп: {INITIAL_STOP_ATR:.1f} ATR / структурный, "
          f"Chandelier {TRAILING_ATR_MULT:.1f} ATR, "
          f"безубыток с издержками.")
    print(f"Без target: TP {ATR_TP_MULTS[0]:.0f}/{ATR_TP_MULTS[1]:.0f}/"
          f"{ATR_TP_MULTS[2]:.0f} ATR, доли 40/30/остаток.")
    print(f"С target: TP1 = 2 ATR (40%), TP2 = 4 ATR (30%), "
          f"финал = target; последняя ступень продаёт всё.")
    print(f"Горизонт: {MAX_HOLDING_DAYS} дн. | "
          f"Издержки: комиссия {COMMISSION_PCT}% | НДФЛ {TAX_PCT}%")
    print(f"Файлы: {PORTFOLIO_FILE}, {STATE_FILE}, {NOTIFICATIONS_FILE}")
    print(f"Telegram: " + ("ТЕСТ (токен не задан)" if is_test_mode()
                           else "включён"))
    print("=" * 82)


def _load_runtime(require_state: bool = True
                  ) -> Tuple[Optional[dict], Optional[str]]:
    state, state_err = load_state()
    if state_err and require_state:
        return None, state_err
    if state_err:
        state = default_state()
    return state, None


def run_stats_cli() -> None:
    state, err = _load_runtime(require_state=False)
    if err:
        print(f"[ОШИБКА] {err}")
        return
    portfolio, cash, pf_err = load_portfolio()
    if pf_err:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] {pf_err}")
        portfolio = {}
    tracked: Dict[str, dict] = {}
    if portfolio:
        try:
            all_stocks = get_moex_market_data()
            tracked = {t: all_stocks[t] for t in portfolio if t in all_stocks}
            enrich_with_indicators(list(tracked.values()))
        except Exception as e:
            print(f"[ПРЕДУПРЕЖДЕНИЕ] не удалось получить рынок: {e}")
    print_trade_stats(state, portfolio, tracked)


def run_reset_plan_cli(argv: List[str]) -> None:
    try:
        idx = argv.index("--reset-plan")
        ticker = argv[idx + 1].upper()
    except Exception:
        print("Использование: python moex_tracker.py --reset-plan TICKER")
        return
    state, err = _load_runtime(require_state=True)
    if err or state is None:
        print(f"[ОШИБКА] {err}")
        return
    portfolio, cash, pf_err = load_portfolio()
    if pf_err:
        print(f"[ОШИБКА] {pf_err}")
        return
    if ticker not in portfolio:
        print(f"[ОШИБКА] {ticker} нет в portfolio.json")
        return
    try:
        all_stocks = get_moex_market_data()
    except Exception as e:
        print(f"[ОШИБКА] MOEX: {e}")
        return
    stock = all_stocks.get(ticker)
    if not stock:
        print(f"[ОШИБКА] {ticker} нет в TQBR")
        return
    enrich_with_indicators([stock])
    today = datetime.now(MSK_TZ).date()
    plan = build_position_plan(ticker, portfolio[ticker], stock, today)
    if not plan:
        print(f"[ОШИБКА] не удалось построить план для {ticker}")
        return
    state["positions"][ticker] = plan
    save_state(state)
    print(f"[RESET] {ticker}: план пересчитан от {plan['plan_anchor']:.2f} ₽")
    print(f"  Стоп: {plan['initial_stop']:.2f} ₽ | "
          f"Цели: {[round(x, 2) for x in plan['targets']]}")
    print(f"  R: {plan['risk_per_share']:.2f} ₽ | "
          f"Риск: {plan['initial_risk_rub']:.2f} ₽")


def main() -> None:
    argv = sys.argv[1:]
    if "--send-pending" in argv:
        print_header()
        send_pending_notifications()
        return
    if "--show" in argv:
        print_header()
        show_recent_notifications(30)
        return
    if "--migrate" in argv:
        print_header()
        run_migration()
        return
    if "--stats" in argv:
        print_header()
        run_stats_cli()
        return
    if "--reset-plan" in argv:
        print_header()
        run_reset_plan_cli(argv)
        return
    if "--once" in argv:
        print_header()
        state, err = load_state()
        if err:
            print(f"\n[ОСТАНОВ] {err}")
            print("Выполните: python moex_tracker.py --migrate")
            return
        ok = run_iteration(state)
        print("\n[ONCE] Итерация завершена."
              if ok else "\n[ONCE] Итерация не выполнена.")
        return
    if "--screen" in argv or "--ideas-check" in argv:
        print_header()
        print("Команда удалена: скрипт теперь sell-only. "
              "fundamentals.json и ideas.json удалены из проекта.")
        return

    print_header()
    state, err = load_state()
    if err:
        print(f"\n[ОСТАНОВ] {err}")
        print("Выполните: python moex_tracker.py --migrate")
        return

    try_send_pending_silent()
    show_recent_notifications(RECENT_NOTIFICATIONS_SHOWN)

    while True:
        try:
            if not is_market_open():
                now = datetime.now(MSK_TZ)
                print(f"[{now.strftime('%H:%M:%S')}] Биржа закрыта. Ждём...")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue
            ok = run_iteration(state)
            if not ok:
                print(f"Повтор через {RETRY_DELAY_SECONDS}с...")
                time.sleep(RETRY_DELAY_SECONDS)
                continue
        except requests.RequestException as e:
            print(f"[СЕТЬ] {e}")
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            save_state(state)
            break
        except Exception as e:
            print(f"[ОШИБКА] {e}")
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

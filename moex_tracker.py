"""
MOEX Stock Tracker v3.6 — ATR + золото + журнал + pre-TP1 защита + Wilder RSI.

Изменения относительно v3.5:
  • Pre-TP1 защита стопа: порог поднят до buy + 2.0 ATR (при TP1 = 2.5 ATR),
    стоп поднимается в buy + 0.5 ATR.
  • TP1 поднят до 2.5 ATR для соблюдения Risk/Reward >= 1.0 (при стопе 2.5 ATR).
  • Расчёт RSI переведён на сглаживание Уайлдера (Wilder's smoothing).
  • Учёт дневного максимума (HIGH) в трекинге пика позиции.

Команды:
    python moex_tracker.py                 — обычный запуск
    python moex_tracker.py --send-pending  — дослать накопленное в Telegram
    python moex_tracker.py --show          — показать последние уведомления
    python moex_tracker.py --audit-buffett — аудит качества бизнеса по Баффету

ВАЖНО: state.json совместим с v3.4. Если обновляешься с v3.0 или раньше —
удали state.json.
"""

import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import html
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

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

# ponytail: простой парсер .env без сторонних библиотек (python-dotenv не нужен)
if os.path.exists(".env"):
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

# --- АКЦИИ: ATR-логика ---
TAKE_PROFIT_ATR_LEVELS = [
    {"atr_mult": 2.5, "sell_pct": 0.25},
    {"atr_mult": 4.0, "sell_pct": 0.30},
    {"atr_mult": 6.0, "sell_pct": 0.35},
]
INITIAL_STOP_ATR       = 2.5
BREAKEVEN_PLUS_ATR     = 0.5      # после TP1: стоп = buy + 0.5 ATR
TRAILING_ATR_MULT      = 3.0
TRAILING_LOOKBACK_DAYS = 22

# --- Pre-TP1 защита прибыли ---
# Если цена дошла до buy + 2.0 ATR, но TP1 (2.5 ATR) ещё не сработал,
# поднимаем стоп в buy + 0.5 ATR. Защищает от «развернулось у порога TP1».
PRE_TP1_PROTECT_ATR      = 2.0
PRE_TP1_PROTECT_PLUS_ATR = 0.5

# --- ЗОЛОТО ---
GOLD_TICKERS = {"GOLD", "SBGD", "AKGD", "TGLD", "BCSG"}
GOLD_MA_PERIOD          = 200
GOLD_RSI_ENTRY          = 38.0
GOLD_RSI_FLOOR          = 20.0
GOLD_STOP_ATR           = 3.0
GOLD_BREAKEVEN_PLUS_ATR = 0.5
GOLD_TRAILING_ATR       = 4.0
GOLD_MAX_POSITION_PCT   = 0.15
GOLD_TAKE_PROFIT_LEVELS = [
    {"atr_mult": 2.5, "sell_pct": 0.30},
    {"atr_mult": 4.5, "sell_pct": 0.30},
    {"atr_mult": 7.0, "sell_pct": 0.40},
]

# --- Риск-менеджмент ---
RISK_PER_TRADE_PCT = 0.015
MAX_POSITION_PCT   = 0.25

# --- Индикаторы ---
ATR_WINDOW_DAYS  = 14
SMA_WINDOW_DAYS  = 20
RSI_WINDOW_DAYS  = 14
TREND_EMA_PERIOD = 50

# --- Вход для акций (эмпирический откат в тренде) ---
BUY_Z_THRESHOLD   = -1.2
BUY_RSI_THRESHOLD = 38.0
BUY_DAY_DROP_PCT  = 1.8

# --- Издержки ---
COMMISSION_PCT = 0.1
TAX_PCT        = 13.0
INCLUDE_TAX    = True

# --- Ликвидность ---
TOP_N_LIQUID     = 50
MIN_DAILY_VOLUME = 30_000_000

# --- Технические ---
CHECK_INTERVAL_SECONDS = 600
RETRY_DELAY_SECONDS    = 30
MOEX_CONNECT_TIMEOUT   = 10
MOEX_READ_TIMEOUT      = 25
MOEX_MAX_RETRIES       = 3
CANDLES_CACHE_TTL      = 1800

PORTFOLIO_FILE     = "portfolio.json"
STATE_FILE         = "state.json"
NOTIFICATIONS_FILE = "notifications.json"
FUNDAMENTALS_FILE  = "fundamentals.json"
MAX_NOTIFICATIONS_KEPT     = 500
RECENT_NOTIFICATIONS_SHOWN = 10

# --- Баффет-контроль качества бизнеса ---
BUFFETT_AUDIT_INTERVAL_DAYS   = 7      # аудит раз в неделю
MAX_DEBT_EBITDA_THRESHOLD     = 3.0    # долговой предел при ставке 20%+
MIN_ROE_THRESHOLD             = 10.0   # порог отдачи на капитал
SMA200_STRUCTURAL_DROP_PCT    = 25.0   # порог затяжного рыночного спада

MSK_TZ        = timezone(timedelta(hours=3))
SESSION_START = dtime(10, 0)
SESSION_END   = dtime(23, 50)

_candles_cache: Dict[str, Tuple[float, list]] = {}


# ==============================================================================
# 1. СОСТОЯНИЕ
# ==============================================================================

def load_state() -> dict:
    default = {
        "positions": {},
        "notified_date": "",
        "notified_profit": [],
        "notified_buy": [],
        "notified_stop": [],
        "notified_buffett": [],
        "last_buffett_audit": "",
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось прочитать {STATE_FILE}: {e}")
        return default


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось сохранить состояние: {e}")


# ==============================================================================
# 2. ЖУРНАЛ УВЕДОМЛЕНИЙ
# ==============================================================================

def load_notifications() -> list:
    if not os.path.exists(NOTIFICATIONS_FILE):
        return []
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось прочитать {NOTIFICATIONS_FILE}: {e}")
        return []


def save_notifications(notifs: list) -> None:
    tmp = NOTIFICATIONS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(notifs, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, NOTIFICATIONS_FILE)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось сохранить {NOTIFICATIONS_FILE}: {e}")


def _strip_html(text: str) -> str:
    return text.replace("<b>", "").replace("</b>", "")


def is_test_mode() -> bool:
    return (not TELEGRAM_BOT_TOKEN
            or TELEGRAM_BOT_TOKEN == "ВАШ_ТОКЕН_БОТА")


def notification_exists(key: str, date_str: str, notifs: Optional[list] = None) -> bool:
    if notifs is None:
        notifs = load_notifications()
    for n in notifs:
        if n.get("key") == key and n.get("date") == date_str:
            return True
    return False


def dispatch_notification(key: str, kind: str, ticker: str, message: str) -> bool:
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
    notifs = notifs[:MAX_NOTIFICATIONS_KEPT]
    save_notifications(notifs)

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
        print(f"[PENDING] Telegram не настроен. Уведомления только в файле.")
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
# 3. ПОРТФЕЛЬ
# ==============================================================================

def load_portfolio(filepath: str) -> Tuple[Dict[str, dict], float]:
    if not os.path.exists(filepath):
        print(f"[ВНИМАНИЕ] Файл портфеля '{filepath}' не найден.")
        return {}, 0.0
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cash = float(raw.get("CASH", 0.0))
        positions = {}
        for key, value in raw.items():
            if key.upper() == "CASH":
                continue
            if isinstance(value, dict) and "count" in value and "buy_price" in value:
                positions[key.upper()] = {
                    "count": int(value["count"]),
                    "buy_price": float(value["buy_price"]),
                    "core_count": int(value.get("core_count", 0)),
                }
            else:
                print(f"[ПРЕДУПРЕЖДЕНИЕ] Некорректная позиция {key}: {value}")
        return positions, cash
    except Exception as e:
        print(f"[ОШИБКА] Не удалось прочитать '{filepath}': {e}")
        return {}, 0.0


# ==============================================================================
# 3.1. БАФФЕТ-ФУНДАМЕНТАЛ
# ==============================================================================

def load_fundamentals() -> dict:
    if not os.path.exists(FUNDAMENTALS_FILE):
        return {}
    try:
        with open(FUNDAMENTALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось прочитать {FUNDAMENTALS_FILE}: {e}")
        return {}


def evaluate_buffett_health(ticker: str, pos: dict, stock: Optional[dict],
                            fund_map: dict) -> Tuple[str, List[str]]:
    """
    Проверяет бизнес компании по фундаментальным и рыночным критериям Баффета.
    """
    if ticker in GOLD_TICKERS:
        return "EXCELLENT", []

    fund = fund_map.get(ticker)
    reasons = []

    if fund:
        status = fund.get("status", "GOOD")
        net_debt = fund.get("net_debt_ebitda", 0.0)
        roe = fund.get("roe_pct", 15.0)
        fcf = fund.get("fcf_positive", True)
        flags = fund.get("red_flags", [])

        if status == "BAD":
            reasons.append(f"Критический статус: {fund.get('moat', 'бизнес разрушает стоимость')}")

        if fund.get("negative_equity", False):
            reasons.append("Отрицательный собственный капитал по МСФО (модель держится на долге)")

        if net_debt > MAX_DEBT_EBITDA_THRESHOLD:
            reasons.append(f"Чрезмерный долг: Чистый долг / EBITDA = {net_debt:.1f}x (выше {MAX_DEBT_EBITDA_THRESHOLD}x)")

        if not fund.get("negative_equity", False) and roe < MIN_ROE_THRESHOLD and fund.get("sector") != "Золотодобыча":
            reasons.append(f"Слабая отдача на капитал: ROE = {roe:.1f}% (ниже {MIN_ROE_THRESHOLD}%)")

        if not fcf:
            reasons.append("Отрицательный свободный денежный поток (FCF)")

        for fl in flags:
            reasons.append(fl)
    else:
        status = "UNKNOWN"

    # Биржевые критерии (ликвидность и затяжной тренд ниже SMA200)
    if stock:
        vol = stock.get("volume", 0.0)
        if vol > 0 and vol < MIN_DAILY_VOLUME:
            reasons.append(f"Падение ликвидности: оборот {vol:,.0f} ₽ < {MIN_DAILY_VOLUME:,.0f} ₽")

        sma200 = stock.get("sma200")
        last = stock.get("last", 0.0)
        if sma200 and last > 0:
            drop_from_sma = (sma200 - last) / sma200 * 100.0
            if drop_from_sma >= SMA200_STRUCTURAL_DROP_PCT:
                reasons.append(f"Структурный спад: цена {last:.2f} ₽ на {drop_from_sma:.1f}% ниже SMA200 ({sma200:.2f} ₽)")

    # Дедупликация причин
    reasons = list(dict.fromkeys(reasons))

    is_bad = (
        status == "BAD"
        or any("Критический" in r or "Чрезмерный долг" in r for r in reasons)
        or len(reasons) >= 3
    )
    final_status = "BAD" if is_bad else ("WARNING" if reasons else "GOOD")
    return final_status, reasons


def format_buffett_warning_message(ticker: str, status: str, reasons: List[str],
                                   fund_map: dict) -> str:
    fund = fund_map.get(ticker, {})
    name = fund.get("name", ticker)
    sector = fund.get("sector", "Рынок РФ")
    moat = fund.get("moat", "Не определён")
    # Экранирование спецсимволов HTML для Telegram
    reasons_fmt = "\n".join(f"• {html.escape(r)}" for r in reasons)

    # Динамический подбор первоклассных альтернатив без текущего тикера
    best_candidates = [
        t for t, f in fund_map.items()
        if t != ticker and f.get("status") == "EXCELLENT" and f.get("net_debt_ebitda", 0.0) <= 0.0
    ]
    if not best_candidates:
        best_candidates = [t for t, f in fund_map.items() if t != ticker and f.get("status") in ("EXCELLENT", "GOOD")]
    alt_str = ", ".join(best_candidates[:4]) if best_candidates else "SBER, LKOH, MOEX, HEAD"

    return (
        f"🚨 <b>БАФФЕТ-КОНТРОЛЬ: УХУДШЕНИЕ БИЗНЕСА — {ticker} ({name})</b>\n"
        f"Сектор: {sector} | Оценка: <b>{status}</b>\n"
        f"Экономический ров (Moat): {moat}\n\n"
        f"<b>Причины ухудшения:</b>\n"
        f"{reasons_fmt}\n\n"
        f"💼 <b>Рекомендация Баффета:</b> рассмотреть закрытие позиции "
        f"и перекладку в первоклассные компании с чистым балансом ({alt_str})."
    )


def run_buffett_audit(portfolio: Dict[str, dict], tracked: Dict[str, dict],
                      state: dict, force_notify: bool = False) -> None:
    fund_map = load_fundamentals()
    now = datetime.now(MSK_TZ)
    ts = now.strftime("%H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")

    print("\n" + "=" * 90)
    print(f"[{ts}] АУДИТ КАЧЕСТВА БИЗНЕСА ПО БАФФЕТУ (Buffett Economic Moat Audit)")
    print("=" * 90)
    print(f"{'Тикер':6} | {'Компания':18} | {'Сектор':15} | {'Статус':10} | {'Экономический ров (Moat)'}")
    print("-" * 90)

    alerts = []
    for t, pos in portfolio.items():
        if t in GOLD_TICKERS:
            print(f"{t:6} | {'Золото (БПИФ)':18} | {'Сырьё/Защита':15} | {'EXCELLENT':10} | Физическое золото")
            continue

        st = tracked.get(t)
        fund = fund_map.get(t, {})
        name = fund.get("name", t)[:18]
        sec = fund.get("sector", "-")[:15]

        status, reasons = evaluate_buffett_health(t, pos, st, fund_map)
        moat_short = fund.get("moat", "-")[:35]
        print(f"{t:6} | {name:18} | {sec:15} | {status:10} | {moat_short}")

        if status in ("BAD", "WARNING") and reasons:
            alerts.append((t, status, reasons))

    print("=" * 90 + "\n")

    for t, status, reasons in alerts:
        msg = format_buffett_warning_message(t, status, reasons, fund_map)
        key = f"{t}_BUFFETT_{date_str}" if not force_notify else f"{t}_BUFFETT_FORCE_{int(time.time())}"
        if key not in state.get("notified_buffett", []):
            if dispatch_notification(key, "BUFFETT_ALERT", t, msg):
                print(f"[{ts}] [БАФФЕТ-ПРЕДУПРЕЖДЕНИЕ] {t}: {status} — уведомление отправлено!")
                state.setdefault("notified_buffett", []).append(key)


# ==============================================================================
# 4. MOEX ISS
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


def get_moex_market_data(max_attempts: int = MOEX_MAX_RETRIES) -> Tuple[Dict[str, dict], List[dict]]:
    url = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"
           "?iss.meta=off&iss.only=securities,marketdata")
    session = create_moex_session()
    payload = None
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = session.get(url, timeout=(MOEX_CONNECT_TIMEOUT, MOEX_READ_TIMEOUT))
            r.raise_for_status()
            payload = r.json()
            break
        except requests.RequestException as e:
            last_err = e
            if attempt < max_attempts:
                sleep_time = attempt * 3
                print(f"[СЕТЬ] Попытка {attempt}/{max_attempts}: {e}. Ждём {sleep_time}с...")
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
    i_sec  = m_cols.index("SECID")
    i_last = m_cols.index("LAST")
    i_high = m_cols.index("HIGH") if "HIGH" in m_cols else None
    i_chg  = m_cols.index("LASTTOPREVPRICE")
    i_vol  = m_cols.index("VALTODAY")
    i_bid  = m_cols.index("BID") if "BID" in m_cols else None
    i_ask  = m_cols.index("OFFER") if "OFFER" in m_cols else None

    all_stocks = {}
    liquid = []
    for row in payload["marketdata"]["data"]:
        t = row[i_sec]
        last = row[i_last]
        chg = row[i_chg]
        vol = row[i_vol] or 0.0
        if last is None or t not in sec_map:
            continue
        info = sec_map[t]
        is_gold = t in GOLD_TICKERS
        if info["sec_type"] not in ("1", "2", "A") and not is_gold:
            continue

        high_val = row[i_high] if i_high is not None else None
        bid_val = row[i_bid] if i_bid is not None else None
        ask_val = row[i_ask] if i_ask is not None else None

        stock = {
            "ticker": t,
            "last": float(last),
            "high": float(high_val) if high_val is not None else float(last),
            "bid": float(bid_val) if bid_val is not None else float(last),
            "ask": float(ask_val) if ask_val is not None else float(last),
            "change_pct": float(chg or 0.0),
            "volume": float(vol),
            "lot_size": info["lot_size"],
            "name": info["name"],
            "is_gold": is_gold,
        }
        all_stocks[t] = stock
        if stock["volume"] >= MIN_DAILY_VOLUME or is_gold:
            liquid.append(stock)

    liquid.sort(key=lambda s: s["volume"], reverse=True)
    top = liquid[:TOP_N_LIQUID]
    existing = {s["ticker"] for s in top}

    # Всегда включаем золото, бумаги портфеля и первоклассные компании Баффета
    priority_tickers = set(GOLD_TICKERS)
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                p_raw = json.load(f)
                priority_tickers.update(k.upper() for k in p_raw.keys() if k.upper() != "CASH")
        except Exception:
            pass
    fund_map = load_fundamentals()
    for t, f in fund_map.items():
        if f.get("status") in ("EXCELLENT", "GOOD"):
            priority_tickers.add(t)

    for s in liquid:
        t = s["ticker"]
        if t in priority_tickers and t not in existing:
            top.append(s)
            existing.add(t)
    return all_stocks, top


def fetch_candles(ticker: str, session: requests.Session) -> list:
    now = time.time()
    cached = _candles_cache.get(ticker)
    if cached and (now - cached[0]) < CANDLES_CACHE_TTL:
        return cached[1]
    days_back = 400 if ticker in GOLD_TICKERS else 120
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
           f"securities/{ticker}/candles.json?interval=24&from={from_date}&iss.meta=off")
    try:
        res = session.get(url, timeout=(MOEX_CONNECT_TIMEOUT, 15)).json()
        candles = res.get("candles", {}).get("data", [])
        _candles_cache[ticker] = (now, candles)
        return candles
    except Exception:
        return []


# ==============================================================================
# 5. ИНДИКАТОРЫ
# ==============================================================================

def _rsi(closes: List[float], period: int = 14) -> float:
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


def _atr(candles: List[list], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        prev_close = candles[i - 1][1]
        if high is None or low is None or prev_close is None:
            continue
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
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
    def worker(stock: dict):
        session = create_moex_session()
        ticker = stock["ticker"]
        candles = fetch_candles(ticker, session)

        stock.setdefault("sma", stock["last"])
        stock.setdefault("stdev", 0.0)
        stock.setdefault("z_score", 0.0)
        stock.setdefault("rsi", 50.0)
        stock.setdefault("atr", 0.0)
        stock.setdefault("atr_pct", 0.0)
        stock.setdefault("ema50", stock["last"])
        stock.setdefault("trend_up", True)
        stock.setdefault("highest_high", stock["last"])
        stock.setdefault("sma200", None)
        stock.setdefault("above_sma200", False)

        if len(candles) < 2:
            return

        closes = [c[1] for c in candles if c[1] is not None]
        highs  = [c[2] for c in candles if c[2] is not None]

        closes_hist = closes[:-1] if len(closes) > SMA_WINDOW_DAYS else closes
        if len(closes_hist) >= SMA_WINDOW_DAYS:
            sample = closes_hist[-SMA_WINDOW_DAYS:]
            sma = statistics.mean(sample)
            stdev = statistics.stdev(sample) if len(sample) > 1 else 0.0
            stock["sma"] = sma
            stock["stdev"] = stdev
            stock["z_score"] = (stock["last"] - sma) / stdev if stdev > 0 else 0.0

        stock["rsi"] = _rsi(closes, RSI_WINDOW_DAYS)

        atr = _atr(candles, ATR_WINDOW_DAYS)
        stock["atr"] = atr
        stock["atr_pct"] = (atr / stock["last"] * 100.0) if stock["last"] > 0 else 0.0

        ema50 = _ema(closes, TREND_EMA_PERIOD)
        stock["ema50"] = ema50
        stock["trend_up"] = stock["last"] > ema50

        n = min(TRAILING_LOOKBACK_DAYS, len(highs))
        stock["highest_high"] = max(highs[-n:]) if n > 0 else stock["last"]

        sma200 = _sma(closes, GOLD_MA_PERIOD)
        stock["sma200"] = sma200
        stock["above_sma200"] = (sma200 is not None and stock["last"] > sma200)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(worker, stocks))


# ==============================================================================
# 6. ЧИСТАЯ ПРИБЫЛЬ
# ==============================================================================

def net_proceeds(shares: int, sell_price: float, buy_price: float) -> dict:
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


# ==============================================================================
# 7. ИНИЦИАЛИЗАЦИЯ И СИНХРОНИЗАЦИЯ
# ==============================================================================

def init_position(ticker: str, pos: dict, stock: dict, state: dict) -> bool:
    atr = stock.get("atr", 0.0)
    if atr <= 0:
        return False
    buy = pos["buy_price"]
    count = pos["count"]
    lot = stock["lot_size"]
    is_gold = ticker in GOLD_TICKERS

    # Ядро (только для акций, не для золота)
    raw_core = pos.get("core_count", 0)
    if is_gold:
        core_count = 0
    else:
        core_count = min(raw_core, count) // lot * lot
        if core_count < 0:
            core_count = 0

    if is_gold:
        levels = GOLD_TAKE_PROFIT_LEVELS
        stop_atr = GOLD_STOP_ATR
    else:
        levels = TAKE_PROFIT_ATR_LEVELS
        stop_atr = INITIAL_STOP_ATR

    targets = [buy + atr * lvl["atr_mult"] for lvl in levels]
    initial_stop = buy - atr * stop_atr

    state["positions"][ticker] = {
        "entry_atr": atr,
        "entry_price": buy,
        "buy_price": buy,
        "original_count": count,
        "core_count": core_count,
        "targets": targets,
        "initial_stop": initial_stop,
        "tp1_hit": False,
        "sold": 0,
        "peak": max(buy, stock["last"], stock.get("high", stock["last"]), stock.get("highest_high", stock["last"])),
        "is_gold": is_gold,
    }

    if not is_gold and lot > 0:
        tradable = count - core_count
        if tradable < lot:
            print(f"[ИНФО] {ticker}: только ядро ({core_count} шт.), спутник отсутствует.")

    return True

def sync_state_with_portfolio(state: dict, portfolio: Dict[str, dict],
                              tracked: Dict[str, dict]) -> List[str]:
    changes = []
    pos_state = state["positions"]

    for ticker in list(pos_state.keys()):
        if ticker not in portfolio:
            del pos_state[ticker]
            changes.append(f"{ticker}: закрыта, удалена из state")

    for ticker, pos in portfolio.items():
        if ticker not in tracked:
            continue
        stock = tracked[ticker]
        atr = stock.get("atr", 0.0)
        if atr <= 0:
            continue

        lot = stock["lot_size"]
        pstate = pos_state.get(ticker)

        # Миграция: если в state нет core_count — добавляем без переинициализации
        if pstate is not None and "core_count" not in pstate:
            raw_core = pos.get("core_count", 0)
            if ticker in GOLD_TICKERS:
                core_count = 0
            else:
                core_count = min(raw_core, pos["count"]) // lot * lot if lot > 0 else 0
            pstate["core_count"] = max(0, core_count)
            changes.append(
                f"{ticker}: миграция, добавлено ядро {pstate['core_count']} шт."
            )

        if pstate is None:
            if init_position(ticker, pos, stock, state):
                p = pos_state[ticker]
                tradable = p["original_count"] - p["core_count"]
                changes.append(
                    f"{ticker}: новая, ATR={p['entry_atr']:.3f}, "
                    f"ядро={p['core_count']}, спутник={tradable}, "
                    f"TP1={p['targets'][0]:.3f}, Stop={p['initial_stop']:.3f}"
                )
            continue

        original = pstate.get("original_count", 0)
        sold = pstate.get("sold", 0)
        stored_buy = pstate.get("buy_price", 0.0)
        stored_core = pstate.get("core_count", 0)
        expected_remaining = original - sold

        count_diff = pos["count"] - expected_remaining
        buy_changed = abs(pos["buy_price"] - stored_buy) > 0.001

        # Пользователь нарушил ядро (count < core_count) — форсированная переинициализация
        user_broke_core = (pos["count"] < stored_core)

        # Если пользователь изменил core_count в portfolio.json — переинициализация
        new_core_from_file = pos.get("core_count", stored_core)
        if ticker in GOLD_TICKERS:
            new_core_from_file = 0
        else:
            new_core_from_file = min(new_core_from_file, pos["count"]) // lot * lot if lot > 0 else 0
        core_changed = (new_core_from_file != stored_core)

        if buy_changed or count_diff > 0 or user_broke_core or core_changed:
            old_desc = (f"было: count={expected_remaining}, "
                        f"buy={stored_buy:.3f}, sold={sold}, core={stored_core}")
            if init_position(ticker, pos, stock, state):
                state["notified_profit"] = [
                    k for k in state["notified_profit"]
                    if not k.startswith(f"{ticker}_")
                ]
                state["notified_stop"] = [
                    k for k in state["notified_stop"]
                    if not k.startswith(f"{ticker}_")
                ]
                state["notified_buffett"] = [
                    k for k in state.get("notified_buffett", [])
                    if not k.startswith(f"{ticker}_")
                ]
                p = pos_state[ticker]
                reason = []
                if buy_changed: reason.append("смена buy_price")
                if count_diff > 0: reason.append("докупка")
                if user_broke_core: reason.append("ядро нарушено вручную")
                if core_changed: reason.append("изменён core_count")
                changes.append(
                    f"{ticker}: переинициализация ({'; '.join(reason)}) "
                    f"[{old_desc}] → count={pos['count']}, "
                    f"core={p['core_count']}, "
                    f"buy={pos['buy_price']:.3f}, ATR={p['entry_atr']:.3f}"
                )
        elif count_diff < 0:
            # Ручная продажа — увеличиваем sold, но проверяем, что не трогаем ядро
            new_sold = original - pos["count"]
            pstate["sold"] = new_sold
            changes.append(
                f"{ticker}: ручная продажа {abs(count_diff)} шт., "
                f"sold={sold} → {new_sold}, остаток={pos['count']}, "
                f"ядро={pstate['core_count']}"
            )

    return changes


# ==============================================================================
# 8. АКЦИИ: TP и стоп
# ==============================================================================

def _tp_trigger_price(stock: dict) -> float:
    """Для TP-триггера — max(LAST, BID): если BID выше цели, ты можешь продать."""
    return max(stock["last"], stock.get("bid", stock["last"]))


def evaluate_profit_taking(ticker: str, pos: dict, stock: dict, state: dict) -> Optional[dict]:
    if ticker in GOLD_TICKERS:
        return None
    pstate = state["positions"].get(ticker)
    if not pstate:
        return None
    price = _tp_trigger_price(stock)
    lot = stock["lot_size"]
    if lot <= 0:
        return None

    targets = pstate["targets"]
    original_count = pstate["original_count"]
    core_count = pstate.get("core_count", 0)
    tradable_count = original_count - core_count  # спутник
    already_sold = pstate.get("sold", 0)

    if tradable_count <= 0:
        return None

    cumulative_pct = 0.0
    highest_hit = -1
    for i, (target, lvl) in enumerate(zip(targets, TAKE_PROFIT_ATR_LEVELS)):
        if price >= target:
            cumulative_pct += lvl["sell_pct"]
            highest_hit = i

    if highest_hit < 0:
        return None

    # Считаем от tradable (спутника), не от всего count. Округление к ближайшему лоту:
    target_sold = int((tradable_count * min(cumulative_pct, 0.90) / lot) + 0.5) * lot
    shares_to_sell = target_sold - already_sold
    if shares_to_sell < lot:
        return None

    # Максимум — остаток спутника (ядро не трогаем!)
    remaining_tradable = max(0, pos["count"] - core_count)
    shares_to_sell = min(shares_to_sell, remaining_tradable)
    shares_to_sell = (shares_to_sell // lot) * lot
    if shares_to_sell < lot:
        return None

    money = net_proceeds(shares_to_sell, price, pos["buy_price"])
    gain_pct = (price - pos["buy_price"]) / pos["buy_price"] * 100.0

    return {
        "ticker": ticker,
        "trigger": f"LEVEL_{highest_hit}",
        "level_idx": highest_hit,
        "price": price,
        "buy_price": pos["buy_price"],
        "gain_pct": gain_pct,
        "entry_atr": pstate["entry_atr"],
        "target_price": targets[highest_hit],
        "atr_mult": TAKE_PROFIT_ATR_LEVELS[highest_hit]["atr_mult"],
        "shares": shares_to_sell,
        "lots": shares_to_sell // lot,
        "sell_ratio": shares_to_sell / tradable_count * 100.0 if tradable_count > 0 else 0.0,
        "remaining_after": pos["count"] - shares_to_sell,
        "core_count": core_count,
        "peak": pstate.get("peak", price),
        **money,
    }


def evaluate_stop_loss(ticker: str, pos: dict, stock: dict, state: dict) -> Optional[dict]:
    if ticker in GOLD_TICKERS:
        return None
    pstate = state["positions"].get(ticker)
    if not pstate:
        return None
    price = stock["last"]
    atr = pstate["entry_atr"]
    if atr <= 0:
        return None

    buy = pos["buy_price"]
    peak = pstate.get("peak", price)
    tp1_hit = pstate.get("tp1_hit", False)
    core_count = pstate.get("core_count", 0)
    lot = stock["lot_size"]

    # Сколько можно продать: только спутник, ядро не трогаем, строго кратно лоту
    shares_to_sell = ((pos["count"] - core_count) // lot) * lot
    if shares_to_sell < lot:
        return None  # осталось только ядро — стоп не применяется

    initial_stop = pstate["initial_stop"]
    chandelier = peak - atr * TRAILING_ATR_MULT

    breakeven = None
    pre_tp1_breakeven = None

    if tp1_hit:
        breakeven = buy + atr * BREAKEVEN_PLUS_ATR
    elif peak >= buy + atr * PRE_TP1_PROTECT_ATR:
        pre_tp1_breakeven = buy + atr * PRE_TP1_PROTECT_PLUS_ATR

    components = [initial_stop, chandelier]
    if breakeven is not None:
        components.append(breakeven)
    if pre_tp1_breakeven is not None:
        components.append(pre_tp1_breakeven)
    effective_stop = max(components)

    if price > effective_stop:
        return None

    return {
        "ticker": ticker,
        "trigger": "STOP",
        "price": price,
        "stop": effective_stop,
        "initial_stop": initial_stop,
        "chandelier": chandelier,
        "breakeven": breakeven,
        "pre_tp1_breakeven": pre_tp1_breakeven,
        "buy_price": buy,
        "atr": atr,
        "shares": shares_to_sell,
        "lots": shares_to_sell // lot,
        "lot": lot,
        "core_count": core_count,
        "loss_pct": (price - buy) / buy * 100.0,
        "peak": peak,
    }


# ==============================================================================
# 9. АКЦИИ: сигнал на покупку
# ==============================================================================

def evaluate_buy_signal(stock: dict) -> Optional[dict]:
    if stock["change_pct"] > -BUY_DAY_DROP_PCT:
        return None
    if stock.get("z_score", 0.0) > BUY_Z_THRESHOLD:
        return None
    rsi = stock.get("rsi", 50.0)
    if rsi > BUY_RSI_THRESHOLD or rsi < 15.0:
        return None
    # Тренд: выше EMA(50) или долгосрочно выше SMA(200)
    if not (stock.get("trend_up", False) or stock.get("above_sma200", False)):
        return None
    if stock.get("atr", 0.0) <= 0:
        return None
    return stock


# ==============================================================================
# 10. ЗОЛОТО
# ==============================================================================

def evaluate_gold_buy(stock: dict) -> Optional[dict]:
    if not stock.get("is_gold"):
        return None
    if stock.get("sma200") is None or not stock.get("above_sma200", False):
        return None
    rsi = stock.get("rsi", 50.0)
    if rsi > GOLD_RSI_ENTRY or rsi < GOLD_RSI_FLOOR:
        return None
    if stock.get("atr", 0.0) <= 0:
        return None
    return stock


def evaluate_gold_sell(ticker: str, pos: dict, stock: dict, state: dict) -> Optional[dict]:
    if ticker not in GOLD_TICKERS:
        return None
    pstate = state["positions"].get(ticker)
    if not pstate:
        return None

    price = _tp_trigger_price(stock)
    lot = stock["lot_size"]
    if lot <= 0:
        return None

    atr = pstate.get("entry_atr", 0.0)
    if atr <= 0:
        return None

    buy = pstate["buy_price"]
    original = pstate["original_count"]
    sold = pstate.get("sold", 0)
    peak = pstate.get("peak", price)
    tp1_hit = pstate.get("tp1_hit", False)

    targets = [buy + atr * lvl["atr_mult"] for lvl in GOLD_TAKE_PROFIT_LEVELS]
    cumulative = 0.0
    highest = -1
    for i, (target, lvl) in enumerate(zip(targets, GOLD_TAKE_PROFIT_LEVELS)):
        if price >= target:
            cumulative += lvl["sell_pct"]
            highest = i

    trigger = None
    shares_to_sell = 0
    level_idx = None

    if highest >= 0:
        target_sold = int(original * min(cumulative, 0.90) / lot) * lot
        shares_to_sell = target_sold - sold
        if shares_to_sell >= lot:
            trigger = f"GOLD_LEVEL_{highest}"
            level_idx = highest

    if trigger is None:
        chandelier = peak - atr * GOLD_TRAILING_ATR
        breakeven = buy + atr * GOLD_BREAKEVEN_PLUS_ATR if tp1_hit else None
        hard_stop = buy - atr * GOLD_STOP_ATR

        components = [hard_stop, chandelier]
        if breakeven is not None:
            components.append(breakeven)
        effective_stop = max(components)

        if stock["last"] <= effective_stop and pos["count"] > 0:
            trigger = "GOLD_STOP"
            shares_to_sell = pos["count"]
            level_idx = "STOP"

    if not trigger or shares_to_sell < lot:
        return None

    shares_to_sell = min(shares_to_sell, pos["count"])
    shares_to_sell = (shares_to_sell // lot) * lot
    if shares_to_sell < lot:
        return None

    money = net_proceeds(shares_to_sell, price, buy)
    gain_pct = (price - buy) / buy * 100.0

    return {
        "ticker": ticker,
        "trigger": trigger,
        "level_idx": level_idx,
        "price": price,
        "buy_price": buy,
        "gain_pct": gain_pct,
        "entry_atr": atr,
        "target_price": targets[level_idx] if isinstance(level_idx, int) else None,
        "atr_mult": GOLD_TAKE_PROFIT_LEVELS[level_idx]["atr_mult"] if isinstance(level_idx, int) else None,
        "shares": shares_to_sell,
        "lots": shares_to_sell // lot,
        "sell_ratio": shares_to_sell / original * 100.0,
        "remaining_after": pos["count"] - shares_to_sell,
        "peak": peak,
        "is_gold": True,
        **money,
    }


# ==============================================================================
# 11. TELEGRAM
# ==============================================================================

def send_telegram_message(html_text: str) -> bool:
    if is_test_mode():
        print(f"\n[TELEGRAM ТЕСТ — токен не задан]\n{_strip_html(html_text)}\n"
              + "-" * 60)
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": html_text, "parse_mode": "HTML"}
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


# ==============================================================================
# 12. ФОРМАТИРОВАНИЕ
# ==============================================================================

def format_profit_message(p: dict) -> str:
    level_names = {0: "TP1", 1: "TP2", 2: "TP3"}
    name = level_names.get(p["level_idx"], "TP")
    core = p.get("core_count", 0)
    core_line = f"🏛️ Ядро {core} шт. остаётся\n" if core > 0 else ""

    msg = (
        f"🎯 <b>{name} — {p['ticker']}</b>\n"
        f"Цена: {p['price']:.2f} ₽ | Покупка: {p['buy_price']:.2f} ₽ | +{p['gain_pct']:.2f}%\n"
        f"Цель {name}: {p['atr_mult']} ATR = {p['target_price']:.2f} ₽ (ATR={p['entry_atr']:.2f})\n"
        f"💼 Продать: <b>{p['shares']} шт.</b> ({p['lots']} лот., {p['sell_ratio']:.1f}% спутника)\n"
        f"💵 Выручка: {p['gross']:,.2f} ₽\n"
        f"📉 Комиссия: {p['commission']:,.2f} ₽ | Налог: {p['tax']:,.2f} ₽\n"
        f"✅ Чистая прибыль: <b>{p['net_profit']:,.2f} ₽</b>\n"
        f"{core_line}"
        f"Остаток: {p['remaining_after']} шт."
    )
    if p["level_idx"] == 0:
        msg += "\n🛡️ После продажи стоп переносится в безубыток."
    return msg


def format_gold_profit_message(p: dict) -> str:
    if p["trigger"] == "GOLD_STOP":
        return (
            f"🛑 <b>СТОП-ЛОСС (золото) — {p['ticker']}</b>\n"
            f"Цена: {p['price']:.3f} ₽ | Покупка: {p['buy_price']:.3f} ₽ | {p['gain_pct']:+.2f}%\n"
            f"Пик: {p['peak']:.3f} ₽ | ATR={p['entry_atr']:.3f}\n"
            f"💼 Рекомендация: <b>продать всю позицию</b> — {p['shares']} шт."
        )
    level_names = {0: "TP1", 1: "TP2", 2: "TP3"}
    name = level_names.get(p["level_idx"], "TP")
    return (
        f"🟡 <b>{name} (золото) — {p['ticker']}</b>\n"
        f"Цена: {p['price']:.3f} ₽ | Покупка: {p['buy_price']:.3f} ₽ | +{p['gain_pct']:.2f}%\n"
        f"Цель {name}: {p['atr_mult']} ATR = {p['target_price']:.3f} ₽\n"
        f"💼 Продать: <b>{p['shares']} шт.</b> ({p['lots']} лот., {p['sell_ratio']:.1f}%)\n"
        f"💵 Выручка: {p['gross']:,.2f} ₽\n"
        f"📉 Комиссия: {p['commission']:,.2f} ₽ | Налог: {p['tax']:,.2f} ₽\n"
        f"✅ Чистая прибыль: <b>{p['net_profit']:,.2f} ₽</b>\n"
        f"Остаток: {p['remaining_after']} шт."
    )


def format_stop_message(p: dict) -> str:
    price = p["price"]
    stop = p["stop"]
    buy = p["buy_price"]
    pre = p.get("pre_tp1_breakeven")
    core = p.get("core_count", 0)

    if pre is not None and abs(stop - pre) < 0.01:
        base_type = f"pre-TP1 защита ({pre:.2f} ₽ = buy + {PRE_TP1_PROTECT_PLUS_ATR} ATR)"
    elif p["breakeven"] and abs(stop - p["breakeven"]) < 0.01:
        base_type = f"безубыток ({p['breakeven']:.2f} ₽)"
    elif abs(stop - p["chandelier"]) < 0.01:
        base_type = f"Chandelier Exit (пик {p['peak']:.2f} − {TRAILING_ATR_MULT} ATR)"
    else:
        base_type = f"начальный стоп (покупка − {INITIAL_STOP_ATR} ATR)"

    breach_pct = (price - stop) / stop * 100.0 if stop > 0 else 0.0
    is_breached = breach_pct < -0.5

    if is_breached:
        type_line = f"Тип: {base_type} — <b>ПРОБИТ</b>"
        breach_line = (
            f"⚠️ Цена уже ниже стопа на {abs(price - stop):.2f} ₽ "
            f"({breach_pct:+.2f}%)\n"
            f"Фактическое исполнение — по рынку, ~{price:.2f} ₽\n"
        )
    else:
        type_line = f"Тип: {base_type}"
        breach_line = ""

    core_line = ""
    if core > 0:
        core_line = f"🏛️ Ядро {core} шт. остаётся (не продаётся)\n"

    return (
        f"🛑 <b>СТОП-ЛОСС — {p['ticker']}</b>\n"
        f"Цена: {price:.2f} ₽ | Стоп-уровень: {stop:.2f} ₽\n"
        f"{type_line}\n"
        f"{breach_line}"
        f"Покупка: {buy:.2f} ₽ | P&L: {p['loss_pct']:+.2f}%\n"
        f"💼 Рекомендация: <b>продать спутник</b> — {p['shares']} шт. ({p.get('lots', p['shares'] // p.get('lot', 1))} лот.)\n"
        f"{core_line}"
    )


def format_buy_message(s: dict, cash: float, total_equity: float,
                       current_value: float = 0.0,
                       additional_allowed: Optional[float] = None,
                       is_reload: bool = False,
                       core_count: int = 0) -> str:
    price = s["last"]
    lot = s["lot_size"]
    atr = s["atr"]
    lot_cost = price * lot

    stop_price = price - INITIAL_STOP_ATR * atr
    stop_pct = INITIAL_STOP_ATR * atr / price * 100.0

    risk_amount = total_equity * RISK_PER_TRADE_PCT
    position_by_risk = risk_amount / (stop_pct / 100.0) if stop_pct > 0 else 0.0
    position_by_max = additional_allowed if additional_allowed is not None else total_equity * MAX_POSITION_PCT
    position_by_cash = cash
    position = min(position_by_risk, position_by_max, position_by_cash)

    lots_est = int(position // lot_cost) if lot_cost > 0 else 0
    shares_est = lots_est * lot

    if is_reload:
        header = "СИГНАЛ НА ВОССТАНОВЛЕНИЕ СПУТНИКА (RELOAD)"
    elif current_value > 0:
        header = "СИГНАЛ НА ДОКУПКУ (пирамидинг)"
    else:
        header = "СИГНАЛ НА ПОКУПКУ"

    extra_info = ""
    if is_reload:
        extra_info = (
            f"\n🏛️ Ядро {core_count} шт. сохраняется (спутник был ранее зафиксирован).\n"
            f"🔄 Перезаход в спутник на откате в тренде!\n"
            f"📊 Текущая позиция (ядро): ~{current_value:,.0f} ₽ "
            f"({current_value / total_equity * 100:.1f}% капитала)\n"
        )
    elif current_value > 0:
        extra_info = (
            f"\n📊 Текущая позиция: ~{current_value:,.0f} ₽ "
            f"({current_value / total_equity * 100:.1f}% капитала)\n"
            f"📈 До лимита {MAX_POSITION_PCT*100:.0f}%: ~{additional_allowed:,.0f} ₽\n"
        )

    tp_plan_lines = "\n".join(
        f"• TP{i+1} ({lvl['atr_mult']} ATR): {price + atr * lvl['atr_mult']:.2f} ₽"
        for i, lvl in enumerate(TAKE_PROFIT_ATR_LEVELS)
    )

    trend_mark = "выше EMA(50)" if s.get("trend_up") else "выше SMA(200)"

    return (
        f"🟢 <b>{header} — {s['ticker']}</b>\n"
        f"Падение за день: <b>{s['change_pct']:.2f}%</b>\n"
        f"Z-score: {s['z_score']:+.2f} | RSI: {s['rsi']:.1f} | Тренд: {trend_mark} ✓\n"
        f"Цена: {price:.2f} ₽ | лот: {lot} шт.\n"
        f"ATR: {atr:.2f} ₽ ({s['atr_pct']:.2f}%)\n"
        f"{extra_info}\n"
        f"<b>План по ATR:</b>\n"
        f"• Стоп ({INITIAL_STOP_ATR} ATR): {stop_price:.2f} ₽ (−{stop_pct:.2f}%)\n"
        f"{tp_plan_lines}\n\n"
        f"<b>Размер покупки (риск {RISK_PER_TRADE_PCT*100:.1f}% = {risk_amount:,.0f} ₽):</b>\n"
        f"• {lots_est} лот. ({shares_est} шт., ~{shares_est * price:,.0f} ₽)"
    )


def format_gold_buy_message(s: dict, current_value: float, additional: float,
                            total_equity: float) -> str:
    price = s["last"]
    atr = s["atr"]
    lot = s["lot_size"]
    lot_cost = price * lot
    lots = int(additional // lot_cost) if lot_cost > 0 else 0

    stop_price = price - GOLD_STOP_ATR * atr
    tp1 = price + atr * GOLD_TAKE_PROFIT_LEVELS[0]["atr_mult"]
    tp2 = price + atr * GOLD_TAKE_PROFIT_LEVELS[1]["atr_mult"]
    tp3 = price + atr * GOLD_TAKE_PROFIT_LEVELS[2]["atr_mult"]

    header = "ЗОЛОТО: ДОКУПКА" if current_value > 0 else "ЗОЛОТО: ПОКУПКА"
    pos_line = (f"\n📊 Текущая позиция: ~{current_value:,.0f} ₽ "
                f"({current_value / total_equity * 100:.1f}% капитала)") if current_value > 0 else ""

    return (
        f"🟡 <b>{header} — {s['ticker']}</b>\n"
        f"RSI: {s['rsi']:.1f} (откат в тренде) | Цена > SMA200 ✓\n"
        f"Цена: {price:.3f} ₽ | лот: {lot} шт.\n"
        f"ATR: {atr:.3f} ₽ ({s['atr_pct']:.2f}%)\n"
        f"{pos_line}\n"
        f"<b>План (трендовая логика):</b>\n"
        f"• Стоп (3 ATR): {stop_price:.3f} ₽\n"
        f"• TP1 (2.5 ATR): {tp1:.3f} ₽\n"
        f"• TP2 (4.5 ATR): {tp2:.3f} ₽\n"
        f"• TP3 (7.0 ATR): {tp3:.3f} ₽\n\n"
        f"<b>Лимит на золото {GOLD_MAX_POSITION_PCT*100:.0f}%:</b>\n"
        f"Доступно ~{additional:,.0f} ₽ → ~{lots} лот. (~{lots * lot} шт.)"
    )


# ==============================================================================
# 13. ТОРГОВЫЕ ЧАСЫ
# ==============================================================================

def is_market_open() -> bool:
    now = datetime.now(MSK_TZ)
    if now.weekday() >= 5:
        return False
    return SESSION_START <= now.time() <= SESSION_END


# ==============================================================================
# 14. ДАШБОРД
# ==============================================================================

def print_dashboard(portfolio: Dict[str, dict], tracked: Dict[str, dict],
                    cash: float, state: dict):
    print("\n" + "=" * 148)
    print(f"{'Тикер':<6} | {'Всего':>7} | {'Ядро':>5} | {'Спут.':>6} | "
          f"{'Покупка':>9} | {'Рынок':>9} | {'День%':>7} | {'Доход%':>7} | "
          f"{'P&L,₽':>9} | {'Z':>5} | {'RSI':>5} | {'Стоп':>9} | "
          f"{'TP1':>9} | {'TP2':>9} | {'TP3':>9}")
    print("-" * 148)

    total_val = 0.0
    total_cost = 0.0
    for t, pos in portfolio.items():
        st = tracked.get(t)
        if not st:
            print(f"{t:<6} | {pos['count']:>7} | НЕТ ДАННЫХ")
            continue
        cur = st["last"]
        buy = pos["buy_price"]
        cnt = pos["count"]
        gain = (cur - buy) / buy * 100.0 if buy > 0 else 0.0
        pnl = (cur - buy) * cnt
        total_val += cur * cnt
        total_cost += buy * cnt

        pstate = state["positions"].get(t, {})
        core = pstate.get("core_count", 0)
        tradable = cnt - core
        stop = 0.0
        tp1 = tp2 = tp3 = 0.0
        if pstate:
            peak = pstate.get("peak", cur)
            atr = pstate.get("entry_atr", 0.0)
            targets = pstate.get("targets", [])
            if t in GOLD_TICKERS:
                chand = peak - atr * GOLD_TRAILING_ATR
                breakeven = buy + atr * GOLD_BREAKEVEN_PLUS_ATR if pstate.get("tp1_hit") else None
                comps = [pstate.get("initial_stop", 0.0), chand]
            else:
                chand = peak - atr * TRAILING_ATR_MULT
                breakeven = buy + atr * BREAKEVEN_PLUS_ATR if pstate.get("tp1_hit") else None
                pre_tp1 = (buy + atr * PRE_TP1_PROTECT_PLUS_ATR
                           if (not pstate.get("tp1_hit")
                               and peak >= buy + atr * PRE_TP1_PROTECT_ATR)
                           else None)
                comps = [pstate.get("initial_stop", 0.0), chand]
                if pre_tp1 is not None:
                    comps.append(pre_tp1)
            if breakeven is not None:
                comps.append(breakeven)
            stop = max(comps)
            if len(targets) >= 3:
                tp1, tp2, tp3 = targets[0], targets[1], targets[2]

        print(f"{t:<6} | {cnt:>7} | {core:>5} | {tradable:>6} | "
              f"{buy:>9.2f} | {cur:>9.2f} | "
              f"{st['change_pct']:>+6.2f}% | {gain:>+6.2f}% | {pnl:>+8.2f} | "
              f"{st.get('z_score', 0.0):>+4.2f} | {st.get('rsi', 50.0):>5.1f} | "
              f"{stop:>9.2f} | {tp1:>9.2f} | {tp2:>9.2f} | {tp3:>9.2f}")

    print("-" * 148)
    total_pnl = total_val - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
    print(f"Активы: {total_val:,.2f} ₽ | Кэш: {cash:,.2f} ₽ | Капитал: {total_val + cash:,.2f} ₽")
    print(f"P&L: {total_pnl:+,.2f} ₽ ({total_pnl_pct:+.2f}%)")
    print("=" * 148 + "\n")


# ==============================================================================
# 15. ИТЕРАЦИЯ
# ==============================================================================

def run_iteration(state: dict) -> bool:
    now = datetime.now(MSK_TZ)
    ts = now.strftime("%H:%M:%S")

    today_str = now.strftime("%Y-%m-%d")
    if state.get("notified_date") != today_str:
        state["notified_date"] = today_str
        state["notified_profit"] = []
        state["notified_buy"] = []
        state["notified_stop"] = []
        state["notified_buffett"] = []
        print(f"[{ts}] Новый день — история in-memory уведомлений сброшена.")

    portfolio, cash = load_portfolio(PORTFOLIO_FILE)

    print(f"[{ts}] Запрос рыночных данных MOEX...")
    try:
        all_stocks, liquid = get_moex_market_data()
    except Exception as e:
        print(f"[{ts}] Ошибка получения данных: {e}")
        return False
    if not all_stocks:
        print(f"[{ts}] Пустой ответ MOEX.")
        return False

    tracked: Dict[str, dict] = {}
    for t in portfolio:
        if t in all_stocks:
            tracked[t] = all_stocks[t]
        else:
            print(f"[{ts}] [ВНИМАНИЕ] {t} нет в TQBR.")
    for s in liquid:
        tracked.setdefault(s["ticker"], s)

    enrich_with_indicators(list(tracked.values()))

    changes = sync_state_with_portfolio(state, portfolio, tracked)
    for ch in changes:
        print(f"[{ts}] [SYNC] {ch}")

    # Проверка качества бизнеса по Баффету (раз в BUFFETT_AUDIT_INTERVAL_DAYS дней)
    last_audit_str = state.get("last_buffett_audit", "")
    need_audit = False
    if not last_audit_str:
        need_audit = True
    else:
        try:
            last_d = datetime.strptime(last_audit_str, "%Y-%m-%d").date()
            if (now.date() - last_d).days >= BUFFETT_AUDIT_INTERVAL_DAYS:
                need_audit = True
        except Exception:
            need_audit = True

    if need_audit:
        run_buffett_audit(portfolio, tracked, state)
        state["last_buffett_audit"] = today_str

    # Обновление пиков — по текущей цене и максимуму дня (HIGH)
    for t, st in tracked.items():
        if t in state["positions"]:
            cur_peak = state["positions"][t]["peak"]
            state["positions"][t]["peak"] = max(cur_peak, st["last"], st.get("high", st["last"]))

    print_dashboard(portfolio, tracked, cash, state)

    # ---- 1. СТОП-ЛОСС (акции) ----
    for t, pos in portfolio.items():
        if t in GOLD_TICKERS:
            continue
        st = tracked.get(t)
        if not st or t not in state["positions"]:
            continue
        plan = evaluate_stop_loss(t, pos, st, state)
        if not plan:
            continue
        key = f"{t}_STOP"
        if key in state["notified_stop"]:
            continue
        if dispatch_notification(key, "STOP", t, format_stop_message(plan)):
            print(f"[{ts}] [СТОП] {t}: price={st['last']:.2f} ≤ stop={plan['stop']:.2f}")
            state["notified_stop"].append(key)

    # ---- 2. ФИКСАЦИЯ ПРИБЫЛИ (акции) ----
    for t, pos in portfolio.items():
        if t in GOLD_TICKERS:
            continue
        st = tracked.get(t)
        if not st or t not in state["positions"]:
            continue
        plan = evaluate_profit_taking(t, pos, st, state)
        if not plan:
            continue
        key = f"{t}_{plan['trigger']}"
        if key in state["notified_profit"]:
            continue
        if dispatch_notification(key, "PROFIT", t, format_profit_message(plan)):
            print(f"[{ts}] [ФИКСАЦИЯ] {t}: {plan['trigger']}, "
                  f"{plan['shares']} шт., чистая {plan['net_profit']:.2f} ₽")
            state["notified_profit"].append(key)
            state["positions"][t]["sold"] += plan["shares"]
            state["positions"][t]["tp1_hit"] = True

    # ---- 3. ЗОЛОТО: продажа ----
    for t, pos in portfolio.items():
        if t not in GOLD_TICKERS:
            continue
        st = tracked.get(t)
        if not st or t not in state["positions"]:
            continue
        plan = evaluate_gold_sell(t, pos, st, state)
        if not plan:
            continue
        key = f"{t}_{plan['trigger']}"
        if key in state["notified_profit"]:
            continue
        if dispatch_notification(key, "GOLD", t, format_gold_profit_message(plan)):
            print(f"[{ts}] [ЗОЛОТО] {t}: {plan['trigger']}, "
                  f"{plan['shares']} шт., чистая {plan['net_profit']:.2f} ₽")
            state["notified_profit"].append(key)
            state["positions"][t]["sold"] += plan["shares"]
            if isinstance(plan["level_idx"], int):
                state["positions"][t]["tp1_hit"] = True

    portfolio_value = sum(
        tracked[t]["last"] * pos["count"]
        for t, pos in portfolio.items() if t in tracked
    )
    total_equity = cash + portfolio_value

    # ---- 4. ЗОЛОТО: покупка ----
    for s in liquid:
        t = s["ticker"]
        if t not in GOLD_TICKERS:
            continue
        st = tracked.get(t)
        if not st or t in state["notified_buy"]:
            continue
        signal = evaluate_gold_buy(st)
        if not signal:
            continue

        gold_current = sum(
            tracked[g]["last"] * portfolio[g]["count"]
            for g in GOLD_TICKERS if g in portfolio and g in tracked
        )
        gold_max = total_equity * GOLD_MAX_POSITION_PCT
        additional = gold_max - gold_current
        lot_cost = st["last"] * st["lot_size"]
        if additional < lot_cost:
            continue

        msg = format_gold_buy_message(signal, gold_current, additional, total_equity)
        if dispatch_notification(f"{t}_BUY", "GOLD_BUY", t, msg):
            print(f"[{ts}] [ЗОЛОТО-ПОКУПКА] {t}: RSI={st['rsi']:.1f}, "
                  f"цена={st['last']:.3f}, доп={additional:.0f}₽")
            state["notified_buy"].append(t)

    # ---- 5. АКЦИИ: покупка / докупка ----
    for s in liquid:
        t = s["ticker"]
        if t in GOLD_TICKERS:
            continue
        st = tracked.get(t)
        if not st or t in state["notified_buy"]:
            continue
        signal = evaluate_buy_signal(st)
        if not signal:
            continue

        current_value = 0.0
        tradable_existing = 0
        is_reload = False
        core_cnt = 0
        if t in portfolio:
            pos = portfolio[t]
            core_cnt = pos.get("core_count", 0)
            tradable_existing = max(0, pos["count"] - core_cnt)
            current_value = pos["count"] * st["last"]

            # Если спутник УЖЕ открыт и торгуется, докупаем только в прибыль (пирамидинг)
            if tradable_existing > 0:
                if st["last"] <= pos["buy_price"]:
                    continue
            else:
                # Если же спутника нет (осталось только ядро), это перезаход в спутник на откате
                is_reload = True

        max_allowed = total_equity * MAX_POSITION_PCT
        additional_allowed = max_allowed - current_value
        lot_cost = st["last"] * st["lot_size"]
        if additional_allowed < lot_cost:
            continue

        msg = format_buy_message(signal, cash, total_equity,
                                 current_value, additional_allowed,
                                 is_reload=is_reload, core_count=core_cnt)
        if is_reload:
            kind = "RELOAD"
        elif current_value > 0:
            kind = "ДОКУПКА"
        else:
            kind = "ПОКУПКА"

        if dispatch_notification(f"{t}_BUY", kind, t, msg):
            print(f"[{ts}] [{kind}] {t}: {st['change_pct']:+.2f}%, "
                  f"Z={st['z_score']:+.2f}, RSI={st['rsi']:.1f}, "
                  f"ATR%={st['atr_pct']:.2f}, доп={additional_allowed:.0f}₽")
            state["notified_buy"].append(t)

    save_state(state)
    return True


# ==============================================================================
# 16. MAIN
# ==============================================================================

def print_header():
    print("=" * 75)
    print(" MOEX Stock Tracker v3.6 — ATR + золото + журнал + pre-TP1 + ядро ")
    print("=" * 75)
    print(f"Акции: TP " + ", ".join(
        f"{l['atr_mult']}ATR→{int(l['sell_pct']*100)}%" for l in TAKE_PROFIT_ATR_LEVELS))
    print(f"  стоп {INITIAL_STOP_ATR} ATR | после TP1 → безубыток +{BREAKEVEN_PLUS_ATR} ATR")
    print(f"  pre-TP1: при peak ≥ buy + {PRE_TP1_PROTECT_ATR} ATR → стоп buy + {PRE_TP1_PROTECT_PLUS_ATR} ATR")
    print(f"  Chandelier: пик − {TRAILING_ATR_MULT} ATR")
    print(f"Золото: SMA{GOLD_MA_PERIOD} + RSI<{GOLD_RSI_ENTRY} | TP " + ", ".join(
        f"{l['atr_mult']}ATR→{int(l['sell_pct']*100)}%" for l in GOLD_TAKE_PROFIT_LEVELS))
    print(f"  стоп {GOLD_STOP_ATR} ATR | Chandelier {GOLD_TRAILING_ATR} ATR | "
          f"лимит {GOLD_MAX_POSITION_PCT*100:.0f}%")
    print(f"Риск: {RISK_PER_TRADE_PCT*100:.1f}% | Макс. позиция: {MAX_POSITION_PCT*100:.0f}%")
    print(f"Комиссия: {COMMISSION_PCT}% | НДФЛ: {TAX_PCT}% | TOP_N: {TOP_N_LIQUID}")
    print(f"Интервал: {CHECK_INTERVAL_SECONDS}с")
    print(f"Файлы: {PORTFOLIO_FILE}, {STATE_FILE}, {NOTIFICATIONS_FILE}")
    print(f"Telegram: " + ("ТЕСТ (токен не задан)" if is_test_mode() else "включён"))
    print("=" * 75)


def main():
    if "--send-pending" in sys.argv:
        print_header()
        send_pending_notifications()
        return
    if "--show" in sys.argv:
        print_header()
        show_recent_notifications(30)
        return
    if "--audit-buffett" in sys.argv:
        print_header()
        all_stocks, liquid = get_moex_market_data()
        portfolio, cash = load_portfolio(PORTFOLIO_FILE)
        tracked = {t: all_stocks[t] for t in portfolio if t in all_stocks}
        for s in liquid:
            tracked.setdefault(s["ticker"], s)
        enrich_with_indicators(list(tracked.values()))
        state = load_state()
        run_buffett_audit(portfolio, tracked, state, force_notify=True)
        save_state(state)
        return

    print_header()

    state = load_state()
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
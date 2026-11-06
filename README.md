# MOEX Stock Tracker v4

Спекулятивный среднесрочный трекер MOEX TQBR на идеях ВТБ Инвестиций.
Скрипт не выставляет заявки: он считает риск, размер позиции и уровни, а решения
исполняются вручную, после чего обновляется [`portfolio.json`](portfolio.json).

## Что изменилось в v4

- **Ядро удалено.** Все акции торгуемые. Стоп, последняя ступень ладдера,
  истечение горизонта или инвалидация идеи закрывают всю позицию.
- **Новые покупки только по идеям ВТБ** из [`ideas.json`](ideas.json).
  Без активной идеи, зоны входа и положительного ожидания покупки нет.
- **Горизонт — недели/месяцы.** Идея живёт до `valid_until`; после горизонта
  позиция закрывается, если цель не достигнута.
- **Динамический ладдер по R и цели ВТБ.** TP1 = 1.0R, TP2 = 2.0R (если цель
  позволяет), финал = цель ВТБ. Последняя ступень продаёт весь остаток.
- **Cost-aware безубыток.** Комиссии, slippage и НДФЛ учитываются в R,
  безубытке и математическом ожидании.
- **Риск-движок.** Fixed-fractional, half-Kelly, portfolio heat, секторные и
  корреляционные лимиты, volatility targeting, circuit breakers.
- **Количественный скринер вместо субъективного аудита.** Value + quality +
  growth + momentum + risk + idea expectancy, sector-neutral z-scores и
  winsorization.
- **Fail-closed.** Повреждённые входные файлы не приводят к покупкам «с нуля».

## Философия

Стратегия максимизирует не абсолютную прибыль, а **математическое ожидание на
единицу риска**. Это единственная корректная форма компромисса «минимальный
риск — максимальная прибыль».

Используются устойчивые, опубликованные методы:

- кросс-секционные факторные модели (value, quality, momentum, low-volatility);
- value + momentum как защита от value traps;
- байесовское сжатие вероятности успеха;
- half-Kelly и fixed-fractional sizing;
- volatility targeting и корреляционные кластеры;
- cost-aware expectancy с НДФЛ и проскальзыванием.

Никаких чёрных ящиков и оптимизации весов на исторических данных: стратегия
проверяется форвардно через `--stats`.

## Математика

### R, R:R и ожидание

```text
plan_anchor = цена исполнения входа (ask, иначе last)
stop0       = max(idea.stop, entry_low - 1.0 * ATR)   если stop задан
              entry_low - 1.0 * ATR                    иначе
R           = plan_anchor - stop0
target_r    = (target - plan_anchor) / R
```

Жёсткие ворота входа:

```text
target_r >= 2.0
upside_pct >= 10%
risk_pct <= 18%
E_R >= 0.05 после комиссий, slippage и НДФЛ
```

Ожидание:

```text
c   = (COMMISSION_PCT + SLIPPAGE_PCT) / 100
tau = TAX_PCT / 100
net_win  = (target - anchor) * (1 - tau) - c * (target + anchor)
net_loss = R + c * (anchor + stop0)
E_R = p * (net_win / R) - (1 - p) * (net_loss / R)
```

Вероятность успеха `p` — байесовская:

```text
p = (wins + 4) / (wins + losses + 10)
p = clamp(p, 0.30, 0.55)
```

`conviction` из идеи используется только как tie-breaker и не увеличивает размер.

### Ладдер

Если `target_r >= 3.0`:

| Ступень | Уровень | Доля исходной позиции |
|---|---|---|
| TP1 | `anchor + 1.0R` | 40% |
| TP2 | `anchor + 2.0R` | 30% |
| TP3 | `target` | весь остаток |

Если `2.0 <= target_r < 3.0`:

| Ступень | Уровень | Доля исходной позиции |
|---|---|---|
| TP1 | `anchor + 1.0R` | 50% |
| TP2 | `target` | весь остаток |

Количества выравниваются по лоту вниз. Если остаток меньше лота, скрипт
предупреждает о ручной продаже.

### Стопы

- Начальный стоп — структурный или из идеи, не шире 18%.
- Chandelier: `peak - 3.0 ATR`, ATR не расширяется при росте волатильности.
- До TP1: при `peak >= anchor + 0.75R` стоп поднимается до `anchor + 0.25R`.
- После TP1: безубыток с учётом издержек и `anchor + 0.25R`.
- После TP2: `anchor + 1.0R`.
- `stop_current` только повышается.
- Стоп-триггер — по `min(last, bid)`, TP-триггер — по `bid`.

### Риск

| Лимит | Значение |
|---|---|
| Риск на сделку | 0.75% капитала |
| Макс. позиция | 15% |
| Portfolio heat | 5% |
| Сектор | 30% |
| Число позиций | 10 |
| Доля ADV | 1% |
| Резерв кэша | 10% |
| Волатильность портфеля | 25% годовых |
| Корреляционный кластер | 30% при `rho >= 0.75` |
| Стоп новых входов | просадка 10% или дневной убыток 3% |

## Файлы

| Файл | Назначение |
|---|---|
| [`portfolio.json`](portfolio.json) | Фактические позиции и `CASH` |
| [`ideas.json`](ideas.json) | Идеи ВТБ: зона входа, цель, стоп, горизонт, уверенность |
| [`fundamentals.json`](fundamentals.json) | P/E, P/B, EV/EBITDA, ROE, FCF, долг, рост, дивиденды |
| `state.json` | План позиций, стадии, стопы, пики, статистика (ведёт скрипт) |
| `notifications.json` | Журнал уведомлений и paper-сделок (ведёт скрипт) |
| [`ideas.example.json`](ideas.example.json) | Шаблон идеи |

`state.json` и `notifications.json` игнорируются git и пишутся атомарно:
`tmp` + `os.fsync()` + `os.replace()`.

### portfolio.json

```json
{
  "SBER": { "count": 14, "buy_price": 285.99 },
  "GOLD": { "count": 9652, "buy_price": 2.7855 },
  "CASH": 10448
}
```

Поле ядра больше не используется. `count <= 0` означает закрытую позицию.

### ideas.json

```json
{
  "_meta": { "schema_version": 1, "updated": "2026-11-06" },
  "SBER": {
    "name": "Сбербанк",
    "sector": "Банки",
    "source": "VTB My Investments",
    "published": "2026-11-01",
    "valid_until": "2027-04-30",
    "entry_low": 280.0,
    "entry_high": 300.0,
    "target": 380.0,
    "stop": 260.0,
    "conviction": 4,
    "active": true,
    "thesis": "Краткий тезис аналитика",
    "target_history": [
      { "date": "2026-11-01", "target": 380.0 }
    ]
  }
}
```

Требования:

- `entry_low <= entry_high`, `target > entry_high`, `stop < entry_low` если задан.
- `published < valid_until`, горизонт не больше 180 дней.
- `conviction` 1..5.
- Одна активная идея на тикер; для управления берётся последняя по `published`.
- `active=false` или истёкший `valid_until` — новых входов нет, позиция
  закрывается по thesis/time exit.

### fundamentals.json

```json
{
  "_meta": { "schema_version": 2, "updated": "2026-11-06" },
  "SBER": {
    "name": "Сбербанк",
    "sector": "Банки",
    "listing_level": 1,
    "pe": 4.5,
    "pb": 1.1,
    "ev_ebitda": null,
    "roe": 24.5,
    "fcf_yield": 12.0,
    "fcf_positive": true,
    "net_debt_ebitda": 0.0,
    "eps_growth": 8.0,
    "revenue_growth": 5.0,
    "div_yield": 9.0,
    "earnings_stability": 0.85
  }
}
```

Все доходности и роста — в процентах (`24.5`, а не `0.245`). `pe`, `pb`,
`ev_ebitda` — положительные мультипликаторы; `null` для неприменимых (например,
EV/EBITDA у банков). `net_debt_ebitda` может быть отрицательным.

Данные старше 120 дней — предупреждение, старше 180 дней — новые входы по
тикеру запрещены. После миграции заполните `pe`, `pb`, `fcf_yield`, `eps_growth`,
`revenue_growth`, `div_yield`.

## Команды

| Команда | Назначение |
|---|---|
| `python moex_tracker.py` | основной цикл |
| `python moex_tracker.py --once` | одна диагностическая итерация |
| `python moex_tracker.py --show` | последние уведомления |
| `python moex_tracker.py --send-pending` | дослать неотправленное в Telegram |
| `python moex_tracker.py --screen` | количественный скрининг |
| `python moex_tracker.py --ideas-check` | валидация идей и фундаментала |
| `python moex_tracker.py --stats` | paper-статистика сделок |
| `python moex_tracker.py --migrate` | миграция v3 → v4 с backup |
| `python moex_tracker.py --reset-plan TICKER` | пересчитать план от текущей цены |
| `python test_fixes.py` | самотесты |

## Миграция v3 → v4

Перед первым запуском:

```bash
python moex_tracker.py --migrate
```

Команда:

1. Сохраняет backup `portfolio.json.v3.bak`, `fundamentals.json.v3.bak`,
   `state.json.v3.bak`.
2. Удаляет поле ядра из `portfolio.json`.
3. Переводит `fundamentals.json` на схему v2, сохраняя ROE, долг и FCF.
4. Сбрасывает `state.json` в v4.
5. Создаёт пустой `ideas.json`; шаблон — [`ideas.example.json`](ideas.example.json).

После миграции заполните `ideas.json` и `fundamentals.json`, затем:

```bash
python moex_tracker.py --ideas-check
python moex_tracker.py --screen
python moex_tracker.py
```

Legacy-позиции без идеи ведутся технически до выхода: стоп 3 ATR, цели
2/4/6 ATR, максимум 90 дней. `GOLD` и другие БПИФ — только legacy, новых
покупок нет.

## Ограничения

- Рекомендации ВТБ вносятся вручную; автоматического парсинга приложения нет.
- Фундаментальные мультипликаторы вносятся вручную и обновляются ежеквартально.
- Дивиденды в ожидании не учитываются — консервативно.
- Налоговый вычет по убыткам не моделируется — консервативно.
- Проскальзывание учитывается в воротах ожидания, но не в отчётном P&L.
- Скрипт не выставляет заявки и не гарантирует прибыль.
- Стратегия оценивается форвардно через `--stats`, а не подгонкой на истории.

## Дисклеймер

Это инструмент риск-менеджмента и расчёта, а не инвестиционная рекомендация.
Решение об исполнении принимает владелец капитала. Перед реальными сделками
проверьте `--ideas-check`, `--screen` и `python test_fixes.py`.

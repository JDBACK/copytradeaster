# План: PnL с учётом комиссий (Mini App + Telegram)

Дата: 2026-09-13. Терминал: `c3a99b31`. Статус: **P0–P4 закрыты, P5 свежесть
доков поддержана; открыт только продуктовый вопрос про не-USDT комиссию**.

## Не проверено живьём

- Mini App HTTP-поверхность (trades/detail/feed) в раундах 5–6 не гонялась на
  живом контуре: net-карточка доказана живым Telegram-прогоном и офлайн-тестами
  (`tests/unit/application/test_trade_money_c5.py`, `miniapp/src/tradeResult.test.tsx`),
  а замеры чтения денег — на копии прод-БД. Это и есть вход в live-задание на
  frontend/Mini App.

## Исходная проблема (снимок до кампании)

PnL ни на одном экране не учитывал затраты:

- открытая позиция — сырое `unRealizedProfit` из `positionRisk` (mark-to-market, без комиссий и funding);
- закрытая сделка — `realized_pnl_gross_usdt` с подписью «PnL до комиссий».

Backend при этом уже умел считать чистый результат (`history/accounting.py`,
`EpisodeMoney.net`), а UI умел его показывать (`result_state === 'net_complete'`
→ «Итог сделки»). Он не включался по трём причинам, и все три — наши, не API.

## Границы (решения владельца)

1. Обе поверхности: **Mini App и Telegram** (уведомления + карточки).
2. Открытая позиция остаётся биржевым числом: mark-to-market, как в приложении
   Aster. Никаких пояснений рядом с числом.
3. Закрытая сделка показывает **одно** число — net (gross ± все подтверждённые
   начисления). Одно число, без второго «до комиссий» рядом.
4. Тест-покрытие: **живой тестнет** (фейки не считаются доказательством).
   Тестнет-средства фиктивные, операции разрешены.

## Что именно мешало (факты из кода, снимок до работ)

| # | Препятствие | Где | Чем закрыто |
|---|---|---|---|
| B1 | Для `identity_source='builder'` политика комиссии `unknown` → `net=None` **до** расчёта | `storage/history.py` | P0 + миграция #64: durable-штамп замера (`builder_fee_policy`) |
| B2 | `AccountingProof.builder_complete` захардкожен `False`, `builder_income_types` нигде не заполняется | `history/accounting.py`, `application/service.py` | P1a: политика читается из штампа; отдельный `builder_income_types` не понадобился (P0 доказал, что flow нет) |
| B3 | Окно income всегда `now−7 дней`; эпизоды старше — без начислений навсегда | `history/worker.py` | P2: `history/backfill.py` + CLI `backfill-income` |
| B4 | `load_episode_money` читает все окна и все flow скоупа — после backfill это деградация чтения | `history/accounting.py` | P4: границы выборки + `cashflows_span_for_trades` + индекс #65 |
| B5 | Telegram-карточки печатают только `realized_gross` и слова «до комиссий» | `notifications/trade_cards.py` | P1a/P3: `notifications/money.py` + send-time перерендер |

Номера строк в колонке «Где» — снимок на момент плана; после правок они
сдвинулись, актуальные ориентиры — имена модулей и разделы журнала ниже.

## Этапы

### P0. Проба источника builder fee (блокирующий факт) — ЗАКРЫТА ФАКТОМ

`scripts/probe_builder_fee_source.py` — на тестнете делает round-trip
(builder-attributed, `feeRate=0.0001`) и сверяет три источника.

**Результат (REAL Aster Testnet, copyc2/BTCUSDT, 2026-09-13):**

| Прогон | Ставка комиссии по филлам | `COMMISSION` income |
|---|---|---|
| `builder`+`feeRate=0.0001` | 0.000500 (0,05%) | `-commission`, ровно по каждому `tradeId` |
| без attribution (контроль) | 0.000400 (0,04%) | `-commission`, ровно по каждому `tradeId` |

Разница = `0.000100` на ноционал 154.41 USDT, то есть **ровно `feeRate`**;
отдельного `incomeType` для builder fee нет — дополнительных списаний в
`income` не появилось (`REALIZED_PNL` + `COMMISSION` и всё).

**Вердикт: `included_in_commission`.** Комиссия Builder списывается внутри
обычного `COMMISSION`-потока, отдельного cashflow не имеет. Значит:

- `net = gross + COMMISSION + FUNDING_FEE` уже полон и для builder-аккаунтов;
- gate `builder_policy=unknown` (`storage/history.py:624`) можно снимать —
  но только по durable-доказательству, а не по этой записи в доке;
- `builder/userTrades` на тестнете недоступен без агентского кошелька самого
  Builder (`-1000 No agent found`), поэтому формальная проба строит вердикт из
  пары attributed/control, а не из builder-стороны.

Вердикт: P1 выполняется как **P1a** (без миграции и без отдельной компоненты).

### P1. Снять B1/B2 по факту P0

- P1a: builder fee внутри `COMMISSION` → `builder_policy` возвращает
  `included_in_commission` для builder-аккаунтов с durable-доказательством.
- P1b: отдельный поток → миграция под политику + `builder_income_types` на шве
  композиции + отдельная компонента (`MoneyComponent` уже готова).

Durable-доказательство обязательно: политика без провенанса = догадка.

### P2. Backfill income (B3) + ограничение чтения (B4)

- CLI `copytradeaster history backfill-income --account <id> --from <date>`:
  7-дневные окна через существующие `HistoryReader.income()` + `commit_income()`,
  resumable (окна те же `history_sync_state`), вес 30/запрос — вне торгового цикла.
- `load_episode_money` ограничивает выборку интервалом эпизода.

### P3. Net везде (Mini App + Telegram)

- Mini App: закрытая карточка/детали — одно число net; открытая — без изменений
  (биржевое число), лишние подписи убрать.
- Telegram: `POSITION_CLOSED` получает доказанный net через порт
  `EpisodeMoneyReader` (`notifications/money.py`), который читает
  `EpisodeMoneyView` эпизода в момент рендера карточки; при неполной сверке
  карточка пишет «Итог уточняется», а не gross. `POSITION_REDUCED` сохраняет
  gross закрытой части: net эпизода включал бы затраты ещё открытого остатка.
  Отдельное событие `TRADE_RESULT_CONFIRMED` не понадобилось.

### P4. Живая приёмка (обязательна) — ЗАКРЫТА, кроме Mini App HTTP

1. `probe_builder_fee_source.py` — вердикт по builder fee (тестнет, факт). ✅
2. Фактическая форма вместо первоначального `test_net_pnl_acceptance_live.py`:
   `tests/testnet/test_live_net_close_card_acceptance.py` (обе ветки — gross для
   ASTER-комиссии и net для USDT-комиссии) + `tests/testnet/test_builder_fee_accounting_live.py`
   + `tests/unit/history/test_accounting_bounds.py` + `miniapp/src/tradeResult.test.tsx`. ✅
3. Flat-аудит в `finally`, `CT_ALLOW_MAINNET=false`, один аккаунт за раз. ✅
4. Осталось живьём: **Mini App HTTP** (trades/trade detail/feed) — вход в
   live-задание на frontend/Mini App, см. «Не проверено живьём» выше.

### P5. Документация

- `docs/ASTER_API_RESEARCH_2026-08.md`: `unRealizedProfit` = mark-to-market без
  комиссий/funding; `income` — 7 дней/запрос, `COMMISSION` в `incomeType`.
  Раздел §12 обновлён и по факту P0: **отдельного поля builder fee в V3 нет**,
  комиссия Builder списывается внутри обычного `COMMISSION` (первоначальная
  гипотеза про `builderFee` снята замером).
- `docs/ARCHITECTURE.md` §13 — правило одного числа и границы доказательства.
- Обновление карт `AGENTS.md` / `tests/AGENTS.md` / `scripts/AGENTS.md` /
  `miniapp/AGENTS.md` и журналов — в том же коммите, что и код (конвенция
  `docs/AGENTS.md`).

## Гейты

- backend `pytest -q -m "not testnet"` + ruff;
- `miniapp`: lint + test + build;
- живой тестнет-прогон по P4 с flat-аудитом;
- счётчики — только в `docs/ENGINE_TEST_STATUS_2026-08.md`.

## Журнал

- 2026-09-13: план зафиксирован, начата P0.
- 2026-09-13: P0 закрыта фактом — builder fee внутри `COMMISSION`
  (0,05% против 0,04%, разница ровно `feeRate=0.0001`, отдельных flow нет).
  Переходим к P1a + P3 (net для закрытых в Mini App и Telegram).
- 2026-09-13 (раунд 2): P2/P4 реализованы — `history/backfill.py` + CLI
  `backfill-income` (живой прогон на тестнете: 2790 строк начислений,
  `stopped_at=None`), Telegram-карточка закрытия печатает единый net через
  `notifications/money.py`. Детали и счётчики — в
  `docs/ENGINE_TEST_STATUS_2026-08.md`, раздел «Раунд 2».
- 2026-09-13 (раунд 3–4): P4 живой приёмки закрыт —
  `tests/testnet/test_live_net_close_card_acceptance.py` **1 passed (36.29s)** на
  REAL Testnet + REAL Bot API: событие → durable outbox → перерендер в момент
  отправки → `SENT`. Живой прогон поймал реальный баг: рендерер сверял
  `kind == "user.POSITION_CLOSED"`, тогда как в строке лежит
  `"POSITION_CLOSED"`; карточка в проде не перерендеривалась бы никогда —
  исправлено (`kind.removeprefix("user.")`), проверка закреплена живым тестом.
- Живой факт: аккаунт владельца платит комиссию **в ASTER**, поэтому его
  карточка несёт честный gross (`non_usdt_cost`), а не net; для USDT-аккаунтов
  net считается (`copyc2`, прод-данные: `net_complete`).
- Осталось: P4-ограничение выборки `load_episode_money` интервалом эпизода и
  (опционально) политика учёта не-USDT комиссий (исторический курс).
- 2026-09-13 (раунд 5): P4-ограничение выборки **закрыто фактом**.
  `cashflows_for_symbols`/`windows` получили необязательные границы,
  `cashflows_span_for_trades` даёт промежуток matched-начислений, миграция #65
  добавляет `idx_follower_cash_trade`. Живой факт из пробы: биржа ставит
  `COMMISSION` филла на 500–850 мс **раньше** самого филла, поэтому bound «по
  окну эпизода» отбрасывал комиссию входного филла (19 из 20 эпизодов давали
  `commission_missing`); объединение с промежутком начислений даёт **0
  расхождений** на 80 эпизодах трёх аккаунтов. Страница из 20 эпизодов
  `copyc1`: 100 запросов / 132 707 строк / 0.62 с → **5 запросов / 1415 строк /
  0.015 с**. Проверки: `scripts/verify_episode_money_bounds.py`,
  `scripts/benchmark_episode_money.py`,
  `tests/unit/history/test_accounting_bounds.py`, живая приёмка
  `tests/testnet/test_live_net_close_card_acceptance.py` — см. раунд 6 ниже.
  Детали — `docs/ENGINE_TEST_STATUS_2026-08.md`, раздел «Раунд 5».
- Осталось (опционально, решение владельца): политика учёта не-USDT комиссий
  (исторический курс ASTER/USDT) — сейчас такие сделки честно не публикуют net;
  живая net-строка по USDT-комиссии снята в раунде 6.
- 2026-09-13 (раунд 6): **net-строка реально доставлена**. Вариант
  `NET_CARD_ACCOUNT=copyc2` (комиссия в USDT) — живой прогон **1 passed
  (45.1s)**: в доставленном через REAL Bot API тексте есть
  `Итог сделки: <net> USDT` и нет `PnL до комиссий`, то есть send-time
  перерендер отработал по-настоящему. Дефолтный вариант (владелец,
  ASTER-комиссия) — **1 passed (46.6s)** с честным gross-фолбэком. Попутно
  найдены и закрыты три дефекта обвязки, каждый давал ложную зелень в раунде 4
  (проекция строки без `account_id`/`trade_ref`; привязка к «самому новому
  POSITION_CLOSED» вместо эпизода собственного close-филла; невыровненный
  ретаргетнутый follower). Детали — `docs/ENGINE_TEST_STATUS_2026-08.md`,
  раздел «Раунд 6».

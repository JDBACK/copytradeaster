# S3 — передача реализации на проверку

Дата: 2026-09-08. Реализация поверх `b83c708`.
**IMPLEMENTED / NOT ACCEPTED; G3 OPEN.** Тесты, browser/device и signed live
в этом проходе по просьбе владельца не запускались. Не начинать с нового
рефакторинга: сначала проверить внесённый функциональный объём.

## Что реализовано

S3.1: Home overview и переходы вместо первой позиции/TrustRail.
S3.2: Open/Closed previews, полные списки, фильтры, bounded session history,
независимые fills/events cursors, retry и explicit revision refresh; snapshot
fallback без trade_ref; detail money/stale/partial/loading; fills группируются
по доказанному order_id/role без финансовых вычислений в JS. Split возвращает
owned `TradeResolutionDTO` через существующий `/trade`, экран предлагает
выбрать target, а не открывает случайный эпизод. Alias/merge сохраняются.

S3.3: Mini App и Telegram menu читают `activity_feed`, используют один
`format_user_event`. Меню хранит только cursor цепочки (до 128 пользователей,
100 страниц по 5 событий), перечитывая ownership каждым запросом. Pending
отдельно от подтверждённых фактов. В меню и push есть owned Web App link.
Production `build_stage2_process` включает durable user-event delivery:
rollout floor на первом enable, boot recovery + recurring sweep раз в 2 с,
атомарные outbox/cursor commits. Shutdown отменяет sweep перед drain outbox.
История собирается независимо от уведомлений; muted rows продвигают cursor
без enqueue. Проверка recipient перед отправкой сохранена.

`execution.confirmed` в S3 остаётся console/diagnostic acknowledgement:
router не доставляет его пользователю, а worker помечает ранее накопленные
недоставленные строки этого типа `FAILED/superseded_by_user_events`.
Они входят в исторический счётчик «Не доставлено ранее», не означают сбой
текущей связи. Новый user-event scan не имеет старого capped recovery path.

S3.4: существующие Settings, risk preview, flat/exposed/unknown resume selector,
nullable permissions, Help/Security/Success, local calendar сохранены;
денежные факты не вычисляются на frontend. Исправлен знак отрицательных
значений меньше единицы в общем Python presenter.

S3.5: этот guide и актуальные карты подготовлены. Приёмка ниже НЕ выполнена.

## Совместимость и границы

- Low-level `Stage2Process(..., enable_user_event_delivery=False)` и явный
  factory override False сохраняют legacy command delivery, без user-event
  cursor/drain. Production factory default теперь True. Это seam для
  совместимости; обычный `run-bot` использует S3.
- Старый `/activity` временно оставлен для уже открытых pre-S3 WebView как
  adapter к user events. Никакого command→position mapper больше нет.
  Numbered pages ограничены 0..50; внутри запроса используются feed cursors.
  Новый Mini App и меню используют cursor напрямую. Удалить adapter, old DTO
  и frontend `api.activity` после вывода pre-S3 WebView из эксплуатации.
- Канонический URL: настроенный HTTPS host + `/miniapp/?trade=<opaque-ref>`.
  Уже настроенный `/miniapp/` не дублируется. Frontend также читает прежний
  `trade_ref`; start_param остаётся только navigation intent. Произвольный
  event host не используется; worker добавляет Web App keyboard только в
  private recipient chat. Main Mini App/startapp rollout здесь не включался.
- Trade detail теперь возвращает union detail | split resolution. Old error
  `trade_split` остаётся для запроса events по невыбранному split ref.
  Missing/foreign ref возвращаются одинаково; кеш detail очищается при
  `trade_unavailable`.
- N6 harness требует доработки следующим тестовым проходом: его код здесь
  намеренно не менялся. Нет нового доказательства SL fill, STOP cleanup,
  performance или Telegram device acceptance.

## Следующий проход: по одному блоку

1. Сначала адаптировать obsolete fixtures/assertions, затем один offline gate.
   Изменены контракты `MenuEngine.activity`/`render_activity` (ActivityFeedPage),
   удалены `ActivityKindFilter`, runtime `read_activity`, command Activity mapper
   и `NotificationRouter.recover_user_events`. Старые usages есть в
   `tests/unit/application/test_adapters.py`,
   `tests/integration/test_stage2_adapters.py`,
   `tests/unit/telegram/test_renderers.py`,
   `tests/unit/notifications/test_notifications.py`,
   `tests/unit/application/test_history_acceptance.py`,
   `tests/testnet/test_history_acceptance_live.py`,
   `tests/testnet/test_builder_resume_live.py`.
   Не восстанавливать удалённую трактовку ради старых assertions.
2. Проверить N5 через production factory и low-level False: >1000 событий,
   два boot, crash до/после atomic commit, muted/re-enable без backlog,
   отсутствие command/user duplicate, остановка recurring task до DB close,
   сбой sweep с последующим retry. Реальную отправку в Telegram здесь не делать.
3. HTTP/UI: owned split/alias/merge/foreign/missing, переход через gate/recovery,
   старый и новый query; menu/push/feed одинаковые тексты и знаки малых PnL.
   Pagination fills/events независимо, группировка fills на границе страницы,
   retry сохраняет страницы, revision требует refresh, Back сохраняет фильтр
   и scroll. Проверить open→closed на том же ref и stale/partial/empty.
4. Один общий offline gate: backend `pytest -q -m "not testnet"`, frontend
   `pnpm lint && pnpm test && pnpm build`, ruff check/format, doc links.
   Счётчики записывать только в ENGINE_TEST_STATUS.
5. Browser: итоговый production bundle, 320/360/390/430; новые active/paused
   Home screenshots с commit/viewport; все новые списки/details/feed и ошибки.
   По §19.7: cold root budget, warm >=30 samples, delayed API, длинная история,
   RiskRuler, 50 navigation cycles и cache/listener/memory bounds.
6. UTC/Europe/Moscow/America/New_York: полночь, год, DST; local day совпадает
   с подписью времени. Telegram WebView: owned links, native Back, haptics,
   короткий viewport, keyboard, iOS/Android gestures. Нужен owner evidence.
7. N6 перед signed live: в `tests/testnet/test_history_acceptance_live.py`
   отменять STOP по фактическому `execution_commands.client_order_id` из
   результата PLACE_STOP, не по исходному `stop_coid`. Query/order-list должны
   доказать отсутствие именно этого STOP, одного CONFIRMED после -2011 мало.
   Дополнить scheduled captures, фактический SL fill, restart/open/partial/full
   lifecycle и HMAC HTTP; cleanup подтверждает flat + no open orders.
8. Только после этого заполнить §15/§19.7 evidence и verdict G3. Старые зелёные
   прогоны — baseline, не результат проверки нынешних изменений.

## Статические проверки реализации

TypeScript compilation проходит; изменённые Python-файлы прошли ruff и
форматирование. Это не замена runtime/offline/browser/live проверки.

## Дополнение: toggle boundary после первого implementation commit

Проверить событие, записанное между disable и enable при отсутствии sweep,
а также sweep prepare → toggle → sweep commit. Toggle фиксирует max published
seq в той же транзакции, что и настройку (только после rollout); commit
повторно проверяет recipient и не вставляет строки ниже committed cursor.
Cursor обновляется монотонно через MAX. При выключении PENDING outbox
помечается FAILED/recipient_disabled. Уже начатый внешний send не отзывается.
No-op enable=True не должен съедать ожидающие новые события. Новых тестов и
прогонов для этого дополнения пока нет.

## Прогресс приёмки (2026-09-09, частичный проход)

Проход остановлен владельцем по лимиту сессии; полные факты — §19
`MINIAPP_S3_PROGRESS_2026-09.md`. Сделано: стенд `preview_miniapp_engine`
на `tg-8264892202` (preview-харнесс исправлен — dev-юзер отвязывается в
одноразовой копии), живые testnet-данные через master open/close,
браузерный проход 390×844 (Home active/paused + resume sheet, Positions +
detail, Activity, Settings + 6 вложенных, native Back, RiskRuler save
2%→1%→2%, notifications double-tap gate), 17 скриншотов в
`docs/reports/acceptance-s3/`. Не закрыто: 320/360/430, состояния ошибок,
§19.7 бюджеты, closed list/detail на непустой истории, §6 owner device
evidence, offline/signed гейты. **G3 OPEN.**

## Дополнение: Telegram WebView-проход 2026-09-09

После §19 выполнен отдельный live-проход в уже открытом Telegram Desktop
WebView. Подтверждены непустые `Закрытые` и owned detail на `BTCUSDT · LONG`
(`+2,25 USDT`, вход/выход `0,037 BTC`, остаток `0`), события открытия/закрытия,
два раскрываемых fills (вход и выход), Activity `Все`/`Сделки`, переход
`Открыть сделку`, native Back, Settings и шесть вложенных экранов, а также
Home flat и pause → Resume Sheet → active. Визуальные факты и финальная
сверка БД записаны в §21 `MINIAPP_S3_PROGRESS_2026-09.md`.

Финальное состояние после прохода: `risk_percent=0.02`,
`allow_new_entries=1`, `notifications_enabled=0`, `170` episodes, `487` fills,
`0` незавершённых execution commands. Уведомления не переключались.

Открытыми для G3 остаются mobile iOS/Android evidence (haptics, keyboard,
WebKit gestures), viewports `320/360/430`, полный §19.7 CDP trace и
split/alias/partial-specific history. **G3 OPEN.**

Полная карта фактически выполненных Mini App UI-проверок S2/S3 — Telegram
Desktop WebView, Chromium/CDP, frontend-тесты и screenshots — находится в
[MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md](MINIAPP_UI_ACCEPTANCE_INDEX_2026-09.md).

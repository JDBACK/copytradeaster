# Mini App Live Mode — implementation plan

Дата старта: 2026-09-10
Ветка: `codex/miniapp-live-updates`
Статус: **ACCEPTED (REAL Aster Testnet)** — см. финальную запись §8

## 1. Цель

Mini App должен обновлять позиции, PnL, dashboard, историю и системное состояние
без штатной кнопки «Обновить». Пользователь видит текущие данные как на бирже;
при потере связи последний подтверждённый снимок остаётся на экране и получает
явный статус устаревания.

Итоговый UX:

- healthy-состояние: компактный `LIVE`, без общей кнопки обновления;
- reconnect: `Восстанавливаем связь…`, старые данные остаются видимыми;
- длительный разрыв: `Данные устарели` и контекстный `Повторить` только после
  неуспешного автоматического восстановления;
- ручная проверка Agent/Builder остаётся: это security-действие, а не обновление
  экрана;
- pull-to-refresh не является основным transport-механизмом и не нужен для
  первого выпуска live mode. В Telegram WebView он может конфликтовать с
  вертикальным закрывающим жестом и рассматривается отдельно.

## 2. Текущая точка

Frontend использует session-scoped resources с TTL 30–60 секунд. Слабая
ревалидация происходит при первом открытии, активации вкладки, возврате приложения
на передний план или явной invalidation. Постоянного polling нет; пока вкладка
остаётся открытой, TTL сам по себе запрос не запускает. `refresh=true` выполняет
новый follower REST snapshot, `refresh=false` читает process-local cache.

Периодический reconciler уже получает authoritative follower snapshot, но этот
снимок не попадает в Mini App cache. Поэтому UI не знает о торговом изменении до
ручного refresh или lifecycle-события.

## 3. Целевая схема

```text
Aster follower user stream (per REAL account)
  ACCOUNT_UPDATE / ORDER_TRADE_UPDATE / MARGIN_CALL
                       |
                       v   low-latency hint, coalesced
              authoritative follower REST snapshot
                       |
                       +--> trading reconciliation (как сейчас)
                       |
                       +--> FollowerSnapshotStore (revision, captured_at)
                                      |
Aster public mark-price stream --------+--> MiniAppLiveHub
 (shared, symbols only)                         |
                                               v
                                authenticated browser WebSocket
                                               |
                                               v
                              resource invalidation + weak cache read
```

### Источники истины

- REST snapshot остаётся authoritative для balance, positions, orders,
  protection и всех торговых решений.
- Private follower WS — только low-latency сигнал сделать coalesced REST cutover.
- Public mark-price WS обновляет только presentation quote/PnL между REST
  снимками. Он не открывает admission и не управляет ордерами.
- После любого reconnect сначала выполняется REST boundary; только затем клиент
  получает состояние `live`.

## 4. Backend packages

### L1 — process-local read model

- Добавить monotonic `FollowerSnapshotStore`: `account_id -> snapshot +
  captured_at + revision`.
- Публиковать в него каждый успешно собранный follower snapshot, включая
  startup/periodic/manual reconciliation и post-command refresh.
- Перевести `AppStage2RuntimeAdapter.cached_snapshot` на общий store, чтобы
  `refresh=false` отдавал уже полученные engine данные без нового Aster REST.
- Не хранить credentials, signatures или raw Aster payloads.

### L2 — authenticated Mini App live transport

- POST `/miniapp/api/live-ticket` проходит существующую Telegram initData
  проверку и выдаёт короткоживущий single-use opaque ticket.
- GET WebSocket `/miniapp/api/live?ticket=...` атомарно поглощает ticket и
  получает user/account scope только на сервере. initData и секреты не попадают
  в URL или логи.
- `MiniAppLiveHub` держит bounded latest-wins queue, heartbeat и monotonic
  revision; медленный клиент не создаёт unbounded memory.
- Live payload содержит только безопасные resource keys, revision, captured_at
  и health. Полные DTO по-прежнему читаются через существующие application use
  cases.

### L3 — follower private stream pool

- Переиспользовать `aster.user_stream.UserStream` для каждого published REAL
  follower; SHADOW не создаёт внешний socket.
- События `ACCOUNT_UPDATE`, `ORDER_TRADE_UPDATE`, `ACCOUNT_CONFIG_UPDATE` и
  `MARGIN_CALL` сворачиваются в один refresh на account; burst не запускает
  параллельные REST snapshots.
- Lifecycle связан с registry: boot/start, onboarding/replacement/disconnect и
  shutdown не оставляют orphan tasks/listenKeys.
- Disconnect немедленно публикует degraded presentation health. Reconnect
  публикует `live` только после успешного authoritative snapshot.

### L4 — public mark-price stream

- Один общий market connection для нужных symbol, а не socket на каждого
  пользователя.
- Использовать `<symbol>@markPrice@1s` или all-market stream после testnet
  contract-check; decimal parsing без `float`.
- Quotes coalesce по symbol и публикуются в UI не чаще заданного render cadence.
- Public stream failure не меняет trading safety state; REST marks остаются
  fallback.

## 5. Frontend packages

### L5 — live session client

- Один клиент на `MiniAppResources` session scope: ticket -> WebSocket ->
  reconnect с bounded exponential backoff.
- Учитывать `visibilitychange`/focus, heartbeat timeout и `invalid_session`.
- Игнорировать duplicate/out-of-order revisions.
- Live event вызывает invalidation затронутых resources; активный screen делает
  weak read общего server cache, поэтому не создаёт ещё один Aster REST call.
- Existing generation/fence rules запрещают старому HTTP response затереть более
  новую live revision.

### L6 — status и отказ от постоянных кнопок

- Добавить один ненавязчивый `LiveStatus` в shell/данные экрана.
- Сохранить last good data при reconnect/error и показывать stale age.
- Удалить постоянные `Обновить` из Positions/Activity/history/status после того,
  как L1–L5 проходят тесты.
- Контекстный retry появляется только при подтверждённой длительной ошибке.
- Account Builder «Перепроверить разрешения» остаётся отдельным explicit action.

## 6. Проверки и acceptance

### Offline

- store revision/atomic publication и отсутствие stale overwrite;
- ticket expiry, single-use, wrong user/session, bounded fan-out;
- follower WS burst coalescing, reconnect boundary, lifecycle teardown;
- frontend reconnect/background/heartbeat/out-of-order/resource fencing;
- regression `pnpm lint && pnpm test && pnpm build`;
- targeted backend unit/integration, затем `pytest -q -m "not testnet"` и ruff.

### REAL Aster Testnet

- follower order/position change появляется в открытой Mini App без нажатия;
- PnL меняется от public mark stream без private account event;
- разрыв и восстановление socket не очищают карточки и дают REST boundary;
- restart процесса восстанавливает streams и live client;
- финальное состояние аккаунтов FLAT; mainnet writes не выполняются.

### Manual UI

- Chromium и Telegram Desktop WebView: нет штатных refresh-кнопок;
- `LIVE -> reconnecting -> stale -> LIVE` читается без скачка layout;
- 50 переходов по вкладкам не создают несколько WebSocket sessions.

## 7. Ограничения и источники

На масштабе v1 (`max_followers=10`) один private stream на REAL follower
укладывается в текущий single-process modular monolith. Вынос в отдельный сервис
не нужен до измеренной причины. REST rate budget защищается coalescing и
существующим shared limiter.

Контракты сверены с официальным репозиторием Aster API:

- [Futures V3 API](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md)
- [Futures Testnet API](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-testnet.md)

`listenKey` живёт 60 минут и продлевается keepalive; private stream доступен как
`/ws/<listenKey>`. Документация отдельно предупреждает, что payloads в тяжёлые
периоды могут прийти не по порядку — поэтому revision и REST boundary обязательны.
Mark-price stream поддерживает single-symbol `@markPrice`/`@markPrice@1s` и
all-market варианты.

## 8. Журнал

### 2026-09-10 — kickoff

- [x] Создана отдельная ветка `codex/miniapp-live-updates`.
- [x] Разобран текущий resource/cache/lifecycle flow frontend и backend.
- [x] Подтверждены Aster user-stream и mark-price контракты.
- [x] Зафиксирована целевая архитектура и acceptance matrix.
- [ ] L1 process-local read model.
- [ ] L2 authenticated Mini App live transport.
- [ ] L3 follower private stream pool.
- [ ] L4 public mark-price stream.
- [ ] L5 frontend live session client.
- [ ] L6 live UX и удаление штатных refresh-кнопок.
- [ ] Offline/full/testnet/manual acceptance.

### 2026-09-10 — checkpoint перед передачей в следующую сессию

Чеклист kickoff выше оставлен как список итоговых acceptance-gates. Текущее
состояние реализации:

- [x] L1 core: process-local `FollowerSnapshotStore`, публикация snapshots из
  coordinator и общий cache seam для Mini App.
- [x] L2 core: одноразовый ticket, Telegram/session/account checks,
  account-scoped live hub и WebSocket transport.
- [x] L3 core: private follower user-stream pool для REAL accounts; входящее
  событие только будит coalesced authoritative REST refresh.
- [x] L4 core: общий public mark-price stream; котировки обновляют PnL и не
  подменяют возраст последнего REST snapshot.
- [x] L5 core: frontend live session с reconnect/backoff, stale/auth states и
  resource invalidation для dashboard/positions/activity.
- [ ] L6 UI: live status rail, reconnect/stale affordance и удаление общих
  refresh-кнопок ещё не внесены.
- [ ] Dedicated live tests, REAL Aster Testnet run и ручная Telegram Mini App
  acceptance ещё не выполнены.

Коммит этого checkpoint фиксирует архитектурный фундамент, но не объявляет
live mode production-ready: следующий шаг — довести L6 и acceptance-gates,
после чего отдельно проверить lifecycle/backoff/rate-limit edge cases.

### 2026-09-10 — L6 + offline hardening checkpoint

- [x] L6 UI: в `AppShell` добавлен один компактный live rail (`LIVE` /
  connecting / reconnecting / stale); last-good DTO остаются на экране, а
  `Повторить` появляется только после подтверждённого stale.
- [x] Удалены постоянные generic refresh-кнопки из Positions, Activity,
  history/trade detail и healthy System Status. Контекстные error/unknown
  recovery-действия и `Перепроверить разрешения` в Account Builder сохранены.
- [x] Frontend live lifecycle hardened: heartbeat timeout учитывает hidden
  WebView, focus/visibility запускают восстановление, duplicate/out-of-order
  revision не применяются, ручной retry fenced от старого `onclose`.
- [x] Bounded queue loss закрыт через revision-gap reconciliation: если клиент
  видит пропуск revision или reconnect `hello` с более новым revision, он
  инвалидирует весь live-backed resource set и перечитывает активные DTO.
- [x] WebSocket `hello` отправляется до sender task и задаёт baseline revision;
  server application heartbeat не меняет freshness DTO.
- [x] Dedicated offline live tests добавлены для ticket TTL/single-use,
  account-scoped bounded fan-out, Decimal mark-price, сохранения REST-age,
  follower burst coalescing, heartbeat/auth/reconnect/revision-gap client paths.

Live mode всё ещё **IN PROGRESS**: REAL Aster Testnet и ручной Telegram Mini App
acceptance из §6 не выполнены и не считаются закрытыми этим checkpoint.

### 2026-09-10 — REAL acceptance corrective checkpoint

После `dbddd98` implementation scope L1–L6 остаётся кодово завершённым, но REAL
acceptance выявил несколько transport/causal/lifecycle дефектов. Они исправляются
отдельным corrective-коммитом; `dbddd98` не переписывается.

Текущий фактический прогресс:

- [x] L1–L6 implementation сохранён: общий follower snapshot store,
  authenticated live WebSocket, private follower hints, public mark-price
  presentation stream, session-scoped live client и live/stale UX без generic
  healthy `Обновить`.
- [x] Real Telegram signed WebView подтверждён: Mini App открывается через
  реальную кнопку бота и transport rail достигает `LIVE`; synthetic initData,
  fake HMAC и browser-side bot token не использовались.
- [x] `/live-ticket` больше не связывает Mini App доступ с
  `telegram_users.blocked_at`: notification mute/delivery state остаётся
  отдельным от `access_enabled` authorization boundary.
- [x] Исправлен shutdown race в `UserStream` и market live stream: дочерние
  `recv()/stop.wait()` tasks отменяются и обязательно awaited/consumed при
  parent cancellation и simultaneous completion. После corrective shutdown
  новые `Task exception was never retrieved` / `ConnectionClosedOK` не
  воспроизвелись; старые блоки логов до фикса не считаются регрессией.
- [x] Исправлена causal self-invalidation сильного Dashboard refresh:
  authoritative REST snapshot мог публиковаться через `FollowerSnapshotStore`
  в live hub раньше завершения собственного HTTP response и тем самым fence'ить
  самого себя. Frontend теперь сохраняет `source/captured_at`, принимает только
  точно совпадающую REST self-publication как покрытую этим strong response, а
  более новый REST/mark/gap/mutation boundary по-прежнему выигрывает.
- [x] Healthy live steady-state использует weak cache reads; initial/recovery и
  explicit authoritative verification остаются strong. Weak live evidence не
  снимает retained mutation uncertainty.
- [x] Исправлен stale-rail race: после 12s deadline автоматические reconnect
  attempts продолжаются в фоне, но больше не перетирают `stale` обратно в
  `reconnecting`. Manual `Повторить` намеренно начинает новый deadline.
- [x] Найдена отдельная backend causal race для `LIVE + Требуется проверка`:
  periodic master boundary временно выставляет `ready=false/degraded=true`, в
  этот момент follower reconciliation публикует свежий snapshot, а после
  успешного `clear_degraded()` изменение process-wide system state раньше не
  давало live invalidation. Поэтому browser мог удерживать transient degraded
  Dashboard до следующей follower publication.
- [x] Для этой race добавлен regression, сначала воспроизводивший отсутствие
  post-READY publication. `FollowerSnapshotStore.republish()` теперь может
  переиздать уже существующий immutable follower snapshot с тем же
  `captured_at`, а App после успешного снятия degraded публикует
  `source=system-state` для `dashboard/positions/status`. Это presentation
  invalidation без дополнительного Aster REST и без подделки REST freshness.
- [x] Targeted corrective regressions, changed-surface Ruff и
  `git diff --check` проходят на текущем checkpoint.

### 2026-09-10 — финальный corrective + REAL acceptance PASS

Список выше закрыт полностью; ниже — что именно было найдено и доказано.

**Найдено и исправлено в этой сессии.**

- [x] **Restart backend занимал rail в `stale` навсегда.** Live hub нумерует
  события с `revision=0` в каждом процессе, поэтому клиент с прежним watermark
  отбрасывал `hello` и все последующие события как out-of-order, хотя HTTP
  данные обновлялись. Hub теперь штампует каждое событие и `hello`
  per-process `epoch` (`secrets.token_hex(8)`), а `miniapp/src/live.ts` при
  смене epoch пере-baseline'ит watermark (`previousRevision = -1`) и делает
  полную reconciliation вместо удержания `stale`.
- [x] **Public mark-price stream на testnet-кластере молчит** — и через
  документированный raw path (`<ws>/<symbol>@markPrice@1s`), и через SUBSCRIBE:
  сокет открывается, ack `{"id":1,"result":null}` приходит, дальше **ни одного
  кадра** (проверено изолированной пробой: `aggTrade` на том же endpoint
  стримит, любые `@markPrice`/`!markPrice@arr` — нет). Прежняя реализация
  ставила `_market_live = True` уже на connect, поэтому health врал `live`, а
  PnL не двигался. Теперь L4 работает так: primary — shared SUBSCRIBE-стрим,
  liveness объявляется **только после первого реального кадра** (тишина дольше
  `LIVE_MARK_ACK_TIMEOUT_S=10` уходит в backoff и публикует `reconnecting`), а
  presentation-котировки добирает bounded unsigned REST fallback
  `GET /fapi/v3/premiumIndex` (`AsterClient.mark_prices`, weight 10, Decimal,
  whitelist, мусор отбрасывается) с интервалом 3s. Fallback простаивает, пока
  стрим реально отдаёт кадры, не трогает trading state и не обновляет REST
  `captured_at` (`FollowerSnapshotStore.apply_mark_prices` сохраняет прежний
  `captured_at`) — это ровно тот `REST marks остаются fallback`, который
  предусматривает L4.
- [x] **Сервер отдавал клиенту имя класса исключения как public code.**
  Неизвестный код не находится в copy map фронтенда, поэтому транзиентный
  upstream-сбой рисовал пугающий generic-баннер «начните подключение заново».
  `_error_code()` теперь классифицирует неразмеченный сбой как retryable
  `service_unavailable`, а `errors.ts` получил честные копии для `not_ready`,
  `stale_operation`, `application_error`, `live_unavailable`,
  `live_scope_unavailable`.

**Чем это подтверждено (REAL Aster Testnet + real Telegram Desktop WebView).**

- [x] Стек поднят заново после перезагрузки ПК: tunnel
  `https://dawn-greene-organize-waves…` переиспользован, оба menu button
  (`default` и `chat 203493943`) `MATCH`.
- [x] Periodic master boundary: `degraded_active=true/reason=periodic-snapshot`
  держится ~8s каждые ~75s, и UI в это время честно показывает
  `Данные требуют проверки`, **сам возвращается** в healthy сразу после READY —
  наблюдено 4+ полных цикла в открытом WebView. Заявленный баг закрыт.
- [x] Transport-цикл: `LIVE -> Восстанавливаем связь -> Данные устарели · N сек
  назад + Повторить -> LIVE`. Last-good карточки переживают разрыв, фоновые
  авто-reconnect не сбрасывают `stale` обратно, `Повторить` работает.
- [x] Минимальный REAL Testnet open (`manual_master_order.py open BTCUSDT 0.002`):
  follower-позиция появилась в **уже открытом** Mini App без ручного refresh,
  PnL менялся между замерами (`+4,94 -> +2,58 -> ...`), плашка обновления
  отражала REST-возраст, а не тики котировок.
- [x] Позиция закрыта, повторный direct audit — `AUDIT: FLAT-ALL` (main, copyc1,
  copyc2, copyc3, tg-8264892202: positions=0, open_orders=0). Mainnet writes — 0.
- [x] 53 переключения tab/nested: число server-side соединений не выросло
  (9 -> 9), новых browser WebSocket sessions и REST storm нет.
- [x] Lifecycle: `Task exception was never retrieved` после финального рестарта —
  0 (все 10 в логе старше правок), `mark-price stream failed` — 0 (fallback
  закрывает котировки), orphan listenKeys нет, один shared mark stream.
- [x] Полный frontend gate: `pnpm lint && pnpm test && pnpm build` — 367 тестов,
  30 файлов, build OK. Полный backend: `pytest -q -m "not testnet"` —
  `1540 passed, 2 skipped, 87 deselected`. Changed-surface Ruff и
  `git diff --check` — чисто.

**Остаётся открытым (не блокирует live mode, вынесено отдельно).**

- Silent mark-price stream на testnet — свойство кластера, не наш дефект;
  перед mainnet стоит перепроверить, что production-кластер отдаёт кадры и
  fallback остаётся холодным.
- Известный несвязанный провал глобального `ruff check .` в
  `scripts/check_doc_facts.py` (и ruff-дрейф форматирования в 4 файлах на HEAD)
  не входит в этот gate и не трогался.

Live mode в объёме §6 объявляется **PASS** для REAL Aster Testnet.

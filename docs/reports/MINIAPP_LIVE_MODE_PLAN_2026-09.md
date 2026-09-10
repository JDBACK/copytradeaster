# Mini App Live Mode — implementation plan

Дата старта: 2026-09-10
Ветка: `codex/miniapp-live-updates`
Статус: **ACCEPTED (REAL Aster Testnet)** — см. §8. UX de-noising pass
(ручных обновлений нет, свайп реализован) — проверен offline, финальный ручной
проход в реальном WebView остаётся за владельцем.

## 1. Цель

Mini App должен обновлять позиции, PnL, dashboard, историю и системное состояние
без штатной кнопки «Обновить». Пользователь видит текущие данные как на бирже;
при потере связи последний подтверждённый снимок остаётся на экране и получает
явный статус устаревания.

Итоговый UX:

 автопереподключение продолжается в фоне, а единственное ручное действие —
 свайп вниз (см. журнал 2026-09-10);
- healthy-состояние: rail не показывается вообще (ни `LIVE`, ни `connecting`),
  общей кнопки обновления нет;
- короткие переподключения не видны: транспорт чинит себя в фоне, данные при
  этом остаются на экране;
- длительный разрыв: rail показывает `Нет связи · N сек назад` без кнопки;
  автопереподключение продолжается в фоне, а единственное ручное действие —
  свайп вниз (см. журнал 2026-09-10);
- ручная проверка Agent/Builder остаётся: это security-действие, а не обновление
  экрана;
- pull-to-refresh не является transport-механизмом: данные обновляются сами.
  Он реализован как единственный ручной путь (см. журнал 2026-09-10 «UX
  de-noising») и конфликт с вертикальным жестом Telegram решён явно: жест
  забирается только у верха скролла и сразу возвращается клиенту
  (`disableVerticalSwipes`/`enableVerticalSwipes`, Bot API 7.7+, старые клиенты
  просто игнорируют вызов).

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

### 2026-09-10 — UX de-noising: ручных обновлений больше нет

Приёмка §6 закрыта, но продуктовый просмотр живого WebView показал, что
«честность» была реализована как дёрготня: экран сообщал о собственной работе.
Разбор и исправление (это не новый транспорт, а удаление лишних сообщений).

**Что было не так.**

- `snapshot.stale` в `MiniAppResources` — это флаг «пришла invalidation, читаю
  кэш», а не «данные устарели». Он взводился на каждой live-инвалидации и каждом
  mark-тике, а туннель отвечает ~0,9 s, поэтому Главная мигала «данные требуют
  обновления» / «Не обновлён», Позиции — «снимок позиций устарел», и так каждые
  несколько секунд.
- Periodic master boundary (~каждые 75 s, ~8 s) выставляет `ready=false` +
  `degraded=periodic-snapshot`; UI показывал это как «Требуется проверка». Это
  служебная самопроверка движка, а не инцидент.
- Любой blip сокета публиковал `reconnecting`, поэтому rail мигал
  «Восстанавливаем связь…».
- История отдавала пользователю `409 history_page_changed` («История изменилась —
  обновить») на обычное обновление списка; в Активности жила кнопка «Есть новые
  события».

**Что сделано.**

- Backend: `_presentation_state()` отличает routine-окна движка
  (`periodic-snapshot`, `master-dirty`) от настоящих отказов и отдаёт их как
  READY; trading-gate (`allow_new_entries`, admission) не тронут.
- Frontend: `useTransportStale()` заменил внутренний флаг перечитки; удалены
  ручные кнопки обновления из всех экранов (Home, Positions, TradeList,
  TradeDetail, Activity, Settings, Risk, Notifications, SystemStatus), убраны
  режимы «есть новые события» и «история изменилась»; история тихо пере-basе'ится
  вместо ошибки.
- Ошибка чтения теперь лечится сама: bounded backoff retry в `useResource` и
  `useHistory` (1→2→5→10→20 s), поэтому ручной retry не нужен нигде.
- Rail `LiveStatus` рендерится только в отказе: в healthy-состоянии там нет ни
  `LIVE`, ни `connecting`; blip короче 2,5 s не виден вообще (grace-окно).
  Кнопки в rail нет: автопереподключение идёт в фоне, поэтому в отказе остаётся
  только честная строка `Нет связи · N сек назад`.
- Реализован pull-to-refresh — единственный ручной путь: `pullToRefresh.ts`
  (чистая модель: slop, порог 64 px, демпфирование, право на жест) +
  `components/PullToRefresh.tsx` (арм только у верха скролла, claim вертикального
  жеста у Telegram и немедленный возврат, одно сильное чтение всех смонтированных
  экранов + переподключение live).

**Проверено.**

- frontend `pnpm lint && pnpm test && pnpm build` — 32 файла / **385 passed**,
  oxlint clean, production build PASS.
- backend `pytest -q -m "not testnet"` — **1541 passed, 2 skipped, 87 deselected**.
- Опубликованный бандл (`/miniapp/`) содержит новую логику; `Обновить` как строка
  UI в бандле отсутствует, остаётся только rail-`Повторить`.
- Стек поднят заново через `scripts/dev-up.sh`: туннель переиспользован, оба menu
  button `MATCH`, `status` — ready, heartbeat свежий, degraded нет, 0 orphan
  задач в логе после рестарта.
- Read-only проба против живого API (`data/probe_boundary_presentation.py`,
  git-ignored): 110 замеров по 2 s, из них 9 попали в реальное окно
  `degraded_active=true reason=periodic-snapshot`. Во всех девяти API отдавал
  `copy_state=active`, `system_state=ready`, `freshness=fresh`,
  `positions.is_stale=false` — «OK: no routine boundary reached the user-facing
  payload». До фикса та же проба показывала `degraded`/`не обновлён`.

**Осталось.**

- Финальная ручная проверка в реальном Telegram WebView: healthy-Главная без
  плашек, свайп вниз обновляет, «Нет связи» появляется только при реальном
  разрыве дольше 12 s.
- Известное несвязанное предупреждение `history sync failed
  account_id=copyc1 stream=capture:BTCUSDT` (копится с прошлых прогонов, раз в
  ~5 мин, к UI-политике отношения не имеет) — отдельная задача.

### 2026-09-10 — второй проход: реальный браузер нашёл ещё три «дёргания»

Первый проход закрыл четыре причины, но проверка в настоящем Chromium против
живого бэкенда (не jsdom и не скриншоты) нашла ещё три.

**Найдено замером, не на глаз.**

- `scripts/audit_miniapp_browser.mjs jitter` записывал все DOM-мутации на
  видимой поверхности: за 180 s 30 изменений, из них 5 — появление и удаление
  `Восстанавливаем связь…` примерно каждые 25–50 s по ~2 s. Замер rail после
  правки: 21 изменение, все — числа PnL/баланса и минута в `обновлено HH:MM`,
  rail не появился ни разу.
- Экран «Статус системы» показывал `Данные устарели` у полностью здорового
  аккаунта: строка `Разрешения` старела, потому что TTL доказательств
  (`PERMISSION_TTL_SECONDS`) — 60 s. Пользователь видел наг-вердикт при каждом
  открытии экрана, а после удаления кнопки «Обновить» сделать с этим было
  нечего.
- На экране подключения при прямом аккаунте рядом с `Builder — Не используется`
  стояло `Проверено сервером — Нет подтверждения`, то есть подтверждение
  требовалось там, где подтверждать нечего.

**Что сделано.**

- Rail теперь рендерится только при вердикте `stale` (`Нет связи · N сек назад`).
  `reconnecting` остаётся внутренним состоянием модели: короткие блипы не видно,
  а о разрыве пользователь узнаёт ровно тогда, когда данные действительно
  перестали обновляться.
- Backend: `status()` больше не роняет общий вердикт в `STALE` только из-за
  давности доказательств — строка `Разрешения` честно несёт свой возраст, а общий
  статус остаётся `Всё работает`; сорвавшаяся попытка проверки (`signed_refresh_failed`)
  по-прежнему показывается как `STALE`/`Проверка прав устарела`.
- Frontend: экран «Статус системы» один раз при открытии сам выполняет сильную
  проверку, если права не подтверждены (bounded, без цикла и без кнопки).
- `Проверено сервером` переименовано в `Подтверждение Builder`, и для прямого
  подключения отвечает `Не требуется` вместо `Нет подтверждения`.
- `degraded_since`: движок штампует начало эпизода деградации, а presentation
  показывает служебное окно только пока оно короче 20 s. Набор служебных причин
  расширен на lifecycle-события мастера (`stream-disconnected`,
  `stream-reconnecting`, `stream-startup-timeout`,
  `master-changed-before-ready`, `master-boundary-not-quiescent`,
  `master-recovery:*`). Затянувшееся окно снова читается как деградация —
  «инженерная самопроверка» не может вечно прятать реальный стоп.
- `MiniAppLiveHub.publish_health` сообщает о смене вердикта, и падение
  логируется одной строкой: без этого причину мигания нельзя было отнести ни к
  private-стриму, ни к котировкам.

**Чем подтверждено (real Chrome + живой бэкенд).**

- `nested`: обход всех вложенных экранов (Статус, Риск, Подключение, Уведомления,
  Закрытые сделки, Безопасность, Помощь) — `noise: []` в каждом; Статус читается
  как `Всё работает · Проверка: 15:12 · только что`.
- `series`: четыре вкладки — `noise: []`, rail не отрисован нигде.
- `swipe`: касание-жест вниз → `ready` (74,88 px при 96 px хода, `Отпустите для
  обновления`) → `refreshing` → `idle`; запросы жест делает сильными
  (`dashboard {refresh:true}`, `positions {refresh:true}`); при прокрутке
  страницы вниз (проверено на реально скроллящемся экране: `scrollTop 200`, 0
  запросов, фаза `idle`) жест не срабатывает; передача вертикального жеста
  Telegram — ровно `disable` → `enable`.
- Логи причины флапа: `live health dropped … private=True quotes=False` (раз в
  ~84 s) — падает доступность fallback-котировок, а не private-стрим;
  пользователь теперь этого не видит вообще.
- Тесты: frontend **387 passed**; backend **1545 passed / 2 skipped / 87
  deselected**; ruff и `git diff --check` чисто.

**Осталось.**

- Ручной проход в реальном Telegram WebView (владелец): свайп пальцем, отсутствие
  плашек в здоровом состоянии, `Нет связи` только при реальном разрыве.
- Известное несвязанное `history sync failed account_id=copyc1` (раз в ~5 мин) —
  отдельная задача.

### 2026-09-10 — третий проход: движок перестал говорить кодами

Продуктовый просмотр живого фида нашёл последний след «честности» как служебного
текста: Активность печатала коды движка (`Вход BTCUSDT пропущен: STALE_MASTER`,
`…: INSUFFICIENT_MARGIN`, `…: NO_STOP`). Сырые коды в базе подтвердились запросом:
8 событий `ENTRY_SKIPPED` за 10:30–10:31.

**Почему это было видно.** Строку рисует не фронтенд, а общий презентер
`notifications/trade_cards.py` — тот же, что формирует push и пункт меню. Push-путь
(`formatter.format_skip_reason`) уже говорил по-русски, а ветка `ENTRY_SKIPPED`
подставляла `facts["reason"]` как есть.

**Что сделано.**

- `_SKIP_REASON_COPY`: все 10 кодов `SkipReason` получают человеческую причину
  («недостаточно свободной маржи», «сигнал мастера устарел», «плечо для символа ещё
  не подготовлено» …), без эмодзи — как и весь этот презентер.
- `INTENTIONAL_FLAT` не «пропуск» вовсе: строка читается `Фиксация позиции BTCUSDT
  (SHORT)`.
- Неизвестный код больше не печатается вообще: строка причины опускается, а сырьё
  остаётся в `facts.reason` для разбора.
- Сторона берётся из `pos_side` (его несут факты пропуска), поэтому строка читается
  `Вход BTCUSDT (LONG) пропущен`.

**Чем подтверждено.**

- Живой API с подписанной сессией (`POST /miniapp/api/activity/feed`):
  `Вход BTCUSDT (LONG) пропущен | Причина: сигнал мастера устарел` и такие же строки
  для маржи и стопа — сырых кодов в ответе нет.
- Real Chrome: `series` и `nested` — все вложенные поверхности (Статус, Риск,
  Подключение, Уведомления, Закрытые сделки, Безопасность, Помощь) `noise: []`, rail
  не отрисован; `jitter --seconds 150` — 28 текстовых мутаций, все это числа
  PnL/баланса и минута в «обновлено HH:MM», ни одного предупреждения.
- Тесты: backend **1549 passed / 2 skipped / 87 deselected** (4 новых теста, один из
  них проверяет, что копия покрывает все `SkipReason`), frontend **387 passed**, ruff
  check/format и `git diff --check` чисто.

**Инструмент.** `scripts/audit_miniapp_browser.mjs` больше не может висеть молча:
`Runtime.evaluate` ограничен 15 s, отклонённая сессия и отсутствие элемента дают
внятную ошибку, а минута в подписи свежести исключена из «предупреждений». Грабли:
стаб `data/preview-init.js` переоформлять перед прогоном, иначе гейт сессии выглядит
как зависший UI.

**Осталось.** Ручной проход в реальном Telegram WebView (владелец) и известное
несвязанное `history sync failed account_id=copyc1` (раз в ~5 мин).

### 2026-09-10 — четвёртый проход: разрыв транспорта прочитан одним голосом

Приёмка §6 требовала проверить `LIVE → reconnecting → stale → LIVE` без скачка
layout. Это проверено не только в jsdom, но и настоящим Chrome против живого
бэкенда: страница открыта, бот остановлен (туннель не тронут), затем поднят
заново. Сценарий — `data/audit_transport_break.mjs` (локальный, не коммитится: он
управляет процессом; read-only харнесс в `scripts/` остаётся read-only).

**Что показал замер.**

- Rail появляется один раз и честно: `Нет связи · 41 сек назад`; последние
  данные (PnL, число позиций, заголовок состояния) остаются на экране, экран не
  очищается и не перезагружается.
- До правки в разрыв на экране жили двое: rail и **две** красные плашки «Нет
  связи — Проверьте интернет и попробуйте ещё раз». Это один факт, сказанный
  дважды, причём вторым голосом виноватился интернет пользователя, хотя лёг наш
  собственный процесс. После правки в разрыв плашек **ноль** (`errors: []`).
- Восстановление без единого действия пользователя: через 21 с после старта бота
  rail исчез, чтения снова 200. За весь разрыв — ровно один неудавшийся запрос
  (`dashboard:ERR`): bounded self-heal не устраивает REST storm.

**Что изменено.**

- `railSpeaks(snapshot, hasData)` в `miniapp/src/live.ts`: пока rail показывает
  разрыв и на экране есть последние данные, экран не повторяет ту же ошибку своей
  плашкой. Если данных нет, плашка остаётся единственным объяснением и не
  скрывается.
- Правило применено в Home, Positions, Activity, TradeList (история и открытые),
  PositionDetail и TradeDetail (включая ошибку чтения деталей). Ошибка **действия**
  (пауза/возобновление, сохранение) показывается всегда — её rail не заменяет.
- Новый хук `useLiveSnapshot`; `useTransportStale` переведён на него.
- Шаги подключения перестали быть англоязычными: `Wallet`/`Sync` →
  `Кошелёк`/`Сверка` (`miniapp/src/trustSteps.ts` — один источник для Home, Статуса
  и визарда подключения; `Agent`/`Builder` остаются именами протокола).

**Чем подтверждено после правок.**

- `data/audit_transport_break.mjs`: разрыв — `Нет связи · 41 сек назад`, плашек 0,
  данные на месте; восстановление — 21 с, rail пуст, чтения 200.
- Real Chrome `nested`: все вложенные экраны `noise: []`; Статус читается
  `Всё работает · Проверка: 16:03 · только что`, рельс — `Кошелёк / Agent /
  Builder / Сверка`.
- frontend **33 файла / 392 passed** (5 новых тестов на правило одного голоса и на
  `railSpeaks`), oxlint clean, production build PASS; backend **1549 passed /
  2 skipped / 87 deselected**; ruff, doc-гейты и `git diff --check` чисто.
- Позиции: мастер закрылся на бирже по стопу в 15:55:41, движок реплицировал
  закрытие на подписчиков; `AUDIT: FLAT-ALL` (все 5 аккаунтов — 0 позиций, 0
  ордеров). Торговых команд из сессии не отправлялось.

**Осталось.** Ручной проход в реальном Telegram WebView (владелец) — и только он:
всё, что можно измерить машиной, измерено.

### 2026-09-10 — пятый проход: экран говорит ровно один раз

Четвёртый проход снял дубли на Главной. Тот же вопрос, заданный каждому экрану
по отдельности, нашёл ещё три места, где пользователь получал лишнее — причём
два из них замерены, а не увидены глазами.

**Что нашли замеры.**

- **Позиции во время разрыва.** `data/audit_transport_break.mjs 1` (страница
  открыта на вкладке Позиции, бот остановлен, туннель не тронут): rail «Нет связи ·
  38 сек назад» **и** `warnings: ["Снимок позиций не обновляется. Статус защиты
  может быть неполным."]` — один факт дважды. Причина: экранный флаг `stale`
  взводился от `transportUnhealthy`, то есть от любой ошибки чтения, а rail
  говорит только о подтверждённом разрыве.
- **Закрытые сделки.** `jitter --tab 1 --closed --seconds 95`: 15 текстовых мутаций,
  из них «Загружаем…» появлялось и исчезало на **каждом фоновом перечитывании**. Кнопка
  «Вся история»/«Показать ещё 20» брала состояние из `history.loading`, а он взводится
  и TTL-перечиткой, которую пользователь не запускал; `.btn:disabled` ещё и гасит её
  до 55% прозрачности. То есть кнопка мигала и тускнела сама по себе.
- **Статус системы.** На здоровом аккаунте строка `Разрешения` показывала оранжевое
  «Устарели», пока идёт автопроверка при открытии экрана (TTL доказательств — 60 с).
  Экран сам перепроверяет права, поэтому это был нагоняй, а не информация.

**Что изменено.**

- Правило одного голоса распространено на «снимок устарел»: плашка о снимке
  показывается только когда движок сам пометил снимок устаревшим (`is_stale`) и rail
  молчит. Разрыв транспорта сообщает rail, ошибку чтения без данных — error box.
- `PositionSummaryCard` больше не подменяет реальное состояние стопа на «Статус стопа
  не обновлён»: по двадцать раз на экран это был шум, а не безопасность.
- Новый `usePaging()`: подпись «Загружаем…» и затемнение кнопки — только у того
  нажатия, которое сделал пользователь. Фоновая перечитка больше не трогает контрол
  (ActivityScreen, ClosedTrades, «Ещё события»/«Ещё исполнения» в деталях сделки).
- `permissionsRowValue('stale')` → спокойное «Проверены ранее» (muted) вместо
  оранжевого «Устарели»: возраст несёт строка `Последняя проверка`, а сорвавшаяся
  проверка по-прежнему читается как `Проверка прав устарела`.
- Харнесс научился мерить поверхность, а не только первую вкладку: `--tab N`,
  `--closed` (ровно то нажатие «Открытые → Закрытые») и режим `status` (сэмплит
  Статус 24 с после открытия: перепроверяет ли приложение права само).

**Чем подтверждено (real Chrome + живой бэкенд).**

- Позиции, разрыв: rail «Нет связи · 42 сек назад», `warnings: []`, `errors: []`,
  последние данные на экране; восстановление через 21 с после старта бота без
  действий пользователя.
- Закрытые сделки 95 с: было 15 мутаций с миганием кнопки — стало **5** мутаций и
  **0** миганий «Загружаем…»; Активность 90 с — 4 мутации, 0 миганий; Позиции
  110 с — 6 мутаций. Во всех случаях меняются только числа баланса/PnL и минута в
  «обновлено HH:MM».
- Режим `status`: сразу после открытия — `Разрешения: Подтверждены`, `Проверка:
  16:32 · только что` и так все 24 с наблюдения (то есть экран перепроверяет права
  сам, а не просит пользователя).
- `nested` после правок: все вложенные экраны `noise: []`, включая Статус.
- Тесты: frontend **34 файла / 396 passed** (новый `pagingFeedback.test.tsx`
  проверен на то, что он падает со старым поведением кнопки; 3 новых теста на
  один голос и спокойную строку разрешений), oxlint clean, build PASS; ruff,
  doc-гейты и `git diff --check` чисто.

**Осталось.** Только ручной проход в реальном Telegram WebView (владелец).

**Геометрия rail (измерено, чтобы критерий §6 «без скачка layout» не остался
словами).** `contentTop 0 → 30`, первая строка `132 → 162` при появлении rail, и
ровно обратно при восстановлении: полоса занимает свои 30 px в потоке, как
собственная плашка «нет связи» в Telegram. Это одно сдвижение на настоящий
разрыв и одно на возврат — не мигание: за 150 s здоровой работы (три замера на
разных вкладках) rail не появился ни разу. Альтернативы отклонены сознательно:
постоянно зарезервированные 30 px — это вечная пустая полоса, а оверлей закрыл бы
шапку с адресом кошелька.

# Stage 3 Part 1 — corrective plan после независимого code/UI review

Дата ревью: 2026-08-31
Ветка: `codex/stage-3-mini-app-part1`
Статус: **REOPENED / REQUEST CHANGES**
Scope: Builder-auth Telegram Mini App, от `prepare` до paused account и
управления Pause/Resume. Mainnet не входит в этот gate.

## 1. Итоговая оценка

Текущий happy path работает: пользователь подписывает комбинированный
`ApproveAgent`, Builder создаёт отдельный signer, backend выполняет preflight,
атомарно сохраняет credentials в состоянии paused, Mini App показывает Home.
Однако реализацию нельзя считать закрытой из-за шести блокирующих классов
риска:

1. попытка онбординга и сгенерированный signer живут только в RAM и удаляются
   до первого внешнего вызова;
2. после принятого биржей `ApproveAgent` локальный сбой оставляет невосстановимый
   авторизованный Agent;
3. committed account после ошибки runtime activation не имеет рабочего пути
   восстановления во frontend;
4. риск отображается в 100 раз меньше фактического значения;
5. Home выдаёт кэш за обновлённые данные и допускает Resume без обязательного
   подтверждения и без fail-closed freshness gate;
6. текущий signed testnet тест превышения комиссии может доказать только факт
   произвольного отклонения ордера, а не нарушение именно Builder fee cap.

Оценка текущего состояния: **6/10**. Архитектурные границы в целом удачны,
криптографический happy path и атомарный credential commit сделаны хорошо, но
recovery и live evidence пока недостаточны для финансового control-plane.

### Масштаб и принцип достаточности

План рассчитан на фактический продукт, а не на биржевую инфраструктуру уровня
крупного брокера:

- первые месяцы — ориентировочно 10–30 пользователей;
- практический верх текущей архитектуры — около 100 followers;
- один доверенный процесс, SQLite и контролируемый Telegram-доступ;
- текущая разработка и все state-changing проверки идут только в Aster
  Testnet на ненастоящих BNB/ASTER/USDT.

Поэтому план не требует high-availability кластера, распределённых локов,
очередей, отдельного garbage-collector daemon, сложной event-sourcing модели,
полного pentest/compliance gate или идеальной стерильности testnet-счетов.
Оставшаяся testnet-позиция — неудобство для следующего теста, но не финансовый
инцидент и не основание откладывать live testing.

## 2. Definition of Done corrective gate

Stage 3 Part 1 можно снова закрыть, если выполнены продуктово значимые пункты:

- закрыты все пункты `обязательно` из раздела 4;
- повторный `submit` и рестарт процесса не создают второго Agent и не теряют
  единственный ключ Agent;
- remote-success/local-failure восстанавливается после рестарта без повторного
  `ApproveAgent`;
- committed-but-not-activated account виден как отдельное recovery-состояние и
  переводится в Home;
- Home показывает фактический риск и время фактического snapshot;
- Resume всегда требует confirmation и запрещён при stale/degraded/blocked
  runtime;
- live fee-cap тест доказывает именно builder cap, а не другую ошибку ордера;
- ключевые offline, frontend и signed-live сценарии зелёные;
- состояние testnet-счетов после кампании выведено в отчёт; cleanup выполняется
  по возможности, но его неуспех сам по себе не отменяет результат проверки;
- актуальные verdict и counters записаны только в
  `docs/ENGINE_TEST_STATUS_2026-08.md`.

## 3. Неизменяемые инварианты

- Основной ключ пользователя используется только в wallet provider/test
  harness для EIP-712. Backend, БД и логи его не получают.
- Signer private key до credential commit хранится только зашифрованно.
- Подпись пользователя не сохраняется в БД и не логируется.
- Любой новый аккаунт и любой recovery завершаются с
  `allow_new_entries=false`.
- Внешний успех не откатывается выдуманным локальным rollback. Он должен быть
  обнаружен signed-read проверкой и завершён идемпотентно.
- Testnet host и chain guards обязательны. Любой mainnet URL/chain — hard fail
  до первого state-changing запроса.
- Live-тесты разрешено запускать независимо от наличия автоматического cleanup.
  Перед сценарием тест сам приводит нужный аккаунт в требуемое исходное
  состояние либо явно учитывает существующую testnet-позицию.

## 4. Реестр необходимых исправлений

| ID | Решение для текущего масштаба | Проблема | Основные файлы |
|---|---|---|---|
| C1 | **Обязательно** | RAM-only, one-shot lifecycle теряет signer/retry | `onboarding/builder.py`, `storage/database.py`, repository |
| C2 | **Обязательно, упрощённо** | Истёкшие pending attempts не очищаются | те же файлы; cleanup при boot/следующем prepare |
| C3 | **Обязательно** | Activation recovery backend/frontend расходятся | `application/models.py`, `application/service.py`, `types.ts`, `App.tsx` |
| C4 | **Обязательно** | Risk percent отображается в 100 раз меньше | `format.ts`, `format.test.ts` |
| C5 | **Обязательно** | Кэшированный Home, ложный timestamp, unsafe Resume | service/server/Home files |
| C6 | **Обязательно** | Signed fee-cap test проверяет «любую ошибку» | `test_builder_mini_app_e2e.py` |
| C7 | **Обязательно, дёшево** | Недостаточная config validation | `config.py`, `onboarding/builder.py` |
| C8 | **Рекомендуется** | Wizard не показывает post-sign verification | `BuilderWizard.tsx`, `wizard.ts` |
| C9 | **Обязательно** | Все session errors названы invalid session | `App.tsx`, `errors.ts`, `api.ts` |
| C10 | **Рекомендуется** | Нет timeout/abort frontend requests | `api.ts`, `api.test.ts` |
| C11 | **Рекомендуется** | В client diagnostics проходит произвольный текст | frontend/server/builder logging |
| C12 | **Рекомендуется** | WalletConnect session не освобождается | `wallet.ts`, wizard lifecycle |
| C13 | **Обязательно, дёшево** | Public URL расходится с server route | env/README/tunnel/server |
| C14 | **Отложить** | Preview DB snapshot неатомарен | `scripts/preview_miniapp.py` |
| C15 | **Рекомендуется минимум** | Нет базовых static security headers | `miniapp/server.py` |
| C16 | **Рекомендуется минимум** | Sheet/focus/touch-target gaps | UI/CSS/screens |
| C17 | **Рекомендуется** | Визуальный drift от concepts | UI components/assets/CSS |
| C18 | **Обязательно** | Raw/неточный system copy | presenter/application/Home |
| C19 | **Предупреждение, не blocker** | Dev unsigned + public tunnel | startup/config docs |
| C20 | **Отложить** | Отдельный local abuse limiter | вернуться при публичном доступе/росте |
| C21 | **Обязательно** | Living docs объявляли Part 1 закрытой | DEV plan/test status/architecture/docs map |

## 5. Порядок реализации

Исправления должны идти в следующем порядке. Cleanup testnet-счетов не является
нулевым gate и не задерживает live testing.

1. **WP1 — простой durable Builder lifecycle.** Одна таблица, repository,
   restart recovery.
2. **WP2 — committed/runtime recovery contract.** Backend DTO, session routing,
   frontend recovery state.
3. **WP3 — корректность Home.** Процент, fresh snapshot, mutation
   gates, confirmation.
4. **WP4 — frontend/network/security hardening.** Ошибки, timeout, log boundary,
   WalletConnect cleanup, config guards.
5. **WP5 — UI fidelity и базовая accessibility.** Без отдельной enterprise
   certification campaign.
6. **WP6 — signed-live campaign.** Её можно вести параллельно с исправлениями,
   повторяя затронутые сценарии после фиксов.
7. **WP7 — повторный review и closure docs.** Только после ключевых
   evidence.

---

## 6. Live-test helper — полезная гигиена, не blocker

### Причина

Testnet-позиции и testnet-токены не имеют денежной ценности. Поэтому оставшаяся
после падения теста позиция не является риском проекта. Cleanup полезен только
для повторяемости: следующий тест проще начать с известного состояния.

### Где изменить

- `tests/testnet/test_builder_mini_app_e2e.py`;
- при повторении кода добавить небольшой helper в
  `tests/testnet/support/builder_live.py`; отдельный framework не нужен.

### Что изменить

1. В начале сценария читать `positionRisk`, `openOrders` и position mode.
2. Если тесту нужен flat, выполнить обычный best-effort cancel/close. Если
   очистить не удалось, можно выбрать другой testnet account либо явно упасть
   с понятной причиной; это не общий запрет live suite.
3. В `finally` попытаться отменить созданные тестом ордера и закрыть его
   позицию. Ошибку cleanup вывести отдельно, не подменяя ею результат основной
   проверки.
4. Сохранить разумные маленькие `SYMBOL`/quantity constants, чтобы не упираться
   в тестовый баланс и биржевые лимиты.
5. Перед первым POST проверить точный host
   `fapi.asterdex-testnet.com`, trading chain `714`, registration chain `56`.
6. Можно переиспользовать существующие wallet sections. Отдельный аккаунт нужен
   только если сценарии реально конфликтуют по состоянию.
7. Не печатать signature, private key, полный user/signer/builder address и raw
   signed URL. Диагностика — masked ids, endpoint name, stable error code.

### Тесты WP0

- Отдельный большой unit-suite для helper не нужен.
- Достаточно одного live smoke: helper видит текущее состояние, при возможности
  приводит его к flat и печатает итоговый snapshot.
- State-changing live tests можно запускать до появления helper.

### Acceptance

Основные signed-live сценарии выполняются и дают проверяемые assertions.
Best-effort cleanup делает повторные прогоны удобнее, но не является acceptance
критерием функциональности.

---

## 7. WP1 — простой durable Builder onboarding lifecycle

### Причина

`BuilderOnboardingService.submit()` удаляет `_PendingApproval` из `_pending`
до `ecrecover`, POST `/approveAgent`, signed verification, preflight, DB commit
и runtime activation. Любой сбой делает тот же `pending_id` неповторяемым.
После remote success ключ signer теряется вместе с процессом, хотя Agent
остаётся авторизован на бирже.

### 7.1 Миграция и repository

#### Где изменить

- `src/copytradeaster/storage/database.py` — append-only migration;
- `src/copytradeaster/storage/repositories.py` — новый repository и DTO row;
- существующий `src/copytradeaster/credentials/crypto.py` — переиспользовать
  без нового crypto protocol;
- `src/copytradeaster/application/adapters.py` — durable store adapter;
- `tests/unit/storage/` и `tests/unit/onboarding/test_builder.py`.

#### Новая таблица `builder_onboarding_attempts`

Это не event store и не универсальный workflow engine. Нужна одна небольшая
таблица, чтобы не потерять signer между `prepare`, remote approval и commit.

Минимальные поля:

- `pending_id` — primary key, случайный opaque id;
- `telegram_user_id` — владелец;
- `account_id` — стабильный будущий id аккаунта;
- `user_address`, `signer_address` — canonical/checksummed;
- `encrypted_payload`, `algo` — зашифрованный signer private key и точный
  canonical signing payload через уже существующий AES-GCM helper;
- `state` — CHECK enum;
- `approval_deadline_ms`, `agent_expires_at_ms`;
- `created_at`, `updated_at`;
- `last_error_code` — только stable internal category;

Не хранить: main key, wallet signature, raw exchange response, signed URL.

Обычного `UNIQUE(telegram_user_id)` достаточно для текущей модели «один
аккаунт на пользователя». Отдельные history/tombstone таблицы не нужны.

#### Состояния

Достаточно четырёх состояний:

`PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`

- `APPROVAL_SENT` означает: POST начат, его исход может быть неизвестен; перед
  повтором сначала проверить `GET /agent`;
- preflight block остаётся `APPROVED` с `last_error_code`;
- activation recovery определяется по committed account/runtime, а не отдельным
  десятком состояний onboarding table;
- истёкший `PREPARED` удаляется.

### 7.2 Алгоритм `prepare`

#### Где изменить

`src/copytradeaster/onboarding/builder.py`, методы `prepare()`,
`_drop_user_pending()` и новые recovery helpers.

#### Что изменить

1. До генерации signer искать активную попытку владельца.
2. Если она `PREPARED` и не истекла — вернуть тот же payload/pending id.
3. Если `APPROVAL_SENT`/`APPROVED` — вернуть typed recovery result, не
   создавать новый Agent.
4. Новый signer и payload записать зашифрованно одной транзакцией до ответа
   клиенту.
5. RAM map можно удалить полностью либо оставить как cache. Источник истины —
   SQLite.
6. Exact EIP-712 payload после prepare неизменяем: повторное чтение возвращает
   тот же agent address, nonce, expiry, builder и max fee.

### 7.3 Алгоритм `submit`

#### Где изменить

`src/copytradeaster/onboarding/builder.py`, `submit()`, verification helpers и
store protocol в `src/copytradeaster/onboarding/models.py`.

#### Что изменить

1. Загрузить attempt по `(pending_id, telegram_user_id)` без удаления.
2. Проверить локальный approval TTL до remote POST.
3. Выполнить `ecrecover`; mismatch оставляет `PREPARED`, чтобы пользователь мог
   повторить подпись до TTL.
4. Под существующим per-user `asyncio.Lock` записать
   `PREPARED -> APPROVAL_SENT` до POST. Для одного процесса этого достаточно;
   distributed lock/optimistic version не нужен.
5. Известное exchange rejection вернуть в `PREPARED` или typed terminal state
   согласно коду. Не логировать raw `msg`.
6. Timeout/connection reset/5xx оставляют `APPROVAL_SENT`.
7. В `APPROVAL_SENT` и после рестарта сначала выполнить signed `GET /agent`
   с сохранённым signer. Только доказанное отсутствие Agent позволяет повторить
   POST; подтверждённый Agent переводит attempt в `APPROVED`.
8. После `APPROVED` никогда не требовать новую wallet signature для
   preflight/commit retry.
9. Preflight business block (`non-flat`, open orders, wrong mode, unsafe
   permission) сохраняет `APPROVED` и ключ до remediation.
10. Credential commit должен быть идемпотентным по `account_id` и
    `signer_address`. После успеха attempt отмечается `COMMITTED`.
11. После durable credential commit удалить attempt row: отдельный audit
    tombstone для 10–100 пользователей не нужен.
12. Activation — отдельный повторяемый шаг. Его сбой не превращать в новый
    onboarding и не повторять approveAgent.

### 7.4 Expiry и garbage collection

#### Причина

Текущий `_pending` очищается только при следующем `prepare` того же user или
при `submit`. Брошенные attempts и signer keys остаются в памяти процесса
неограниченно.

#### Что изменить

- при startup и перед новым `prepare` удалить истёкшие `PREPARED` rows;
- `APPROVAL_SENT`/`APPROVED` не удалять по короткому approval TTL: они нужны для
  recovery до commit либо истечения Agent;
- отдельный periodic worker/sweeper не нужен при текущем масштабе;
- если накопятся старые approved attempts, достаточно admin/CLI cleanup команды
  или очистки при следующем обращении пользователя;
- после `COMMITTED` row удаляется, signer уже находится в credentials storage.

### Тесты WP1

Unit:

- submit с неправильной подписью, затем правильной тем же pending id;
- два конкурентных submit в одном процессе — один remote POST;
- timeout POST -> `APPROVAL_SENT` -> GET подтверждает success -> commit;
- timeout POST -> GET подтверждает absence -> один безопасный retry;
- remote success -> preflight fail -> remediation -> тот же signer commit;
- remote success -> commit exception -> retry после рестарта;
- commit success -> activation fail -> новый process завершает activation;
- TTL до POST удаляет secret; TTL после remote success secret не удаляет;
- startup/next-prepare cleanup истёкшего PREPARED идемпотентен;
- encrypted payload не содержит private key/signature в plaintext;
- migration применяется с пустой и существующей Stage 2 БД.

Integration:

- полный `MockTransport` flow с restart после `APPROVAL_SENT`, `APPROVED` и
  credential commit;
- настоящий SQLite, новый экземпляр `Database`, credentials cache reload;
- fault injection на точных границах after-remote, before-commit,
  after-commit, before-activation.

Live: ключевые recovery-сценарии раздела 15; не требуется отдельный live case
на каждую строку state machine.

### Acceptance WP1

После любого kill/restart существует ровно один из двух доказуемых исходов:
Agent не создан и локальный key уничтожен; либо Agent создан и его key доступен
для signed recovery/credential commit. Третьего состояния «Agent есть, ключа
нет» нет.

---

## 8. WP2 — recovery committed account и единый session contract

### Причина

Backend уже умеет возвращать `pending_id=None, already_connected=True`, но
frontend type требует строковый `pending_id` и не знает `already_connected`.
Session считает строку в БД полностью подключённым аккаунтом, даже если runtime
activation не завершилась, и сразу отправляет пользователя в Home.

### Где изменить

- `src/copytradeaster/application/models.py` — session/prepare/recovery DTO;
- `src/copytradeaster/application/service.py` — `miniapp_session()`,
  `_recover_builder_runtime()`, builder prepare/submit paths;
- runtime registry interface — read-only `is_account_registered(account_id)`;
- `src/copytradeaster/miniapp/server.py` — JSON contract/status mapping;
- `miniapp/src/types.ts`, `App.tsx`, `BuilderWizard.tsx`;
- `tests/integration/test_miniapp_builder_flow.py`;
- `tests/unit/miniapp/test_server.py` и frontend state tests.

### Что изменить

1. Добавить явный connection state, например:
   `empty | onboarding | recovery_required | connected`.
2. `connected` означает одновременно durable enabled account, decryptable
   credentials и опубликованный runtime.
3. Account row без runtime возвращает `recovery_required`, а не Home.
4. DTO формально описывает nullable `pending_id` и `already_connected` либо
   заменяет их discriminated union. Backend и TypeScript должны быть
   структурно одинаковы.
5. Recovery endpoint вызывает `recover_account_runtime` под account lock,
   повторно проверяет paused policy и затем возвращает fresh session.
6. Любая recovery ошибка оставляет durable `allow_new_entries=false`.
7. App показывает отдельный экран «Подключение сохранено, завершаем запуск» с
   Retry. Не просит снова подключать wallet и не создаёт новый signer.
8. Boot path должен публиковать все valid enabled credentials либо явно
   оставлять их в `recovery_required` с диагностируемой stable category.

### Тесты WP2

- DTO contract tests Python ↔ fixture JSON ↔ TypeScript parser;
- session: account row есть/runtime нет;
- recovery success, failure, double click, process restart;
- activation failure после commit не ведёт в wizard;
- policy остаётся paused во всех failure paths;
- live L4: принудительный local activation failure после реального remote
  approval/commit, restart, recovery, paused Home.

---

## 9. WP3 — корректность Home, freshness и Resume

### 9.1 Исправить отображение риска

#### Причина

Domain хранит долю: `0.01` означает 1%. Telegram formatter умножает на 100,
Mini App — нет. Скриншот поэтому показывает `0.01%` вместо `1%`.

#### Где изменить

- `miniapp/src/format.ts`, функция `formatPercent`;
- `miniapp/src/format.test.ts`;
- проверить все вызовы в `HomeScreen.tsx` и будущих risk components.

#### Что изменить

- сохранить transport contract fractional;
- умножать на 100 только в presentation formatter;
- использовать достаточную точность для `0.0001` и не округлять допустимую
  Builder fee до `0%`;
- тесты обязательно на `0`, `0.0001`, `0.01`, `0.015`, `1` и null.

### 9.2 Настоящий refresh и честный timestamp

#### Причина

`dashboard()` вызывает `_snapshot(refresh=False)`, а `updated_at` берёт из
account row. Кнопка «Обновить данные» повторяет тот же cached read и показывает
время создания/изменения аккаунта как время рыночного snapshot.

#### Где изменить

- `src/copytradeaster/application/service.py` — dashboard/status use case;
- snapshot model/repository, если `captured_at` ещё не доступен наружу;
- `src/copytradeaster/miniapp/server.py` — отдельный refresh route или явный
  query contract;
- `miniapp/src/api.ts`, `types.ts`, `HomeScreen.tsx`.

#### Что изменить

1. Initial dashboard может использовать bounded cache, но user refresh обязан
   вызвать `_snapshot(refresh=True)`.
2. DTO возвращает `snapshot_captured_at`, `freshness_state` и при необходимости
   `refresh_in_progress`; не подменяет их account timestamp.
3. Failed refresh сохраняет предыдущие данные визуально, но маркирует их stale
   и блокирует state-changing действия.
4. Кнопка имеет loading/disabled state и защищена от parallel clicks.
5. `system_state=stale` не переводить текстом в «обновляется», если polling
   реально не идёт.

### 9.3 Fail-closed Resume и confirmation

#### Причина

Frontend выбирает action только по `copy_state`; backend требует confirmation
только если у мастера есть открытая позиция. UX-план требует confirmation
всегда, а stale runtime не должен разрешать mutation.

#### Где изменить

- `src/copytradeaster/application/service.py`: `resume()`,
  `begin_confirmation()`, `_resume_account()`;
- status/heartbeat provider;
- `miniapp/src/home.ts`, `HomeScreen.tsx`, Sheet component;
- Python integration и frontend component tests.

#### Что изменить

1. До изменения policy потребовать `system_state=READY`, fresh account snapshot,
   valid agent permission и runtime registration.
2. Любой Resume требует short-lived one-shot confirmation token.
3. Если master flat — обычное подтверждение «разрешить новые входы».
4. Если master in position — усиленное предупреждение, что targeted reconcile
   может открыть follower по текущей цене. Убрать фразу «Это безопасно».
5. Confirmation создаётся до вызова Resume; frontend сначала показывает Sheet,
   затем передаёт token.
6. `stale`, `degraded`, `blocked`, expired permission и global emergency
   показывают remediation и не меняют `allow_new_entries`.
7. Если runtime publication/reconcile падает после policy write, компенсирующий
   путь возвращает durable paused и это проверяется повторным DB read.

### Тесты WP3

- Python: fresh/stale/degraded/blocked matrix; confirmation absent/expired/
  reused/wrong user; master flat/in-position; compensation after runtime fail.
- Frontend: confirmation always; warning variants; no action while stale;
  refresh loading/error/old-data state.
- Live: L6–L7.

---

## 10. WP4 — frontend/network/security hardening

### 10.1 Wizard state после подписи

Файлы: `miniapp/src/screens/BuilderWizard.tsx`, `wizard.ts`, `types.ts`.

- После wallet signature реально dispatch `signed` и показывать Sync/verify
  step, а не продолжать говорить «подпишите» во время backend submit.
- Рендерить backend `signature_plan`, а не зашивать предположение о количестве
  wallet prompts.
- Показывать countdown только для local approval TTL. После remote confirmation
  не предлагать подписать заново.
- Различать user rejection, expired approval, remote unknown, preflight block и
  activation recovery.
- Тестировать reducer и компонент, включая двойной click и background/resume.

### 10.2 Session/API errors

Файлы: `miniapp/src/App.tsx`, `api.ts`, `errors.ts`, `SessionGate.tsx`.

- Только HTTP 401/typed Telegram auth error означает invalid session.
- Network offline, timeout, 429 и 5xx получают отдельные retryable states.
- Добавить `AbortController` и bounded timeout для каждого request.
- Abort при unmount/navigation не показывается как backend failure.
- Retry использует новый controller и не дублирует предыдущий mutation.
- Для submit retry опираться на server attempt state, не слепо повторять POST.

### 10.3 Safe client diagnostics

Файлы: `BuilderWizard.tsx`, `src/copytradeaster/miniapp/server.py`,
`src/copytradeaster/onboarding/builder.py`.

- Frontend отправляет только whitelist `event`, stable `code`, provider type и
  phase. Не передаёт `error.message`, nested cause, address или signature.
- Server отклоняет неизвестные event/code, удаляет control chars, ограничивает
  длину и пишет ожидаемые user events не всегда на WARNING.
- Builder logging не включает `str(ManagementError)`; только endpoint,
  internal category и numeric exchange code.
- Достаточно тестов на newline/control chars, oversized payload и отсутствие
  raw provider/exchange message; отдельный fuzzing campaign не нужен.

### 10.4 WalletConnect lifecycle

Файлы: `miniapp/src/wallet.ts`, `BuilderWizard.tsx`, `App.tsx`.

- Вызывать adapter disconnect после terminal success/cancel и при явной смене
  wallet.
- Не disconnect во время активного external-wallet roundtrip или recoverable
  submit.
- На unmount корректно снимать listeners; повторный connect не умножает
  callbacks.
- Тесты connect/sign/reject/retry/success/unmount.

### 10.5 Config validation

Файлы: `src/copytradeaster/config.py`, `onboarding/builder.py`, config tests.

- `registration_chain_id` строго `56` для текущего testnet Builder flow;
- `builder_address` — валидный checksummed EVM address;
- `builder_name` — непустая bounded строка без control chars;
- `max_fee_rate` — finite non-negative `Decimal`, не выше product cap текущего
  gate; запрет exponent/NaN/Infinity и двусмысленной сериализации;
- разумные min/max для approval TTL и Agent expiry;
- base URL без path/query/userinfo, только точный testnet host;
- config failure происходит на startup, а не после wallet signature.

### 10.6 Dev unsigned mode

Файлы: config, app startup, `miniapp/server.py`, `.env.example`.

- Печатать заметный startup warning, если `miniapp_dev_unsigned=true` работает
  вместе с public tunnel. В текущем testnet это осознанный dev-режим, не blocker.
- В production/mainnet profile такой режим должен быть запрещён, но отдельная
  система environment profiles не нужна в Part 1.
- Dev header никогда не имеет приоритета над валидным production initData.
- Достаточно config test на warning и запрет в явно production profile.

### 10.7 Local abuse control — отложить

При 10–30 приглашённых Telegram users отдельные per-IP/per-route limiters
избыточны. Существующих Telegram HMAC/access checks и per-user onboarding lock
достаточно. Вернуться к limiter, если Mini App станет публичной, появятся
реальные признаки abuse или нагрузка приблизится к сотням одновременных
запросов. Не блокирует Part 1.

### 10.8 Public URL и static security

Файлы: `miniapp/README.md`, `.env.example`,
`scripts/dev-miniapp-tunnel.sh`, `miniapp/server.py`.

- Канонический URL должен заканчиваться `/miniapp/`; startup нормализует либо
  fail-fast объясняет path.
- Опционально root делает 302 на `/miniapp/`, но API routes не должны
  случайно публиковаться под root.
- Добавить дешёвый baseline: `X-Content-Type-Options: nosniff`,
  `Referrer-Policy` и корректный cache policy.
- CSP добавлять только после проверки фактических Telegram/Reown origins в
  реальном WebView; не ломать рабочий WalletConnect ради формального header.
- Не использовать `X-Frame-Options: DENY`, несовместимый с Telegram WebView;
  frame policy проверить в реальном Telegram.
- Hashed assets: immutable cache; `index.html`: no-cache.
- Один server test проверяет route, baseline headers и cache policy.

### 10.9 Preview SQLite — отложить

Файл: `scripts/preview_miniapp.py`.

`scripts/preview_miniapp.py` — dev-only инструмент. Его SQLite backup можно
исправить отдельным небольшим follow-up, но это не влияет на Builder onboarding
или работу 10–100 users и не входит в acceptance Part 1.

---

## 11. WP5 — UI fidelity и accessibility

### Причина

Текущие реальные скриншоты узнаваемо следуют тёмной Aster-палитре и общей
иерархии concepts, но это не pixel/interaction complete реализация. Основные
отклонения: отсутствуют connector lines progress rail, используются системные
emoji, логотип является CSS placeholder, proof block упрощён, environment badge
теряется на Home, нижняя зона Success конфликтует с safe area. Sheet и keyboard
navigation не соответствуют accessibility floor.

### 11.1 Визуальные изменения

Файлы: `miniapp/src/components/ui.tsx`, `index.css`, screens и локальные assets.

- заменить emoji Pause/Play/status на контролируемые inline SVG с одинаковым
  stroke/optical size;
- добавить connector lines между шагами rail, состояния current/complete/error;
- использовать утверждённый wordmark/logo asset; если юридически финальный asset
  отсутствует, явно маркировать временный wordmark и не имитировать его CSS
  кругом;
- вернуть modular grid только в proof/authorization card, не превращать весь
  интерфейс в декоративную сетку;
- сохранять `Aster testnet` badge на Home/Success;
- привести vertical rhythm, card padding, CTA height и footer safe-area к
  реальным iOS/Android viewport;
- длинные адреса/локализованные строки должны использовать `min-width: 0`,
  controlled ellipsis и не выталкивать value за card.

Не добавлять в этот gate Part 2 tabs/screens Positions/Activity/Risk/Settings.

### 11.2 Базовая accessibility

- Sheet: `role=dialog`, `aria-modal`, initial focus, Escape, возврат focus
  opener, labelled title/description;
- все интерактивные targets не меньше 44×44 CSS px;
- добавить явный `:focus-visible` для buttons/links;
- semantic headings вместо стилизованных `div`;
- rail объявляет current step через `aria-current="step"`;
- async errors имеют `aria-live`; busy buttons — disabled/`aria-busy`;
- color contrast проверить для muted text, gold и disabled controls.

### 11.3 UX copy

- Не выводить raw `dashboard.message` пользователю. Маппинг только по typed
  state на русский текст и конкретное действие.
- `stale` — «Данные устарели. Обновите статус», не «обновляется».
- `degraded/blocked` — понятная причина и безопасная remediation без внутреннего
  jargon.
- Success copy исправить: «Снять паузу можно на главной или в боте», потому что
  на самом Success screen кнопки Resume нет.
- В post-sign состоянии объяснять, что подпись уже получена и идёт проверка.

### Тесты WP5

- Точечные component tests на Sheet/confirmation/error, если для этого уже есть
  подходящая тестовая инфраструктура; не вводить тяжёлый UI test stack только
  ради галочки.
- Browser screenshots достаточно на двух representative размерах: узкий
  `360×780` и обычный `390×844`.
- Один реальный owner pass в Telegram на основном устройстве с external wallet;
  второй OS/device полезен, но не блокирует Part 1.
- Отдельный axe/certification/полная матрица zoom/reduced-motion отложены.

---

## 12. C6 — исправить существующий signed fee-cap test

### Где изменить

`tests/testnet/test_builder_mini_app_e2e.py`.

### Что изменить

1. Получить реальный position mode перед negative order.
2. В HEDGE передать корректный `positionSide`; в ONE_WAY не передавать его.
3. Выбрать feeRate немного выше approved `0.0001`, но ниже глобального
   exchange/product maximum. Конкретное значение предварительно подтвердить
   live probe/официальным контрактом; не использовать `0.5`.
4. До POST snapshot positions/open orders. После rejection доказать, что они не
   изменились.
5. Assert делать на конкретный documented/зафиксированный exchange code и
   классифицированную причину cap violation. «Любая ошибка» не принимается.
6. Если биржа неожиданно приняла ордер, тест обязан упасть по функциональной
   причине и затем best-effort закрыть позицию. Успех cleanup не влияет на факт
   обнаруженной регрессии.
7. Positive order at cap и negative above cap должны использовать одинаковый
   валидный order shape; отличается только feeRate.
8. `/builder/userTrades` не считать обязательным доказательством до выяснения
   официальной auth-модели. Warning не может превращаться в зелёный assertion,
   будто statement проверен.

---

## 13. Дополнительные кодовые исправления

### C18 — system messages и presentation boundary

Файлы: application presenter/DTO и `HomeScreen.tsx`.

- Backend возвращает stable state/code, frontend — локализованный copy.
- Не прокидывать сырые English internal messages.
- Builder/permission facts показывать только из verified snapshot.

### C21 — документация

После кода, не заранее:

- `docs/ARCHITECTURE.md`: исправить противоречие, будто Builder path не
  использует credentials boundary; описать простые четыре состояния recovery;
- `docs/DEV_PLAN_SKELETON.md`: corrective checklist и фактический статус;
- `docs/ENGINE_TEST_STATUS_2026-08.md`: единственный актуальный verdict и
  counters;
- `docs/docs_map.md`: ссылка на этот corrective plan;
- исторические `STAGE3_BUILDER_MINI_APP_PART1_2026-08.md` и
  `STAGE3_PART1_REVIEW_GUIDE_2026-08.md` не переписывать задним числом.
  Неточности (неполный commit list, опечатка hash, ранний acceptance) фиксировать
  датированным addendum/new report.

---

## 14. Offline и browser test matrix

### Backend unit

- четыре durable state transition;
- encrypted payload round-trip существующим crypto helper;
- migration upgrade;
- config boundaries;
- Telegram initData valid/stale/future/wrong token;
- client log whitelist/redaction;
- fresh/stale dashboard;
- resume confirmation and compensation;
- percentage formatting покрывается frontend tests.

### Backend integration

- full Builder flow на настоящем SQLite;
- restart после approval и после credential commit;
- remote ambiguous outcomes через transport fault;
- committed/runtime recovery;
- concurrent submit/prepare;
- server route/public path/baseline headers;
- auth/session recovery discriminated union.

### Frontend automated

- formatter с domain-real values;
- session error taxonomy;
- request timeout/abort;
- wizard signed/remote/recovery states;
- always-confirm Resume;
- stale action blocking;
- WalletConnect cleanup;
- Sheet confirmation/focus basics.

### Regression commands

```bash
.venv/bin/python -m pytest -q -m "not testnet"
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest tests/integration/test_miniapp_builder_flow.py -q
cd miniapp && pnpm test && pnpm lint && pnpm build
```

Актуальные количества passed/skipped/coverage записывать только в
`docs/ENGINE_TEST_STATUS_2026-08.md` после фактического прогона.

Live testnet scenarios можно запускать в процессе разработки, не дожидаясь
идеального общего offline gate. Перед финальным verdict все команды выше всё
равно должны пройти.

---

## 15. Signed live testnet campaign

Все тесты ниже используют настоящий `fapi.asterdex-testnet.com`. Поскольку
средства ненастоящие, сценарии можно и нужно выполнять смело. Mock/Fake остаётся
для искусственных timeout/exception, которые трудно воспроизвести на живой
бирже, но не заменяет основные live проверки.

### L1 — текущее состояние аккаунта

- Signed GET: Agent permissions/expiry, position mode, positionRisk,
  openOrders, balance.
- Если конкретному сценарию нужен flat, тест в начале делает cancel/close.
- Existing testnet-позиция не считается инцидентом и не блокирует остальные
  сценарии, которым flat не нужен.

### L2 — fresh Builder happy path

- Использовать существующий подходящий testnet wallet либо новый, если старый
  уже привязан к другому Agent.
- Real `prepare` -> EIP-712 main-key signature в test harness -> real
  `/approveAgent` -> signed preflight -> paused commit -> runtime registration.
- DB check: signer encrypted, main key/signature отсутствуют.

### L3 — recovery после принятого Agent

- После real `/approveAgent` одноразово вызвать локальную ошибку перед commit.
- Перезапустить service с той же временной SQLite.
- Повторный submit/recovery находит Agent через signed `GET /agent`, использует
  тот же signer и завершает paused commit без второго wallet prompt.
- Достаточно одного этого live recovery case. Lost-response/timeout branching
  подробно покрыть integration transport test, не строить для него отдельный
  proxy только ради live галочки.

### L4 — recovery после activation failure

- Одноразовая локальная ошибка после credential commit и до runtime publish.
- Новый session возвращает `recovery_required`.
- Retry публикует runtime и открывает paused Home без нового Agent.

### L5 — preflight block и повтор

- Создать на testnet open order, позицию либо mode mismatch — достаточно одного
  реально поддержанного сценария.
- Agent approval проходит, preflight блокирует commit и сохраняет attempt.
- Устранить условие, повторить submit/recovery тем же signer, получить paused
  account.

### L6 — fee at cap / above cap

- Один валидный order shape для фактического HEDGE/ONE_WAY.
- MARKET order с builder + feeRate ровно approved cap должен пройти.
- Тот же shape с feeRate немного выше cap, но ниже общего exchange maximum,
  должен вернуть конкретную cap-related ошибку.
- Если negative order неожиданно исполнен — это корректно найденная регрессия.
  После assertion можно best-effort закрыть позицию, но оставшийся testnet
  остаток не обесценивает результат теста.

### L7 — Pause/Resume через Mini App

- READY + master flat: confirmation обязателен, Resume меняет policy/runtime,
  Pause возвращает `allow_new_entries=0`.
- stale snapshot: Resume отклоняется до policy mutation.
- Затем мастер может открыть минимальную testnet-позицию: подтвердить усиленный
  warning, targeted reconcile и существующую protection-логику.
- После проверки позиции желательно закрыть, но это post-test hygiene.

Важно: обычный production copy-order пока не инжектирует Builder `feeRate` в
торговое ядро. Поэтому L7 доказывает Resume/control plane, а L6 — отдельную
биржевую Builder authorization. Нельзя описывать их как единый end-to-end
monetized copy cycle, пока Part 2 не добавит builder/feeRate в executor.

### L8 — реальный Telegram/Wallet UX

- Запуск из настоящей WebApp-кнопки private Telegram chat;
- один основной телефон и TokenPocket/фактически используемый wallet;
- wallet rejection, app background/foreground, повторное открытие;
- session/recovery после перезапуска bot process;
- network offline/online достаточно проверить на read-only refresh;
- не снимать screen recording с seed/private data.

В том же проходе сравнить реальные screenshots с concepts, проверить testnet
badge, правильный risk percent, snapshot timestamp, stale copy и обязательный
Resume confirmation. Второй OS/wallet — полезный follow-up, не blocker.

### L9 — optional `/builder/userTrades`

Выполнять как gating только после подтверждения официальной auth-модели или
рабочего Builder Center credential flow. Допустимы два честных исхода:

- endpoint проверен и конкретный order содержит `builderFee`;
- endpoint остаётся non-gating Part 2, а отчёт прямо говорит, что current live
  evidence доказывает authorization/order acceptance, но не fee settlement.

Warning/skip не записывать как passed proof и не блокировать Part 1.

### Итоговый live snapshot

После кампании вывести positionRisk/openOrders/policy/attempt states. Это
диагностика и удобная точка для следующего прогона, а не отдельный независимый
acceptance процесс. Ненулевые testnet-позиции можно закрыть сразу или позже.
Обязательны только две проверки: follower policy не осталась случайно
`allow_new_entries=1`, и логи/БД не содержат main keys/signatures.

### Команды live gate

Точные selectors можно уточнить после разбиения файлов:

```bash
CT_TESTNET=1 .venv/bin/python -m pytest tests/testnet/test_builder_onboarding_recovery_live.py -q
CT_TESTNET=1 .venv/bin/python -m pytest tests/testnet/test_builder_mini_app_e2e.py -q
CT_TESTNET=1 .venv/bin/python -m pytest tests/testnet/test_builder_resume_live.py -q
```

По умолчанию pytest идёт последовательно; отдельный file/session lock для
текущей команды не нужен.

---

## 16. Scope boundary: что не добавлять в corrective Part 1

- Полный 4-tab shell, Positions, Activity, Risk, Settings — Part 2.
- Revoke/replace user flow — Part 2.
- Builder fee statement UI/withdrawal — Part 2.
- Внедрение `builder`/`feeRate` во все production executor orders — Part 2,
  если владелец отдельно не повышает это до acceptance Part 1.
- Mainnet, реальные средства и production rollout запрещены.

Corrective gate должен честно называться: **Builder authorization + paused
credential/runtime onboarding + Mini App control-plane на testnet**. Он не
является полным monetized copy-trading product.

### 16.1 Результат anti-overengineering аудита плана

Из обязательной части удалены или сознательно отложены:

- cleanup testnet-позиций как prerequisite/gate;
- девять lifecycle states, optimistic CAS/version и отдельный crypto AAD
  protocol — заменены четырьмя состояниями и существующим AES-GCM helper;
- periodic sweeper/garbage-collector daemon — достаточно startup и lazy
  cleanup;
- distributed/file locks и отдельный wallet на каждый live case;
- специальный live proxy для принудительного lost response — ambiguity
  достаточно покрыть integration transport test;
- отдельный per-route abuse limiter для закрытого круга до 100 users;
- обязательная строгая CSP до проверки WalletConnect origins;
- исправление dev-only preview SQLite как условие acceptance;
- axe certification, пять viewport, два OS и несколько wallet providers как
  обязательная матрица;
- отдельный финальный audit process, revoke ledger и требование flat/no-orders
  после каждого testnet-сценария.

Оставлены обязательными только исправления, которые прямо влияют на обычного
пользователя или достоверность продукта: не потерять signer/recovery, не
показать риск в 100 раз меньше, не разрешить Resume на заведомо устаревшем
состоянии, не сломать session routing и не выдавать произвольное отклонение
ордера за доказанный Builder fee cap.

## 17. Коммит-план

Каждый пункт — отдельный логический Conventional Commit:

1. `feat: persist builder onboarding attempts`
2. `fix: recover builder approval and runtime activation`
3. `fix: enforce fresh confirmed miniapp resume`
4. `fix: correct miniapp percentage and session errors`
5. `fix: align miniapp wizard routes and diagnostics`
6. `fix: polish builder miniapp mobile interface`
7. `test: expand signed builder recovery and fee-cap coverage`
8. `docs: close stage 3 part 1 corrective gate`

Не смешивать code fixes с переписыванием исторических reports. После каждого
обязательного коммита запускать focused unit/integration tests; полный offline
gate — перед финальным verdict. Live testnet можно использовать после каждого
существенного изменения.

## 18. Финальный независимый review checklist

- Просмотреть diff каждого corrective commit и общий diff от принятой Stage 2
  базы.
- Повторно пройти четыре состояния onboarding и две restart boundaries.
- Проверить, что no-secret assertions основаны не только на `repr=False`, но и
  на БД/log scan.
- Сопоставить Python DTO, JSON response и TypeScript types.
- Проверить real screenshots на двух representative viewport.
- Проверить live evidence: specific response codes, order ids masked и
  before/after snapshots. Cleanup result — справочная информация.
- Убедиться, что `/builder/userTrades` не заявлен проверенным при warning/skip.
- Убедиться, что Part 1 verdict не обещает builder fee внутри обычного engine
  copy-order, если эта интеграция не реализована.
- Только после этого заменить `REOPENED` на `CLOSED` в living docs.

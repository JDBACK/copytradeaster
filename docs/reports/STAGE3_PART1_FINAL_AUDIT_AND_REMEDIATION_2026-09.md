# Stage 3 Part 1 — итоговый code/UI audit и план обязательной доводки

Дата аудита: 2026-09-01

Ветка: `codex/stage-3-mini-app-part1`

Проверенный HEAD: `7b5baba71f9684448beabbc3bf6b14984699a759`

База сравнения: `c2a9584f91bb7de5023cbe87bd86f3ad5ac9a762`

## 1. Вердикт

Stage 3 Part 1 имеет хороший и подходящий проекту фундамент, но corrective gate пока нельзя считать полностью закрытым.

Итоговая оценка текущего состояния: **7/10, условная приёмка после обязательной доводки P1**.

- Архитектура соответствует реальному масштабу 10–30 пользователей, максимум около 100: один async-процесс, SQLite WAL, локальные блокировки и модульный монолит здесь уместны.
- Основные security-инварианты соблюдены: main private key не передаётся сервису; signer создаётся отдельно и хранится зашифрованно; Telegram `initData` валидируется на сервере; аккаунт коммитится на паузе.
- Offline-контур сильный и воспроизводимый.
- Два ключевых recovery-инварианта реализованы не до конца: неизвестный результат удалённого `approveAgent` ошибочно откатывается в `PREPARED`, а настоящий Mini App не умеет продолжить `APPROVAL_SENT`/`APPROVED` без повторной подписи.
- Часть отчётности о live-покрытии завышена: L7/L8 не выполняют сетевых запросов, L3 не моделирует заявленный сценарий «биржа приняла, локальный ответ потерян».

До исправления P1 ветку можно использовать для дальнейшей разработки и Testnet, но нельзя документировать как полностью закрытый production-ready Part 1.

## 2. Что проверено

Полностью прочитаны и сопоставлены:

- `AGENTS.md`;
- `docs/DEV_PLAN_SKELETON.md`, Stage 3;
- `docs/ARCHITECTURE.md` в части затронутых инвариантов;
- `docs/reports/STAGE3_MINI_APP_UX_PLAN_2026-08.md`;
- `docs/reports/STAGE3_BUILDER_MINI_APP_PART1_2026-08.md`;
- `docs/reports/STAGE3_PART1_REVIEW_GUIDE_2026-08.md`;
- `docs/reports/STAGE3_PART1_CORRECTIVE_PLAN_2026-08.md` целиком, включая журнал выполнения;
- история всех 14 коммитов после базы и итоговый cumulative diff: 42 файла, 2807 добавлений, 463 удаления;
- backend, storage, Mini App transport, React UI, testnet tests и документационные изменения;
- reference templates в `docs/reports/assets/stage3-mini-app/reference-templates/` и три фактических скриншота Telegram Mini App.

## 3. Подтверждённые quality gates

### Offline backend

```text
725 passed, 2 skipped, 30 deselected
TOTAL coverage: 87%
ruff check: passed
ruff format --check: passed
git diff --check: passed
```

### Frontend

```text
Vitest: 34 passed
Oxlint: passed
TypeScript + Vite production build: passed
Impeccable mechanical detector: 0 findings
```

### Signed Aster Testnet, полный запуск 2026-09-01

```text
28 passed, 1 failed, 1 skipped in 770.18s
```

Падение: `test_builder_order_with_fee_rate_round_trip` не дошёл до order round-trip из-за ответа Aster `The request has timed out` на подписанный `GET /fapi/v3/positionSide/dual`. Это не доказывает дефект продукта, но доказывает, что заявленный зелёный live gate сейчас невоспроизводим, а helper не умеет безопасно повторять idempotent signed reads.

## 4. Что сделано хорошо

1. Durable attempt хранит signer key и params в AES-GCM payload; подпись main wallet не сохраняется.
2. `APPROVAL_SENT` фиксируется до удалённого POST, что является правильной основой для recovery.
3. Коммит follower-аккаунта создаёт его с `allow_new_entries=False`.
4. Runtime activation failure не откатывает уже созданный paused account; существует отдельный recovery path.
5. Resume path выполняет READY/fresh snapshot/permission проверки и повторные проверки под локами. Эта сложность оправдана торговой корректностью и не является enterprise-overengineering.
6. Сервер не доверяет Telegram user id из тела запроса.
7. UI визуально близок к утверждённому направлению: почти чёрный canvas, champagne accent, ясная иерархия состояний, крупные touch targets, masked addresses, inline SVG и понятный русский copy.
8. Отсутствие полноценных Positions/Activity/Settings экранов не является дефектом Part 1: они явно вынесены в следующую часть.

## 5. Реестр обязательных исправлений

| ID | Приоритет | Область | Решение |
|---|---:|---|---|
| R1 | P1 | Builder remote uncertainty | Не откатывать неизвестный результат POST в `PREPARED` |
| R2 | P1 | Mini App recovery | Продолжать `APPROVAL_SENT`/`APPROVED` без новой wallet signature |
| R3 | P1 | Live evidence | Заменить псевдо-live L3/L7/L8 настоящими проверками или честно переклассифицировать |
| R4 | P2 | Attempt lifecycle | Реально использовать `COMMITTED` и убрать ложную формулировку об атомарности cleanup |
| R5 | P2 | Home freshness | Не подменять время snapshot временем изменения account row |
| R6 | P2 | Resume sheet | Вернуть `.overlay`, Escape и базовое управление фокусом |
| R7 | P2 | Safe logs | Не писать raw remote/internal exception text |
| R8 | P2 | Testnet robustness | Повторять idempotent reads и точно проверять fee-cap rejection |
| R9 | P2 | Docs/evidence | Исправить stale claims, counters и получить скриншоты именно текущего HEAD |

## 6. R1 — корректная семантика неизвестного результата `approveAgent`

### Причина

`BuilderOnboardingService.submit()` сначала сохраняет `APPROVAL_SENT`, но любой `ManagementError` затем переводит попытку обратно в `PREPARED`. `post_signed_registration()` кодирует transport error как `-1006`; HTTP 5xx, malformed response и exchange `-1000` также не доказывают, что удалённая мутация не состоялась.

Сейчас при «биржа приняла Agent, ответ потерян» следующий запрос может повторно отправить `approveAgent`. Это нарушает главный смысл durable recovery.

### Где изменить

- `src/copytradeaster/aster/management_api.py`;
- `src/copytradeaster/onboarding/builder.py`;
- `src/copytradeaster/onboarding/errors.py` при необходимости нового typed code;
- `tests/unit/onboarding/test_builder.py`;
- `tests/integration/test_miniapp_builder_flow.py`.

### Что изменить

1. Явно разделить результаты user-signed POST на:
   - success;
   - definitive business rejection;
   - unknown remote outcome.
2. К unknown отнести как минимум transport timeout/error, HTTP 5xx без доказанного business rejection и exchange unknown codes `-1000`, `-1006`, `-1007`.
3. После unknown оставить durable state `APPROVAL_SENT`; сохранить только безопасный error code/category.
4. На повторном recovery сначала выполнять подписанный `GET /agent`:
   - Agent подтверждён — перейти в `APPROVED` и продолжить preflight/commit;
   - доказанное отсутствие Agent — разрешить переход в `PREPARED`;
   - timeout/unknown read — оставить `APPROVAL_SENT`, вернуть retryable typed error.
5. Удалить `-1000` из ветки, которая трактует ответ как доказанное отсутствие Agent.
6. Не логировать raw `str(exc)`.

### Обязательные тесты

- POST реально меняет fake remote state, затем wrapper бросает `ManagementError(-1006)`; attempt остаётся `APPROVAL_SENT`.
- Новый экземпляр сервиса находит Agent через GET, не вызывает второй POST, коммитит paused account.
- `GET /agent` с `-1000`/timeout не переводит attempt в `PREPARED`.
- Явный business rejection переводит attempt в `PREPARED` и позволяет новую подпись/повтор.
- Concurrent retry одного Telegram user не создаёт второй POST.

### Acceptance

Ни один ambiguous remote outcome не ведёт к blind re-submit.

## 7. R2 — настоящий transport/UI recovery без второй подписи

### Причина

Domain service уже требует signature только для `PREPARED`. Однако application boundary всегда отвергает пустую signature, session скрывает конкретное attempt state, а React маршрутизирует любой `ONBOARDING` обратно в обычный wizard. В результате после рестарта `APPROVAL_SENT`/`APPROVED` пользователь снова подключает кошелёк и подписывает payload.

### Где изменить

- `src/copytradeaster/application/models.py`;
- `src/copytradeaster/application/service.py`;
- `src/copytradeaster/miniapp/server.py`;
- `miniapp/src/types.ts`;
- `miniapp/src/api.ts`;
- `miniapp/src/App.tsx`;
- `miniapp/src/screens/BuilderWizard.tsx` или отдельный компактный recovery state/component;
- unit/integration/frontend tests соответствующих модулей.

### Что изменить

1. Добавить server-owned recovery use case, например `builder_resume_attempt(user_id)`.
2. Endpoint сам получает attempt по authenticated Telegram user id. Клиент не должен выбирать state и не должен иметь возможность обойти signature для `PREPARED`.
3. Разрешать signature-less продолжение только если серверное состояние равно `APPROVAL_SENT` или `APPROVED`.
4. Добавить в session DTO минимальный typed признак `onboarding_state`/`onboarding_recoverable`; не отдавать signer private key, params или signature.
5. На старте Mini App:
   - `PREPARED` — показать существующий wizard и запросить подпись;
   - `APPROVAL_SENT`/`APPROVED` — показать «Проверяем ранее отправленное разрешение» и вызвать recovery без wallet prompt;
   - committed runtime failure — использовать существующий runtime recovery.
6. Retry после transport/API error должен продолжать тот же pending attempt, а не вызывать новый `prepare`.

### Обязательные тесты

- Application service отклоняет signature-less recovery для `PREPARED`.
- Application service принимает recovery для `APPROVAL_SENT`/`APPROVED`.
- HTTP integration: restart между POST и verify, затем `/session` → recoverable → recovery endpoint → connected/paused.
- Frontend: recoverable session не вызывает wallet adapter и `signTypedData`.
- Frontend: PREPARED по-прежнему требует подпись.

### Acceptance

После принятой подписи ни restart, ни потерянный HTTP response не требуют второй подписи пользователя.

## 8. R3 — честное и настоящее live-покрытие

### Причина

- `test_builder_resume_live.py` использует `FakeLiveApp`, fake coordinator, fake snapshot и искусственный signer key; сетевых запросов к Aster нет.
- L3 вручную ставит `APPROVAL_SENT` до удалённого POST, затем подтверждает отсутствие Agent. Это не сценарий «remote accepted, response lost».
- L4 смешивает real onboarding с fake runtime recovery.

### Где изменить

- `tests/testnet/test_builder_onboarding_recovery_live.py`;
- `tests/testnet/test_builder_resume_live.py`;
- `tests/testnet/support/builder_live.py`;
- при необходимости новый `tests/testnet/test_miniapp_builder_http_live.py`.

### Что изменить

1. L3: выполнить настоящий Testnet `approveAgent`, после успешного remote response искусственно выбросить локальный `-1006` до записи `APPROVED`; после пересоздания сервиса подтвердить Agent настоящим signed GET и завершить без второго POST/signature.
2. L7: собрать реальный runtime adapter с настоящим signer/account, выполнять fresh signed account snapshot и permission validation; пройти pause → confirmation → resume через aiohttp Mini App API.
3. HMAC Telegram `initData` в HTTP live test формировать тестовым bot token; user id должен извлекаться только из валидной подписи.
4. L8 разделить честно:
   - automated signed backend/HTTP live scenario;
   - manual Telegram + WalletConnect/TokenPocket acceptance. Ручную проверку не считать автоматическим pytest.
5. Если тест оставляется полностью fake, перенести его в `tests/integration/` и убрать слова `signed live`.

### Acceptance

Каждый тест, названный live, делает проверяемый network call к Aster Testnet. В отчёте automated и manual evidence указаны отдельно.

## 9. R4 — довести attempt lifecycle без лишней архитектуры

### Причина

Таблица и документы объявляют `PREPARED -> APPROVAL_SENT -> APPROVED -> COMMITTED`, но код после account commit сразу удаляет `APPROVED` attempt. `COMMITTED` никогда не записывается, а commit и delete — разные DB-транзакции.

### Правильная для масштаба реализация

Не нужно строить distributed transaction, outbox или отдельный workflow engine.

1. После успешного paused account commit записать attempt state `COMMITTED`.
2. Runtime activation выполнять после этой записи.
3. Удалять `COMMITTED` attempt после успешной активации либо лениво при session/boot recovery.
4. Если crash произошёл между account commit и state update, существующий account является authoritative; recovery должен распознать account и очистить stale `APPROVED` attempt.
5. В документах не называть account commit + attempt cleanup одной атомарной транзакцией, если это фактически не реализовано.

### Тесты

- После account commit и искусственного activation failure attempt имеет `COMMITTED` или детерминированно очищается recovery path.
- Crash-window с account + `APPROVED` attempt не создаёт второй account/signer.
- Повторный session/recovery идемпотентен.

## 10. R5 — честный Home freshness

### Причина

Backend выставляет `snapshot_captured_at` только при `refresh=True`, а UI при его отсутствии показывает `dashboard.updated_at`, то есть время изменения account row. Это не время биржевого snapshot. На первом открытии Home UI может показать свежую минуту рядом с неизвестным количеством позиций.

### Где и как изменить

- `miniapp/src/screens/HomeScreen.tsx`: первый Home load должен запрашивать `dashboard(refresh=true)`. Для 10–100 пользователей один signed read при входе приемлем.
- Не использовать `updated_at` как fallback для надписи «Снимок».
- Если snapshot отсутствует, показывать `Снимок: не получен` и `Открытые позиции: —`.
- `src/copytradeaster/application/service.py`: присваивать `snapshot_captured_at` только фактическому fresh snapshot либо сохранять реальный capture time вместе с cache.

### Тесты

- Первый Home request вызывает fresh snapshot.
- Без snapshot UI не показывает account `updated_at` как freshness.
- После ручного refresh timestamp меняется и соответствует fresh read.

## 11. R6 — вернуть bottom-sheet поведение и базовую accessibility

### Причина

В `Sheet` был удалён `className="overlay"`, хотя CSS `.overlay` остался. Dialog теперь находится в обычном document flow вместо fixed dimmed overlay.

### Где и как изменить

- `miniapp/src/components/ui.tsx`: вернуть `className="overlay"` внешнему dialog container.
- Закрывать по Escape, если передан `onClose`.
- При открытии переводить фокус внутрь dialog и после закрытия возвращать его на кнопку, открывшую sheet.
- `miniapp/src/screens/HomeScreen.tsx`: передать `onClose` и не разрешать фоновые действия во время confirmation.
- Не добавлять тяжёлую modal-библиотеку ради одного sheet.

### Тесты

- Dialog имеет `.overlay`, `role=dialog`, `aria-modal=true` и accessible label.
- Escape/overlay click закрывают его.
- Resume не выполняется без явного подтверждения.

## 12. R7 — безопасные логи

### Где изменить

- `src/copytradeaster/onboarding/builder.py`;
- `src/copytradeaster/application/service.py`;
- тесты safe logging.

### Что изменить

- Вместо `str(exc)` логировать class, numeric code, typed category и correlation fields.
- Не писать raw remote `msg`, response body, signature, EIP-712 payload, full user/signer address.
- Исправить stale module docstring, который всё ещё утверждает, что pending approvals хранятся в памяти.

## 13. R8 — устойчивые и точные Testnet tests

### Что изменить

1. Общий signed-live helper должен ограниченно повторять только idempotent GET/read после timeout/`-1000`/`-1006`/`-1007`, каждый раз с новым nonce.
2. Для state-changing order POST использовать уникальный client order id и resolve unknown outcome через query; это нужно для детерминизма теста, а не из-за ценности Testnet-средств.
3. Positive fee-cap test обязан доказать, что order с `feeRate == approved cap` принят и исполнен.
4. Negative test должен отличаться только `feeRate > cap` и проверять наблюдённый конкретный exchange code/message. Слова `invalid` и широкий набор общих кодов не являются доказательством fee-cap semantics.
5. Очистку позиции выполнять в `finally` как гигиену повторного запуска. Остаточная Testnet-позиция не является финансовым blocker.

### Live matrix после исправлений

- Fresh Builder onboarding и paused commit.
- Remote accepted + local response lost + restart recovery без второго POST/signature.
- Preflight failure и повтор после исправления состояния.
- At-cap order accepted/executed.
- Above-cap identical order rejected конкретно по cap.
- Mini App HTTP pause/resume с fresh real snapshot и confirmation token.
- Expired/stale confirmation rejection.
- Manual Telegram + WalletConnect/TokenPocket run на текущем HEAD.

## 14. R9 — документация и UI evidence

### Исправить

- `docs/reports/STAGE3_PART1_REVIEW_GUIDE_2026-08.md`;
- `docs/reports/STAGE3_PART1_CORRECTIVE_PLAN_2026-08.md` — не переписывать историю, а добавить audit addendum со ссылкой на этот документ;
- `docs/reports/STAGE3_BUILDER_MINI_APP_PART1_2026-08.md`;
- `docs/ENGINE_TEST_STATUS_2026-08.md` как единственный источник актуальных counters.

### Фактические несоответствия evidence

- Фактический скриншот Home показывает `0,01%`, а текущий formatter для stored `0.01` показывает `1%`.
- Фактический paused state содержит emoji/icon variant, отличающийся от текущего inline SVG.
- Поэтому приложенные скриншоты подтверждают общую UX-концепцию и прежний manual flow, но не являются pixel/behavior evidence для текущего HEAD.

После исправлений получить новые скриншоты текущего deployment:

1. Wallet step;
2. signing/pending recovery;
3. connected paused success;
4. Home paused;
5. Resume confirmation sheet;
6. Home active;
7. error/retry state при временном Testnet timeout.

## 15. UI/UX оценка

Визуальное соответствие intended Part 1: **8/10**.

Сильные стороны:

- идентичность и цветовая система совпадают с reference direction;
- главный state считывается мгновенно;
- primary action визуально доминирует;
- таблица параметров, masked addresses и testnet badge понятны;
- мобильные размеры и типографическая иерархия в целом удачны.

Обязательная доработка: modal overlay. Желательная доработка: перепроверить safe-area/footer на реальном iPhone после нового build и получить current-HEAD screenshots. Полная навигация и позиции относятся к Part 2 и не должны раздувать этот corrective gate.

## 16. Что не добавлять

Для текущего масштаба не нужны:

- PostgreSQL/Redis/Kafka;
- distributed locks;
- event sourcing/Saga/workflow engine;
- Kubernetes/микросервисы;
- отдельный design-system package;
- Playwright-ферма для реального WalletConnect;
- сложная observability-платформа.

Достаточно текущего modular monolith, SQLite transaction/locks, typed states, настоящих Testnet calls и нескольких точных UI/integration tests.

## 17. Рекомендуемый порядок коммитов

1. `fix: preserve ambiguous builder approval state`
2. `fix: resume durable builder attempt without resigning`
3. `fix: finalize builder attempt lifecycle and safe logs`
4. `fix(miniapp): restore resume sheet and honest freshness`
5. `test: make builder recovery and resume genuinely live`
6. `test: harden signed reads and fee cap assertions`
7. `docs: correct stage 3 part 1 evidence and gate status`

## 18. Финальный Definition of Done

- Все R1–R9 выполнены или явно помечены как non-blocking с техническим основанием.
- Unknown remote POST никогда не переводится автоматически в `PREPARED`.
- `APPROVAL_SENT`/`APPROVED` восстанавливаются без второй подписи.
- Account остаётся paused до явного confirmed Resume.
- Initial Home использует честный fresh snapshot.
- Resume sheet снова является настоящим modal bottom sheet.
- Offline suite, coverage, ruff, frontend tests/lint/build зелёные.
- Полный `tests/testnet/` зелёный; transient read timeouts обрабатываются bounded retry.
- Live-тесты действительно делают Aster Testnet calls; fake tests так не называются.
- Manual Telegram/WalletConnect acceptance выполнен на текущем HEAD.
- `docs/ENGINE_TEST_STATUS_2026-08.md` содержит фактические counters и timestamp прогона.

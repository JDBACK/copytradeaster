# Готовый промпт исполнителю — Stage 3 Part 1 remediation

Ниже находится готовый текст для передачи следующей модели без сокращений.

---

Ты работаешь в репозитории `copytradeaster`, ветка `codex/stage-3-mini-app-part1`.

Твоя задача — полностью выполнить оставшийся corrective/remediation gate Stage 3 Part 1 для Builder-auth Telegram Mini App, проверить его offline и на реальном Aster Testnet, обновить документацию и оставить ветку в чистом, закоммиченном состоянии.

Работай самостоятельно, без субагентов и без делегирования.

## Обязательное чтение до любых изменений

Прочитай полностью, не выборочно и не только первые строки:

1. `AGENTS.md`.
2. `docs/ARCHITECTURE.md`.
3. Stage 3 в `docs/DEV_PLAN_SKELETON.md`.
4. `docs/reports/STAGE3_MINI_APP_UX_PLAN_2026-08.md`.
5. `docs/reports/STAGE3_BUILDER_MINI_APP_PART1_2026-08.md`.
6. `docs/reports/STAGE3_PART1_REVIEW_GUIDE_2026-08.md`.
7. `docs/reports/STAGE3_PART1_CORRECTIVE_PLAN_2026-08.md`.
8. `docs/reports/STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md` — это основной документ исполнения.
9. `docs/ENGINE_TEST_STATUS_2026-08.md`.

После чтения изучи историю и cumulative diff от базового коммита до текущего HEAD:

```bash
git log --oneline --reverse c2a9584f91bb7de5023cbe87bd86f3ad5ac9a762..HEAD
git diff --stat c2a9584f91bb7de5023cbe87bd86f3ad5ac9a762..HEAD
git diff c2a9584f91bb7de5023cbe87bd86f3ad5ac9a762..HEAD
```

Не доверяй словам `CLOSED`, старым counters и старым live-claims без проверки кода и фактического прогона.

## Контекст масштаба и запреты на лишнюю архитектуру

Это личный/малый trading project: в первое время 10–30 пользователей, максимум около 100. Не превращай corrective gate в enterprise-платформу.

Не добавляй PostgreSQL, Redis, Kafka, distributed locks, event sourcing, Saga/workflow engine, микросервисы, Kubernetes, тяжёлый UI-kit или отдельную observability-платформу. Сохрани modular monolith, один asyncio-процесс, SQLite WAL и существующие process-local/per-account locks.

При этом не упрощай security/correctness инварианты:

- main private key пользователя не должен попадать на backend;
- wallet signature не хранится;
- signer key хранится только зашифрованно;
- Telegram user id берётся только из валидного `initData`;
- новый account всегда коммитится paused;
- Resume только после fresh snapshot, READY и одноразового confirmation token;
- неизвестный результат удалённой мутации нельзя трактовать как гарантированный отказ.

## Testnet разрешение

Все state-changing проверки выполняются только против Aster Testnet. Testnet BNB/ASTER/USDT не являются реальными деньгами. Разрешено открывать и закрывать реальные Testnet orders/positions и выполнять Builder/Agent approvals.

Не называй возможную остаточную Testnet-позицию финансовым риском или blocker. Cleanup в `finally` нужен для повторяемости и независимости тестов, а не для защиты реальных средств. Mainnet остаётся строго запрещённым fail-closed guards.

## Обязательный порядок реализации

### 1. Исправь remote-outcome state machine

Файлы: `src/copytradeaster/aster/management_api.py`, `src/copytradeaster/onboarding/builder.py`, typed errors и соответствующие tests.

- Раздели definitive rejection и unknown remote outcome.
- Transport timeout/error, HTTP 5xx без доказанного business rejection и `-1000/-1006/-1007` должны оставлять attempt в `APPROVAL_SENT`.
- Удали `-1000` из трактовки «Agent точно отсутствует».
- На retry сначала делай signed `GET /agent`; не повторяй POST вслепую.
- После подтверждения продолжай `APPROVED -> preflight -> paused commit`.
- Не логируй raw exception/response text.

### 2. Реализуй Mini App recovery без второй подписи

Файлы: application DTO/service, aiohttp server, frontend types/api/routing/wizard/recovery UI.

- Добавь server-owned use case/endpoint для продолжения текущего attempt по authenticated Telegram user id.
- Signature-less recovery разрешена только для server-side state `APPROVAL_SENT` или `APPROVED`.
- Для `PREPARED` signature остаётся обязательной.
- Session должен сообщать минимальный typed recoverable state.
- Frontend после restart продолжает проверку/commit без нового WalletConnect и `signTypedData`.
- Retry не создаёт новый signer/pending attempt.

### 3. Доведи lifecycle и safe logging

- Реально используй `COMMITTED` либо исправь модель/документацию так, чтобы они не обещали несуществующий state. Предпочтительный right-sized вариант: записать `COMMITTED` после paused account commit, затем activation и lazy cleanup.
- Crash-window account + stale `APPROVED` должен восстанавливаться идемпотентно.
- Исправь docstring `builder.py` про in-memory attempts.
- В логах оставь только typed category, code, exception class и безопасные correlation fields.

### 4. Исправь Home freshness и Resume sheet

- Первый Home load выполняет `dashboard(refresh=true)`.
- Не показывай account `updated_at` как время snapshot.
- Без snapshot показывай «Снимок: не получен».
- Верни `className="overlay"` в `Sheet`.
- Добавь Escape, onClose и базовый focus management без тяжёлой зависимости.

### 5. Сделай live evidence настоящим

- L3 должен реально вызвать Testnet `approveAgent`, затем после remote success симулировать локально потерянный response и восстановиться signed GET без второго POST/signature.
- L7 должен использовать настоящий Aster signer/snapshot/permission path и пройти Mini App aiohttp API pause/confirmation/resume.
- Fake-only tests перенеси в `tests/integration/` и не называй signed/live.
- Manual Telegram + WalletConnect/TokenPocket acceptance документируй отдельно от automated pytest.

### 6. Укрепи signed live helpers и fee-cap assertions

- Bounded retry с новым nonce только для idempotent signed reads при transient timeout/unknown codes.
- Unknown state-changing order outcome разрешай через unique client order id и exchange query.
- At-cap order обязан быть принят и исполнен.
- Above-cap order должен отличаться только feeRate и отклоняться с конкретно зафиксированной cap-семантикой. Общие слова `invalid` и широкий список несвязанных error codes не принимаются.
- Cleanup Testnet positions/orders делай в `finally` для повторяемости.

### 7. Обнови документацию и evidence

- `docs/ENGINE_TEST_STATUS_2026-08.md` — единственный источник актуальных counters.
- В review guide и historical corrective plan добавь ссылки на новый audit/result, не переписывая историю задним числом.
- Убери утверждения, что L7/L8 являются signed live, если они всё ещё используют fake runtime.
- Получи новые ручные Telegram screenshots именно текущего build/HEAD: Wallet, recoverable signing state, Success paused, Home paused, Resume sheet, Home active, timeout/retry.

## Обязательные tests

Добавь/исправь tests для каждого найденного defect, минимум:

1. Unknown POST outcome сохраняет `APPROVAL_SENT`.
2. Restart recovery находит remote Agent и не делает второй POST.
3. Unknown agent read не переводит state в `PREPARED`.
4. Definitive rejection разрешает controlled retry.
5. Signature-less recovery запрещена для `PREPARED` и разрешена для `APPROVAL_SENT/APPROVED`.
6. HTTP session/recovery full flow после restart.
7. Frontend recoverable session не вызывает wallet signature.
8. Initial Home делает fresh request и честно рендерит отсутствие snapshot.
9. Sheet overlay/Escape/focus/explicit confirmation.
10. Live at-cap accept и above-cap specific reject.
11. Настоящий live Mini App pause/resume с fresh signed snapshot.

## Команды финального gate

```bash
.venv/bin/python -m pytest -q -m "not testnet" --cov=copytradeaster --cov-report=term-missing
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .

cd miniapp
CI=true pnpm test
CI=true pnpm lint
CI=true pnpm build
cd ..

CT_TESTNET=1 .venv/bin/python -m pytest tests/testnet/ -v
git diff --check
git status --short
```

Coverage не должна опуститься ниже 87%. Полный signed Testnet suite должен завершиться зелёным; не выдавай частичный прогон за полный.

## Коммиты

Делай небольшие логические коммиты в таком порядке:

1. `fix: preserve ambiguous builder approval state`
2. `fix: resume durable builder attempt without resigning`
3. `fix: finalize builder attempt lifecycle and safe logs`
4. `fix(miniapp): restore resume sheet and honest freshness`
5. `test: make builder recovery and resume genuinely live`
6. `test: harden signed reads and fee cap assertions`
7. `docs: correct stage 3 part 1 evidence and gate status`

Не переписывай чужие unrelated изменения и не используй destructive git commands.

## Что должно быть в финальном отчёте

- список изменённых файлов по каждому work package;
- какое нарушение исправлено и почему новая семантика корректна;
- точные offline/frontend/live результаты, включая duration и skips;
- какие live tests действительно сделали Aster network calls;
- current Testnet account/order/position snapshot после suite без драматизации остаточных тестовых позиций;
- ссылки на новые screenshots текущего HEAD;
- список коммитов;
- `git status`;
- честный список оставшихся рисков/Part 2 задач.

Не объявляй gate закрытым, пока не выполнены обязательные acceptance criteria из `STAGE3_PART1_FINAL_AUDIT_AND_REMEDIATION_2026-09.md`.

---

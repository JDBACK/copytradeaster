# Stage 2 safe logging audit journal — 2026-08-24

## Scope

Проверен persistent operational logging для диагностики важных действий без
попадания credential-adjacent данных в console/file/caplog. Scope ограничен
logging/config/onboarding audit; live процесс, `.env`, `.botkey`, рабочая DB и
торговое состояние не использовались.

## Changes

- `setup_logging()` поднимает console + rotating file с default
  `data/logs/copytradeaster.log`, rotation bounds, owner-only `0600` mode для
  active/rollover-файлов и idempotent managed handlers;
- `httpx`/`httpcore` выставлены на WARNING;
- `log_onboarding_audit` фиксирует `attempt`/`success`/`recovered`/`rejected`,
  stable mode/code/category и bounded/sanitized user/account identifiers;
  `attempt`/`rejected`
  пишут `paused=unknown`, `success`/`recovered` — `paused=true`; rejected —
  WARNING;
- unexpected exception пишет только safe category/type;
- logging calls не передают addresses, key/token/signature, signed URL/query или
  raw exchange/exception payload; handler filter — defense-in-depth;
- Settings добавляет rotation/log-level validation и безопасный redacted dump;
- credential import action оставляет только account/role, без wallet/signer.

## Verification

Проверены focused logging/application tests, полный offline suite с coverage,
`ruff check .`, `ruff format --check .` и `git diff --check`. Актуальные
счётчики suite и coverage ведутся только в
[`docs/ENGINE_TEST_STATUS_2026-08.md`](../ENGINE_TEST_STATUS_2026-08.md) — это
единственная точка истины для текущего gate.

Историческая причина live onboarding failure этим audit не восстанавливается:
фиксируется только безопасная текущая классификация.

## Append-only gate result

13:42 MSK: static gate завершён clean. Никакие live process, `.env`, `.botkey`,
рабочая DB или торговые данные не изменялись; generated test logs находились
только в `tmp_path`.

## Control audit corrective implementation — 2026-08-24

На application boundary добавлен отдельный bounded `control_audit` helper:

- фиксированные action/result (`attempt|success|rejected`), bounded/sanitized
  user/account identifiers, whitelist state и changed field names; address-like
  значения
  редактируются, значения risk policy и confirmation token не сериализуются;
- rejected пишется уровнем WARNING, остальные события — INFO; helper и service
  boundary defensive, поэтому сбой logging sink не меняет return/exception
  semantics;
- public `set_notifications`, `update_risk`, `pause`, `resume` и
  `confirm_action` (RESUME/EMERGENCY/DISCONNECT) дают attempt + terminal event;
  risk success содержит только names изменённых полей, notifications
  различает `enabled`/`disabled`;
- адресные tests покрывают bounds/redaction/levels, risk round-trip,
  notification states, safe rejected classification и logging-failure
  semantics.

Admin control methods (`admin_set_user_access`, `admin_decide_access`,
`admin_reconcile`, `admin_pause`, `admin_resume`, `admin_emergency`) в этой
узкой corrective итерации не менялись; их control-audit coverage остаётся
отдельным follow-up. WP9b и общий Stage 2 остаются открытыми до ручной owner
приёмки; этот change не является acceptance verdict.

## Admin control audit completion — 2026-08-24

Следующая implementation-итерация добавила bounded attempt/terminal audit для
`admin_allow_user`, `admin_set_user_access`, `admin_decide_access`,
`admin_reconcile` и подтверждённых global pause/resume/emergency boundaries.
Успешное decision-событие берёт target только из безопасного
`AccessRequestDTO`; APPROVED сообщает `changed_fields=status,access_enabled`,
REJECTED — только `changed_fields=status`, а duplicate idempotent decision
сохраняет state без `changed_fields`. Request ID и presentation details не
являются user-идентификаторами. Reconcile сообщает `state=account` при
переданном account ID и `state=all` без него, сам target ID/address не пишет.
`result` означает terminal outcome application call.
Post-commit presentation failure не откатывает durable state: актуальное
состояние при необходимости отдельно читается из DB. WP9b и общий Stage 2
остаются открытыми до ручной owner приёмки.

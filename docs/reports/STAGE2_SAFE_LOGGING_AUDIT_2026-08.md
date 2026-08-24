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
  stable mode/code/category и opaque user/account IDs; `attempt`/`rejected`
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

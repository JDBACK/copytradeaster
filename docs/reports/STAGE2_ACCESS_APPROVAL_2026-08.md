# Stage 2 access approval — 2026-08-24

## Scope

Этот append-only журнал фиксирует hardening первичного доступа Telegram после
ручной проверки inline UX. Trading core Stage 1 не менялся. Контур рассчитан на
один master и 10–30, максимум около 100 private Telegram users в одном asyncio
процессе.

## Фактический flow

```text
unknown private /start
  → короткая inline арифметическая captcha
  → durable PENDING request в SQLite
  → карточка только configured env-admin IDs
  → approve/reject (idempotent transaction)
  → попытка первого user menu с V3 connect CTA
```

- Middleware пропускает неизвестному identity только private `/start` и typed
  `GuestAccessCallback`; произвольные menu callbacks до application facts не
  доходят. Ошибка authorization backend остаётся fail-closed.
- Captcha process-local, single-use, TTL 5 минут, максимум 3 ошибки и cooldown;
  она нужна только до первой заявки, а не при каждом открытии меню.
- `telegram_access_requests` имеет уникальный pending request на user. SQLite
  transaction сообщает, кто выиграл вставку, поэтому гонка/повтор captcha не
  отправляет две admin-карточки.
- Admin decision дополнительно проверяется по immutable configured IDs из env;
  DB role сама по себе не даёт capability. Approval/reject меняет только
  `PENDING`, повторный click не откатывает принятое решение.
- Approval и создание/открытие `telegram_users.access_enabled` атомарны и
  сохраняют существующие account, role и notification settings. Bot API
  вызывается после commit.
- Ошибка direct delivery не удаляет заявку и не откатывает доступ. Заявка видна
  через inline резервный экран `Админка → Заявки`; manual add-by-ID остаётся
  recovery-only.

## Изменения hardening

- Добавлен transport DTO flag `AccessRequestDTO.created`, сравнение которого не
  влияет на equality; он обозначает только победившую insert-операцию.
- Добавлен repository seam `create_pending_result()` поверх совместимого
  `create_pending()` wrapper.
- Прямые Bot API delivery paths ловят обычные transport exceptions без вывода
  внутренних деталей в Telegram; лог содержит только тип исключения.
- Добавлены regression tests для direct admin delivery failure, duplicate
  admin decision, approval dashboard, guest status/retry и forged menu callback.
- Сцены aiogram не добавлялись: постоянное меню остаётся stateless, линейные
  credential/risk input flows используют обычные `StatesGroup`.

## Evidence

Команды выполнялись на рабочей ветке `codex/stage-2-telegram` без запуска
реального бота и без изменения `.env`/рабочей `data/copytrade.sqlite3`.
Актуальные счётчики suite и coverage после последующих исправлений ведутся
только в [ENGINE_TEST_STATUS_2026-08.md](../ENGINE_TEST_STATUS_2026-08.md):
это единственная точка истины для текущего gate.

- `ruff check .`: passed;
- `ruff format --check .`: passed; immutable `docs/history/**` и
  `docs/reviews/**` исключены из formatter configuration и не редактировались
  по правилам репозитория;
- `git diff --check`: passed.

## Остаточные риски

- Captcha state не переживает process restart; `/start` выдаёт новый challenge,
  durable request при этом сохраняется.
- Direct admin card — best-effort Bot API delivery; fallback list позволяет
  принять решение, но отдельная durable per-admin delivery queue для approval
  cards не вводилась ради масштаба Stage 2.
- Нужна ручная owner acceptance в private Telegram chat и отдельный flat
  testnet follower account для проверки signed V3 onboarding; production/mainnet
  не входит в этот verdict.

## Verdict

Автоматическая access-approval часть Stage 2 закрыта regression gate-ом. Общий
Stage 2 остаётся открытым до ручной owner acceptance flow в Telegram.

## Current-state note — 2026-08-24

После этой кампании owner вручную подтвердил approval, direct onboarding,
notifications, risk и итоговый inline UI. Текущий Stage 2 открыт уже не из-за
access flow, а только до одного live resume/copy cycle. Актуальный verdict:
[STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md](STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md).

# Stage 2 onboarding UX/typed-errors journal — 2026-08-24

## Scope

Owner acceptance показал, что direct onboarding принимал корректный набор
значений с экрана Aster, но при preflight failure Telegram сворачивал причину
в generic «подключение не выполнено». Внутренние названия полей `User`/`Signer`
также не соответствовали фактической странице API Wallet.

## Решение

- `UserApplicationService` передаёт через application boundary только
  `OnboardingApplicationError.code: OnboardingErrorCode`; raw exchange status,
  addresses и private key не передаются presenter-у.
- Telegram intro/prompt copy использует адрес обычного testnet-кошелька,
  `API wallet address` и `private key`, показанный под ним. До сбора данных
  отображаются prerequisites: финальная authorization подписью обычного
  wallet, чтение/perpetual trading включены, withdrawal выключен, пустой счёт,
  Hedge.
- Typed mapping даёт actionable remediation для key-pair, authorization,
  permissions, expiry/IP, signed read, flat/no-orders, Hedge, conflicts и
  commit/activation/unknown. Каждая ошибка имеет `🔄 Попробовать снова` и
  `⌂ На главную`.
- Secret-message удаляется до application call; state/key clearing остаётся в
  `finally`. Retry callback переносит `connect`/`replace` branch и использует
  актуальную menu revision, чтобы не стать stale.

## Verification

Параметризованные application/renderer/Telegram tests проверяют enum mapping,
отсутствие raw key/address/error, recovery buttons, deletion, typed replacement
preflight и отсутствие commit до preflight success. Полный offline gate,
coverage и static evidence записаны в живом
`docs/ENGINE_TEST_STATUS_2026-08.md`; live bot/PID не запускались.

Старый live failure намеренно не переименован в конкретную причину: generic
boundary ранее потерял classification. Наиболее вероятные классы для повторной
проверки — незавершённая authorization API wallet, mismatch пары API wallet
address/private key или permission/signed-read prerequisite.

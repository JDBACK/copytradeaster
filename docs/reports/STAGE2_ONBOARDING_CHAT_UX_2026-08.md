# Stage 2 onboarding chat UX — 2026-08-24

## Scope

Follow-up to owner feedback on direct Aster testnet onboarding. Scope is limited
to Telegram card lifetime, credential-message deletion order, expiry recovery,
and truthful prerequisite copy. This report contains no wallet addresses,
private keys, Telegram tokens, or exact suite counters; current counters live
only in `docs/ENGINE_TEST_STATUS_2026-08.md`.

## Implemented behavior

- The intro names the exact Aster path `Aster testnet → Settings → Position
  mode → Hedge Mode` and the required API wallet settings: `Perps` and read
  enabled, withdrawal disabled, ordinary wallet authorization completed, and
  no open positions or orders.
- The three data messages (ordinary wallet address, `API wallet address`, and
  `private key`) are deleted best-effort immediately after reading and before
  validation or the Aster call. The Telegram notification/screenshot/cache
  caveat remains in the intro. A deletion failure is logged safely and does not
  expose the value.
- Every valid input transition retires the previous onboarding card and sends a
  fresh step card. If Telegram cannot delete the old card, the handler edits it
  in place and does not send a duplicate. After a successful delete, the retired
  anchor is invalidated in `MenuSessionStore` and removed from FSM before the
  replacement send, so a send failure cannot retain a deleted message ID.
- Step 3 first shows a clean loading card, then a typed recovery card or the
  paused dashboard. Replacement and retry revisions preserve their branch and
  remain protected by the menu revision check.
- An expired credential input is already deleted, then the current anchor is
  replaced by a clean expiry/recovery card (or a safe fresh fallback). No
  separate `message.answer` is layered beside the stale prompt; FSM is cleared
  after the recovery view attempt.

## Safe live facts

The observed testnet acceptance sequence was:

```text
agent_perp_permission → invalid_input → position_mode_mismatch → success
```

The successful path confirmed an AES-GCM encrypted credential blob, read and
`Perps` permissions enabled, withdrawal disabled, and `allow_new_entries=false`
after onboarding. Dynamic follower reconciliation remained active. This report
does not claim that an emergency or global pause was removed.

## Evidence boundary

Renderer and runtime tests cover exact Aster labels, deletion of all three input
message IDs, fresh step revisions, delete-fallback behavior, expiry without an
extra duplicate message, stale-anchor cleanup, replacement/retry branches, and
absence of addresses/keys in outgoing text, FSM, and logs. The final offline and
static gate results are recorded in the single current status document named
above.

## Current-state note — 2026-08-24

После этого UX pass владелец подтвердил итоговое подключение и упрощённое меню.
Фраза выше о том, что данный узкий report не делает emergency-выводов, остаётся
исторической границей отчёта; текущий follower emergency уже удалён из public
UI/application boundary. Итоговый verdict и оставшийся один live copy cycle:
[STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md](STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md).

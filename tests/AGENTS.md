# Tests Guidelines

Map of `tests/`: what lives where, what each file covers, how to run it.
Suite counters live only in `docs/ENGINE_TEST_STATUS_2026-08.md` — never copy them here.

## Suites

- `unit/` — 58 files, hermetic: `tmp_path` SQLite + `respx` + in-memory fakes. Default run needs no network or keys.
- `integration/` — 7 files + `fake_aster.py`: real stack (Projector→Coordinator→Planner→Executor→App/Service) over faked exchange edges.
- `replay/` — 1 parametrized test × 6 JSON fixtures through `MasterProjector`.
- `testnet/` — 15 live files + `support/builder_live.py`: signed runs against `fapi.asterdex-testnet.com`, double-gated by `pytest.mark.testnet` + `CT_TESTNET=1`.

```bash
pytest -q -m "not testnet"                              # offline (unit + integration + replay)
pytest -q -m "not testnet" --cov=src/copytradeaster     # + coverage (>= 87% by eye, no fail_under)
CT_TESTNET=1 pytest tests/testnet/ -v                   # signed live (needs wallet.txt, leave flat behind)
CT_LOAD=1 pytest tests/integration/test_shadow_scale.py # scale preflight 30x100 + 100x20 (load marker unused)
cd miniapp && pnpm test                                 # frontend Vitest; counters live in docs/ENGINE_TEST_STATUS_2026-08.md
```

## unit/trading + risk + domain + master — trading core

| File | What it pins |
|---|---|
| `trading/test_planner_transitions.py` | Exhaustive `build_plan` table: open/reduce/stop-replace/leverage-gate/hedge/idempotency/Rule-9; exact `(kind, priority)`, payloads, coids, `depends_on_action_id` |
| `trading/test_executor_journal.py` | Journal/idempotency: UNKNOWN resolution (503/timeout/query), reduceOnly/−2011, P0 leverage gate (`BLOCKED`), `command_key` stability, SENDING/UNKNOWN boot resolver |
| `trading/test_coordinator_routing.py` | 4-follower fan-out: error isolation/degraded, pause barriers, generation-safe hot-add/replace, stale-master, self-stop/re-entry, no-stop/whitelist, leverage gating |
| `trading/test_leverage_manager.py` | `LeverageManager` with no HTTP: candidate catalog, singleflight, generation invalidation, `observe()` restore, bounded worker, read-before-write |
| `trading/test_builder_attribution.py` | P2.7 payload (`builder`/`feeRate`, 0.001 cap, Decimal canonicalization) + `BuilderAttributionProvider` routing (strict tri-state permissions, expiry) + `time_in_force` |
| `trading/test_builder_executor.py` | P2.7 in executor: attribution on the wire only (journal clean), entry fail-closed, close/Stop/TP fallback, snapshot/race isolation, no-secrets |
| `trading/test_builder_stale_corrective.py` | Only −5026 triggers invalidation + DIRECT retry of reductions; entries fail closed; restart durability; emergency fallback |
| `trading/test_follower_registry.py` | Copy-on-write snapshot/publish/replace/unpublish/`load_at_boot` + mutation idle-barrier + postfill-drain bound |
| `trading/test_reconciler_idempotent.py` | Steady state → 0 commands ×3; drift → 1 batch → 0 (reuses coordinator fakes) |
| `risk/test_engine_sizing.py` | Pure math: `floor_to_step`, `entry_quantity` (0.019 from 1000×1%), clamps/skips, `post_fill_cap`, `scaled_reduce`, `margin_check`, `deviation_exceeded` |
| `domain/test_models_invariants.py` | `QtyDecision` exactly-one-field, `PositionKey` hash/eq, Decimal-only (float forbidden) |
| `master/test_projector_payloads.py` | `MasterProjector`: ORDER_TRADE_UPDATE lifecycle, dedup vs out-of-order TRADE, informational-only AU, margin-call/calculated triggers, `rebuild_full`, hedge legs |

## unit/aster + credentials + storage — adapter, keys, DB

| File | What it pins |
|---|---|
| `aster/conftest.py` | Shared fakes for all adapter tests: `FakeIdentities`/`FakeKeys`/`FakeNonces`, limiter fixture |
| `aster/test_trading_api.py` | Order signing, param building, error mapping, dry-run, weights |
| `aster/test_trading_api_endpoints.py` | Endpoint coverage: leverage, margin, position mode, listenKey, exchangeInfo, order query |
| `aster/test_agent_api.py` | Signed `GET /fapi/v3/agent` contract → typed `AgentDTO` |
| `aster/test_builder_api.py` | Signed `GET /fapi/v3/builder` contract → typed `BuilderDTO` (P2.6) |
| `aster/test_management_api.py` | `approveAgent`/`approveBuilder` EIP-712 registration requests |
| `aster/test_signing.py` | Pure EIP-712 primitives: query string, trading/registration signatures, nonce |
| `aster/test_user_stream_protocol.py` | `UserStream` protocol: events, listenKey rotation, backoff, stop, keepalive |
| `aster/test_rate_limiter.py` | `SlidingWindowLimiter`/`RateLimits` timing and isolation |
| `aster/test_shadow.py` | `ShadowExchange` semantics: fills, protection, hedge (no creds/HTTP) |
| `credentials/test_service.py` | `SqliteCredentials`: import/load/cache/encryption roundtrips |
| `credentials/test_crypto_roundtrip.py` | AES-GCM: layout, auth, nonce uniqueness, key validation |
| `credentials/test_wp1.py` | Atomic onboarding, concurrency winners, rollback, disconnect/re-onboard |
| `credentials/test_builder_lifecycle.py` | Builder-proof durability across onboarding/replace/disconnect (P2.6) |
| `credentials/test_shadow_accounts.py` | SHADOW accounts need no credential rows |
| `storage/test_storage.py` | Repository roundtrips: accounts, policies, events, trades, commands, nonces, builder attempts |
| `storage/test_wp1.py` | Migrations, casefold owners, policies, access requests, outbox lease/retry |
| `storage/test_account_builder_identity.py` | Builder-snapshot primitives: set/clear/invalidation (P2.6) |
| `storage/test_episode_allocations.py` | Allocation index (C2): HEDGE/flip shares sum to source, rebuild from payloads |
| `storage/test_database_upgrade.py` | Fresh/ghost-v33/S1 upgrade convergence incl. C1–C3 tables |

## unit/history — projection, capture, events (S1 remediation C1–C5)

| File | What it pins |
|---|---|
| `history/test_capture.py` | Serial boundary capture: bracket proof, live-edge cutoff, partial (not refused), no-op republish |
| `history/test_replay.py` | Stable identity (R2/C1): rolling windows keep one ref, late-fill origin rollback, alias/split corrections, silent rebuild |
| `history/test_projector.py` | Pure quantity projection: seeds, flips, HEDGE legs, complete/partial quality |
| `history/test_event_builder.py` | One event per semantic allocation; batch == sequential publication (R3/C3) |
| `history/test_transition_events.py` | Publish-level transition events incl. legacy upgrade + silent correction (C3) |
| `history/test_episode_events.py` | Atomic episode+event publication, first-wins dedup |
| `history/test_episodes.py` | Episode repo: refs, revisions, no-op republish on covered ranges |
| `history/test_protection.py` | Protection fold: leg-keyed SET/CHANGED/UNCONFIRMED, provenance (R4/C4) |
| `history/test_protection_legs.py` | LONG/SHORT STOP independence, replace-then-cancel per leg, unknown-leg silence (C4) |
| `history/test_storage.py` | Fill/coverage/commit semantics, ImportDelta, protection baseline queries |
| `history/test_worker.py` | Split dirty sets, fills→capture chaining, stats, no-op fall-through |
| `history/test_accounting.py` + `test_accounting_c5.py` | Pro-rata costs, (symbol,trade_id) keys, proof-driven net with named gaps (R5/C5) |

## unit/application + onboarding — use cases and entry gates

| File | What it pins |
|---|---|
| `application/test_service.py` | Full `UserApplicationService` shell: auth, dashboard/status/risk/positions/activity, connect/replace, pause/resume/confirm/emergency, admin |
| `application/test_history_contracts.py` | S1.4 read contracts: trades/detail/events/feed DTOs, ownership, cursors |
| `application/test_history_acceptance.py` | S1.6 offline cross-surface: one ref, four surfaces, transition kinds (C3 semantics) |
| `application/test_risk_preview_resume.py` | Risk preview steps + resume context incl. disabled/whitelist advisory |
| `application/test_trade_views.py` | Shared presenters: neutral close, executed_qty None, money sections (C5) |
| `application/test_trade_events_c2.py` | Allocation-bound fills, split cursors, history_page_changed (R1/C2) |
| `application/test_trade_money_c5.py` | Proof-driven net/gaps over HTTP-equivalent service calls (R5/C5) |
| `application/test_system_events_c4.py` | ENTRY_SKIPPED dedup, connectivity transitions, writer-failure tolerance (R4/C4) |
| `application/test_status.py` | Read-only status: fresh/stale/blocked, snapshot lifecycle, blocked→degraded→stale→ready precedence |
| `application/test_account_builder.py` | `AccountBuilderDTO` verdicts (verified/expiring/stale/blocked/NA), proof lifecycle, trust rail, reauthorize/pause ordering |
| `application/test_adapters.py` | Stage2 runtime/store seams: snapshot cache, pause/reconcile admission, resume safety, recovery, replace atomicity, leverage-gated resume, emergency |
| `application/test_builder_reauth_store.py` | Same-signer reauth over real SQLite: gating, version marker, atomic commit |
| `application/test_control.py` | Transport-neutral emergency: per-account/global flatten with two flat-proofs, failure isolation, cancel-then-reduceOnly plan shape |
| `application/test_engine.py` | `Stage2Process` lifecycle: start/run/shutdown order, shared bot, admin bootstrap, recovery floor |
| `onboarding/test_direct.py` | Direct gates: fail-closed agent policy, flat/mode/signer/IP, testnet guards, stable IDs, commit→activate |
| `onboarding/test_builder.py` | Builder prepare/submit: EIP-712 payload, pending lifecycle, flat/expiry/IP, paused commit, secret-safe logs |
| `onboarding/test_builder_attestation.py` | Pure `validate_builder_record`: exact-match addresses, Decimal fee compare, fail-closed errors |
| `onboarding/test_builder_reauthorize.py` | Same-signer reauthorization: foreign/stale/expired/rotated signer rejected, stays paused |

## unit/telegram + miniapp + notifications + cli + root — transports

| File | What it pins |
|---|---|
| `telegram/test_runtime.py` | Dispatcher end-to-end: `/start`, callback freshness/staleness, single-effect dedup, secret-safe onboarding, captcha/access/admin, confirmations, lifecycle |
| `telegram/test_scale_gate.py` | Concurrency: FSM/confirmation isolation over 30 users, 100-burst with busy replies, post-restart freshness |
| `telegram/test_renderers.py` | Pure renderers: dashboard/status/admin/activity/notifications/help/onboarding copy, callback byte budget, 4096 bound, secret-safety |
| `telegram/test_access.py` | `CaptchaChallengeStore`: chat/user binding, single-use, TTL, wrong-attempt blocking, cooldown, callback budgets |
| `telegram/test_middleware.py` | `BoundedTaskTracker` admit/release/drain, fail-closed private access, busy/unknown no-ops |
| `miniapp/test_server.py` | Transport core: initData-auth/dev-mode, builder prepare/submit/recover/resume, dashboard/positions/pause/resume/confirmations, activity, notifications, static/security headers, client-log sanitize |
| `miniapp/test_status_server.py` | Status route: auth gate, strict boolean-only body, exact safe DTO shape, secret-safe error mapping |
| `miniapp/test_account_builder_server.py` | Account-builder + reauthorize: auth/forgery rejection, strict bodies, DTO denylist, prepare ignores client fields |
| `miniapp/test_risk_server.py` | Risk route: backend-owned read (no leverage/deviation), string-only `risk_percent`, forged/malformed rejection, `ValidationError`→400 |
| `miniapp/test_history_server.py` | History routes: cursor/limit validation, owned trade/events/feed shapes |
| `miniapp/test_trade_events_c2_server.py` | Split fills/events cursors over HTTP, revision-change signal (C2) |
| `miniapp/test_activity_feed_snapshot_c7.py` | Snapshot-bound feed: pin high_water, no interleave, banner, 400s (R9/C7) |
| `notifications/test_formatter.py` | Pure formatter: open/close/stop/TP/leverage/cancel copy + price/volume formatting, skip reasons |
| `notifications/test_notifications.py` | Router+worker on tmp SQLite: account/global/critical-admin routing, dedup, retry-after/network/403, truncation, pacing, lease recovery, burst bound, hourly maintenance tick (C8) |
| `notifications/test_user_event_delivery_c6.py` | Durable delivery cursor: crash replay, rollout floor, link gate (R6/R7/C6) |
| `cli/test_cli_commands.py` | Light CLI on tmp DB: init-db/status/pause, close-plan math, idempotent import, telegram-user add/list/block/unblock |
| `cli/test_run_bot.py` | `run-bot` wiring: parser + `_dispatch` via `build_stage2_process` |
| `cli/test_cli_heavy_commands.py` | Heavy CLI over `respx`: reconcile reports, double-verified emergency flat, approve-builder success vs missing-main-key skip |
| `cli/test_wallet_parser.py` | wallet.txt parser: typo tolerance (`evm adress`/`privte`), main-prefix roles, dupe-free upsert |
| `test_app_lifecycle.py` | App lifecycle/REAL-SHADOW routing: shadow boot gate, client selection, stop ordering, paused admission, unpublish, notifier isolation, stream fallback |
| `test_coverage_gaps.py` | Targeted coverage gaps: dispatch/run, logging utils, config validation, DB guards, stream robustness |
| `test_telegram_config.py` | `Settings`: `.botkey` compat (`BOT_KEY=`), redaction, admin IDs/egress IP normalization |

## integration — engine stacks over a faked exchange

| File | What it pins |
|---|---|
| `fake_aster.py` | In-memory Aster V3 (no HTTP): balances, fills, STOP_MARKET triggers, leverage, 503/timeout/−2019 injection; `btc_filters`/`FakeOrder` |
| `test_end_to_end_fake.py` | Copy E2E matrix (copy/reduce/stops/failures/protection/flip/dust); `order_trade`/`stop_new` builders reused by other files |
| `test_acceptance_gate.py` | Adversarial Stage 1 invariants over the real engine + SQLite |
| `test_boot_fail_closed.py` | Unresolved SENDING blocks READY and mutes order POSTs, 0 mutations |
| `test_miniapp_builder_flow.py` | Builder onboarding over the real HTTP stack (aiohttp TestServer + httpx MockTransport) down to encrypted paused-commit |
| `test_stage2_adapters.py` | Real App composition + SQL-side activity pagination regression (category in SQL, not memory) |
| `test_resume_snapshot_tuple_contract.py` | P0 tuple regression `(AccountState, captured_at)`: resume/dashboard/positions unpack before validate/render; TTL-stale |
| `test_shadow_scale.py` | Non-certifying scale preflight (30×100 + 100×20): 0 signed requests, `FAILED=UNKNOWN=0`, <60s. `CT_LOAD=1` only |

## replay — deterministic WS projector

`test_projector_replay_fixtures.py` replays every `fixtures/*.json` through `MasterProjector.apply_raw` and diffs the snapshot against embedded `expected`.

| Fixture | Event → expectation |
|---|---|
| `order_trade_new.json` | NEW — no position, no episode |
| `order_trade_fill_open.json` | TRADE fill — 0.002 BTC position + 1 open trade |
| `order_trade_fill_close.json` | TRADE open→close — flat, episode closed |
| `account_update.json` | ACCOUNT_UPDATE — USDT balance applied, positions informational |
| `account_config_update.json` | ACCOUNT_CONFIG_UPDATE — leverage 10 on a stream-opened position |
| `margin_call.json` | MARGIN_CALL — position intact, episode alive |

## testnet — signed live (testnet only)

Gate: `pytestmark = [pytest.mark.testnet, skipif(CT_TESTNET != "1")]` in every file.
Protocol: `wallet.txt` at repo root (git-ignored, never commit) with section blocks `main`/`copyc1..3`/`copyc2`/`builder`/`builder-gate`; `ensure_testnet_guard` (host + chains 714/56), pre/post `flatten` (cancel + MARKET-close to flat in `finally`), scratch DBs via `sqlite3.backup` (never touch prod).
Format per block (parser: `cli/main.py:parse_wallet_file`, typos are literal): label line, then `evm adress: 0x…` (user), `api wallet adress: 0x…` (signer), `privte key: 0x…` (66-hex signer key), optional `main privte key: 0x…`. Fund accounts from the testnet faucet (`https://www.asterdex-testnet.com/en/faucet`: 1000 test USDT + 1000 test ASTER once a day); example shape in `tests/unit/cli/test_wallet_parser.py:WALLET_FIXTURE`.

| File | Scenario |
|---|---|
| `test_signed_scenarios.py` | Checklist: clock drift, reads, listenKey, master cycle, margin error + REAL-01/04/05/06 and idempotency |
| `test_one_way_engine_e2e.py` | Full ONE_WAY engine cycle (open/stop-move/partial/TP/flat/dust/reversal) + HEDGE-resize proof |
| `test_leverage_control_plane_live.py` | Leverage live: −4028 fallback, fail-closed unready, P0 executor gate, durable downshift |
| `test_builder_onboarding_recovery_live.py` | Builder lifecycle L1–L4: reads, happy path, lost response, commit/activation recovery |
| `test_builder_mini_app_e2e.py` | Part 1 e2e: onboarding commit, feeRate round-trip trade, over-cap rejection |
| `test_builder_resume_live.py` | Pause/resume over MiniApp HTTP (L7–L8): live snapshot + HMAC initData |
| `test_builder_stale_corrective_live.py` | Stale builder fee → direct fallback on risk-reducing close |
| `test_builder_engine_attribution_live.py` | P2.7 proof: Builder vs direct routing through the production core with a `place_order` spy |
| `test_live_status_acceptance.py` | P2.5 status: signed snapshot+agent reads, safe DTO + HTTP contract |
| `test_live_positions_acceptance.py` | P2.1 positions: copy, stop-move, TP, partial, MISSING_STOP, flat audit |
| `test_live_risk_acceptance.py` | P2.4 risk: 1% vs 3% legs, ratio check, forged-body 400, DB invariance |
| `test_live_activity_acceptance.py` | P2.2 activity: journal DTO, trade/system filters, no-leak, `SET_LEVERAGE` via executor |
| `test_live_notifications_acceptance.py` | P2.3 notifications: toggle gate + real SENT delivery via Bot API on a DB copy |
| `test_live_account_builder_acceptance.py` | P2.6 attestation (copyc1/copyc2) + disconnect + fresh onboarding |
| `test_live_builder_reauthorize.py` | Same-signer reauth: 5d EXPIRING → re-auth → 30d VERIFIED, no orders |
| `test_history_acceptance_live.py` | §10.9 history acceptance (copyc2/BTCUSDT): executor/journal lifecycle, distant STOP place+cancel, post-send-timeout query-before-retry, REAL file restart, authenticated HTTP on all surfaces. Requires an EXCLUSIVE window: skips unless flat, no open orders, and no fills in the last 5 minutes |
| `support/builder_live.py` | Shared helpers: wallet.txt parsing, testnet guard, signed reads, state snapshot, flatten |

## Conventions

- **Names**: `test_<module>_<behavior>.py` (`test_live_*_acceptance.py` / `test_builder_*_live.py` allowed in testnet).
- **Isolation**: `tmp_path` SQLite + `await db.migrate()` per test; `respx` for HTTP (only `aster/`, heavy CLI, boot); synthetic `0x..` keys; zero network by default.
- **Asyncio**: `asyncio_mode=auto` (`pyproject.toml`); stray `@pytest.mark.asyncio` decorators are redundant but harmless.
- **Markers**: `testnet` (live, double-gated), `load` (registered, unused — the real gate is `CT_LOAD=1` + explicit path).
- **Secret-safety**: no secrets/signatures in asserts; the `privte`/`adress` wallet typos are literals — do not "fix".
- **Living docs**: after suite changes update counters only in `docs/ENGINE_TEST_STATUS_2026-08.md`, never duplicate them.

"""SQLite (aiosqlite, WAL) access: connection + ordered migrations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SQLITE_BUSY_TIMEOUT_MS = 30_000


class MigrationConflictError(RuntimeError):
    """Fail closed with an actionable diagnostic for unsafe legacy data."""


MIGRATIONS: list[str | tuple[str, ...]] = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations(
      version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts(
      id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('master','follower')),
      label TEXT NOT NULL UNIQUE, user_address TEXT NOT NULL UNIQUE,
      enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')))
    """,
    """
    CREATE TABLE IF NOT EXISTS credentials(
      account_id TEXT PRIMARY KEY REFERENCES accounts(id),
      encrypted BLOB NOT NULL, algo TEXT NOT NULL DEFAULT 'AESGCM-v1', updated_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_settings(
      account_id TEXT PRIMARY KEY REFERENCES accounts(id),
      risk_percent TEXT NOT NULL, max_leverage INTEGER NOT NULL,
      max_entry_deviation_pct TEXT NOT NULL, risk_base TEXT NOT NULL DEFAULT 'available_balance',
      enabled INTEGER NOT NULL DEFAULT 1, allow_new_entries INTEGER NOT NULL DEFAULT 1)
    """,
    """
    CREATE TABLE IF NOT EXISTS master_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, event_time INTEGER NOT NULL,
      received_at TEXT NOT NULL, raw TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE,
      processed_at TEXT)
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_trades(
      trade_id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, pos_side TEXT NOT NULL,
      opened_at TEXT NOT NULL, closed_at TEXT, master_event_id INTEGER,
      status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED','SKIPPED')))
    """,
    """
    CREATE TABLE IF NOT EXISTS order_links(
      id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER REFERENCES copy_trades(trade_id),
      account_id TEXT NOT NULL, kind TEXT NOT NULL,
      master_client_order_id TEXT, follower_client_order_id TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(account_id, follower_client_order_id))
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_commands(
      command_key TEXT PRIMARY KEY, account_id TEXT NOT NULL, trade_id INTEGER,
      kind TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 1,
      status TEXT NOT NULL
        CHECK(status IN ('PENDING','SENDING','SUBMITTED','CONFIRMED','UNKNOWN','FAILED')),
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, client_order_id TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL)
    """,
    "CREATE INDEX IF NOT EXISTS idx_commands_account_status"
    " ON execution_commands(account_id, status)",
    """
    CREATE TABLE IF NOT EXISTS nonce_store(
      signer_address TEXT PRIMARY KEY, last_nonce INTEGER NOT NULL, updated_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_basis(
      account_id TEXT NOT NULL, symbol TEXT NOT NULL, pos_side TEXT NOT NULL,
      master_ref_abs TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
      issued_target TEXT, updated_at TEXT NOT NULL,
      PRIMARY KEY(account_id, symbol, pos_side))
    """,
    """
    ALTER TABLE accounts ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'REAL'
      CHECK(execution_mode IN ('REAL','SHADOW'))
    """,
    """
    ALTER TABLE sync_basis ADD COLUMN zero_target INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE copy_trades ADD COLUMN direction INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE execution_commands ADD COLUMN operation_id TEXT
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_users(
      telegram_user_id INTEGER PRIMARY KEY,
      chat_id INTEGER NOT NULL UNIQUE,
      account_id TEXT UNIQUE REFERENCES accounts(id),
      role TEXT NOT NULL CHECK(role IN ('USER','ADMIN')),
      notifications_enabled INTEGER NOT NULL DEFAULT 1,
      blocked_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_validations(
      account_id TEXT PRIMARY KEY REFERENCES accounts(id),
      signer_address TEXT NOT NULL,
      can_read INTEGER NOT NULL,
      can_perp_trade INTEGER NOT NULL,
      can_withdraw INTEGER NOT NULL,
      expired_ms INTEGER NOT NULL,
      ip_whitelist TEXT NOT NULL,
      status TEXT NOT NULL,
      checked_at TEXT NOT NULL,
      last_error_code TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_outbox(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_key TEXT NOT NULL,
      telegram_user_id INTEGER NOT NULL REFERENCES telegram_users(telegram_user_id),
      chat_id INTEGER NOT NULL,
      account_id TEXT REFERENCES accounts(id),
      kind TEXT NOT NULL,
      severity TEXT NOT NULL CHECK(severity IN ('INFO','WARN','CRITICAL')),
      text TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','SENT','FAILED')),
      attempts INTEGER NOT NULL DEFAULT 0,
      available_at TEXT NOT NULL,
      lease_until TEXT,
      claimed_by TEXT,
      last_error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      sent_at TEXT,
      UNIQUE(event_key, telegram_user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_available"
    " ON notification_outbox(status, available_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_user_address_lower"
    " ON accounts(lower(user_address))",
    "ALTER TABLE telegram_users ADD COLUMN access_enabled INTEGER NOT NULL DEFAULT 1",
    """
    CREATE TABLE IF NOT EXISTS telegram_access_requests(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      telegram_user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      username TEXT,
      display_name TEXT,
      status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED')),
      requested_at TEXT NOT NULL,
      decided_at TEXT,
      decided_by INTEGER
    )
    """,
    "CREATE UNIQUE INDEX idx_telegram_access_requests_pending_user"
    " ON telegram_access_requests(telegram_user_id) WHERE status='PENDING'",
    "CREATE INDEX IF NOT EXISTS idx_telegram_access_requests_status_id"
    " ON telegram_access_requests(status, id)",
    """
    CREATE TABLE IF NOT EXISTS builder_onboarding_attempts(
      pending_id TEXT PRIMARY KEY,
      telegram_user_id INTEGER NOT NULL UNIQUE,
      account_id TEXT NOT NULL,
      user_address TEXT NOT NULL,
      signer_address TEXT NOT NULL,
      encrypted_payload BLOB NOT NULL,
      algo TEXT NOT NULL DEFAULT 'AESGCM-v1',
      state TEXT NOT NULL CHECK(state IN ('PREPARED','APPROVAL_SENT','APPROVED','COMMITTED')),
      approval_deadline_ms INTEGER NOT NULL,
      agent_expires_at_ms INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_error_code TEXT
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_builder_attempts_user "
        "ON builder_onboarding_attempts(telegram_user_id)"
    ),
    # P2.6 Account + Builder attestation: durable identity source plus a
    # nullable last-known Builder snapshot on the account row.  The snapshot
    # is evidence, never proof: only a fresh signed Agent + Builder check
    # counts as verified.  Existing rows backfill to direct without guessing
    # any legacy Builder status.
    "ALTER TABLE accounts ADD COLUMN identity_source TEXT NOT NULL DEFAULT 'direct'"
    " CHECK(identity_source IN ('direct','builder'))",
    "ALTER TABLE accounts ADD COLUMN builder_address TEXT",
    "ALTER TABLE accounts ADD COLUMN builder_name TEXT",
    "ALTER TABLE accounts ADD COLUMN builder_max_fee_rate TEXT",
    "ALTER TABLE accounts ADD COLUMN builder_agent_expires_at_ms INTEGER",
    "ALTER TABLE accounts ADD COLUMN builder_verified_at TEXT",
    # Follower history is an independent read model. Exact amounts remain TEXT.
    """
    CREATE TABLE IF NOT EXISTS follower_fills(
      account_id TEXT NOT NULL REFERENCES accounts(id), environment TEXT NOT NULL,
      symbol TEXT NOT NULL, exchange_trade_id TEXT NOT NULL, order_id TEXT NOT NULL,
      side TEXT NOT NULL, position_side TEXT NOT NULL, qty TEXT NOT NULL,
      price TEXT NOT NULL, quote_qty TEXT NOT NULL, realized_pnl TEXT NOT NULL,
      commission TEXT NOT NULL, commission_asset TEXT NOT NULL, executed_at INTEGER NOT NULL,
      PRIMARY KEY(account_id, environment, symbol, exchange_trade_id))
    """,
    # S1.6 repair: 36c55d3 inserted the history schema mid-list, so DBs
    # migrated before S1 recorded a ghost version 33 and skip the real
    # CREATE TABLE follower_fills at index 33. This entry re-creates it
    # idempotently before its first index; the displaced time index moves
    # to entry 51. Fresh DBs see a no-op here.
    """
    CREATE TABLE IF NOT EXISTS follower_fills(
      account_id TEXT NOT NULL REFERENCES accounts(id), environment TEXT NOT NULL,
      symbol TEXT NOT NULL, exchange_trade_id TEXT NOT NULL, order_id TEXT NOT NULL,
      side TEXT NOT NULL, position_side TEXT NOT NULL, qty TEXT NOT NULL,
      price TEXT NOT NULL, quote_qty TEXT NOT NULL, realized_pnl TEXT NOT NULL,
      commission TEXT NOT NULL, commission_asset TEXT NOT NULL, executed_at INTEGER NOT NULL,
      PRIMARY KEY(account_id, environment, symbol, exchange_trade_id))
    """,
    "CREATE INDEX IF NOT EXISTS idx_follower_fills_order ON follower_fills"
    "(account_id, environment, symbol, order_id)",
    """
    CREATE TABLE IF NOT EXISTS follower_cash_flows(
      account_id TEXT NOT NULL REFERENCES accounts(id), environment TEXT NOT NULL,
      income_type TEXT NOT NULL, tran_id TEXT NOT NULL, symbol TEXT NOT NULL,
      asset TEXT NOT NULL, amount TEXT NOT NULL, occurred_at INTEGER NOT NULL,
      exchange_trade_id TEXT NOT NULL,
      PRIMARY KEY(account_id, environment, income_type, tran_id))
    """,
    "CREATE INDEX IF NOT EXISTS idx_follower_cash_time ON follower_cash_flows"
    "(account_id, environment, occurred_at)",
    """
    CREATE TABLE IF NOT EXISTS history_sync_state(
      account_id TEXT NOT NULL REFERENCES accounts(id), environment TEXT NOT NULL,
      stream TEXT NOT NULL CHECK(stream IN ('fills','income')), symbol TEXT NOT NULL,
      start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
      state TEXT NOT NULL CHECK(state IN ('pending','complete','saturated')),
      checked_at INTEGER,
      PRIMARY KEY(account_id, environment, stream, symbol, start_ms, end_ms))
    """,
    """
    CREATE TABLE IF NOT EXISTS follower_order_observations(
      account_id TEXT NOT NULL REFERENCES accounts(id), environment TEXT NOT NULL,
      symbol TEXT NOT NULL, order_id TEXT NOT NULL, client_order_id TEXT NOT NULL,
      command_key TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
      executed_qty TEXT NOT NULL, avg_price TEXT NOT NULL, observed_at INTEGER NOT NULL,
      PRIMARY KEY(account_id, environment, symbol, order_id))
    """,
    """
    CREATE TABLE IF NOT EXISTS history_order_checks(
      account_id TEXT NOT NULL REFERENCES accounts(id), environment TEXT NOT NULL,
      command_key TEXT NOT NULL, checked_at INTEGER NOT NULL,
      outcome TEXT NOT NULL CHECK(outcome IN ('observed','not_found')),
      PRIMARY KEY(account_id,environment,command_key))
    """,
    """
    CREATE TABLE IF NOT EXISTS follower_episode_refs(
      public_ref TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL, symbol TEXT NOT NULL, position_side TEXT NOT NULL,
      anchor TEXT NOT NULL,
      UNIQUE(account_id,environment,symbol,position_side,anchor))
    """,
    """
    CREATE TABLE IF NOT EXISTS follower_episodes(
      public_ref TEXT PRIMARY KEY REFERENCES follower_episode_refs(public_ref),
      account_id TEXT NOT NULL REFERENCES accounts(id), environment TEXT NOT NULL,
      symbol TEXT NOT NULL, position_side TEXT NOT NULL, opened_at INTEGER, closed_at INTEGER,
      revision INTEGER NOT NULL, payload TEXT NOT NULL)
    """,
    "CREATE INDEX IF NOT EXISTS idx_episode_owned_time ON follower_episodes"
    "(account_id,environment,closed_at,opened_at,public_ref)",
    """
    CREATE TABLE IF NOT EXISTS follower_episode_aliases(
      alias_ref TEXT PRIMARY KEY REFERENCES follower_episode_refs(public_ref),
      target_ref TEXT NOT NULL REFERENCES follower_episode_refs(public_ref),
      CHECK(alias_ref != target_ref))
    """,
    # S1.3 user events (§10.3): immutable account-owned facts for the
    # activity feed. Events are never updated; replays resolve to the same
    # public ref through the per-account dedup key. Feed cursors use the
    # internal monotonic seq so late backfill never shifts open pages.
    """
    CREATE TABLE IF NOT EXISTS user_activity_events(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      event_ref TEXT NOT NULL UNIQUE,
      account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL,
      trade_ref TEXT,
      kind TEXT NOT NULL CHECK(kind IN ('POSITION_OPENED','POSITION_INCREASED',
        'POSITION_REDUCED','POSITION_CLOSED','PROTECTION_SET','PROTECTION_CHANGED',
        'PROTECTION_UNCONFIRMED','RISK_CHANGED','COPY_PAUSED','COPY_RESUMED',
        'CONNECTIVITY_LOST','CONNECTIVITY_RESTORED','ENTRY_SKIPPED')),
      occurred_at INTEGER NOT NULL CHECK(occurred_at >= 0),
      recorded_at INTEGER NOT NULL CHECK(recorded_at >= 0),
      facts TEXT NOT NULL,
      dedup_key TEXT NOT NULL,
      notify_eligible INTEGER NOT NULL CHECK(notify_eligible IN (0,1)),
      UNIQUE(account_id, environment, dedup_key))
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_events_feed ON user_activity_events"
    "(account_id, environment, seq)",
    "CREATE INDEX IF NOT EXISTS idx_user_events_trade ON user_activity_events"
    "(account_id, environment, trade_ref)",
    # S1.3 capture activation: durable per-symbol publication frontier.
    # cap_start_ms pins the window start so placeholder anchors stay stable
    # across retries; cap_end_ms is the newest fully published window end.
    # NULL end means nothing published yet.
    """
    CREATE TABLE IF NOT EXISTS history_capture_state(
      account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL,
      symbol TEXT NOT NULL,
      cap_start_ms INTEGER CHECK(cap_start_ms IS NULL OR cap_start_ms >= 0),
      cap_end_ms INTEGER CHECK(cap_end_ms IS NULL OR cap_end_ms >= 0),
      last_error TEXT,
      updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
      PRIMARY KEY(account_id, environment, symbol))
    """,
    # S1.5 structured delivery: proven follower episode ref and a typed
    # action for the Telegram keyboard payload. Both stay NULL for legacy
    # engine rows; the button is assembled on the transport boundary only
    # when both are present. No backfill: old rows keep no invented link.
    "ALTER TABLE notification_outbox ADD COLUMN trade_ref TEXT",
    "ALTER TABLE notification_outbox ADD COLUMN action TEXT",
    # S1.6 repair companion of entry 34 (see comment there).
    "CREATE INDEX IF NOT EXISTS idx_follower_fills_time ON follower_fills"
    "(account_id, environment, symbol, executed_at)",
    # C1 stable projection: facts revision per import scope, durable replay
    # boundaries (origin + latest proven frontier with seed JSON), and typed
    # episode identity resolutions. Boundaries are saved only after a
    # successful reconciliation; resolutions share the publication commit.
    """
    CREATE TABLE IF NOT EXISTS history_scope_state(
      account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL,
      symbol TEXT NOT NULL,
      facts_revision INTEGER NOT NULL CHECK(facts_revision >= 0),
      updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
      PRIMARY KEY(account_id, environment, symbol))
    """,
    """
    CREATE TABLE IF NOT EXISTS history_projection_boundaries(
      account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL,
      symbol TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('origin','latest')),
      cutoff_ms INTEGER NOT NULL CHECK(cutoff_ms >= 0),
      mode TEXT NOT NULL CHECK(mode IN ('settled','partial')),
      seeds_json TEXT NOT NULL,
      coverage_revision INTEGER NOT NULL CHECK(coverage_revision >= 0),
      version INTEGER NOT NULL CHECK(version >= 1),
      updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
      PRIMARY KEY(account_id, environment, symbol, kind))
    """,
    """
    CREATE TABLE IF NOT EXISTS follower_episode_resolutions(
      account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL,
      symbol TEXT NOT NULL,
      ref TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('canonical','alias','split','merge')),
      targets_json TEXT NOT NULL,
      reason TEXT NOT NULL,
      revision INTEGER NOT NULL CHECK(revision >= 0),
      updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
      PRIMARY KEY(account_id, environment, ref))
    """,
    # C2 fill ownership (plan §23.3): materialized index over the allocations
    # already published inside episode payloads, not a second facts source.
    # Rows are replaced only together with their episode revision inside the
    # same publish transaction; rebuilds refill from stored payloads. Unique
    # over scope, episode ref, fill and role; money stays TEXT. One statement
    # per migration entry: the leading PRIMARY KEY columns already index the
    # (account, environment, trade_ref) read prefix.
    """
    CREATE TABLE IF NOT EXISTS follower_episode_allocations(
      account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL,
      symbol TEXT NOT NULL,
      position_side TEXT NOT NULL,
      trade_ref TEXT NOT NULL REFERENCES follower_episode_refs(public_ref),
      exchange_trade_id TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('entry','exit','unknown')),
      allocated_qty TEXT NOT NULL,
      allocated_realized_gross TEXT NOT NULL,
      occurred_at INTEGER NOT NULL CHECK(occurred_at >= 0),
      PRIMARY KEY(account_id, environment, trade_ref, exchange_trade_id, role))
    """,
    # C3 deterministic transition events (plan §23.4): widen the event kind
    # CHECK with HISTORY_CORRECTED, the silent link for superseded pre-C3
    # evt:* lineage. SQLite cannot ALTER a CHECK, so this entry rebuilds the
    # table inside the single migration transaction (plan §23 C8): stage to
    # a fresh table, copy every row verbatim, prove no row was lost or
    # gained with a parity guard that fails the migration on mismatch,
    # then swap and recreate the same indexes. Crash-resume safe: an
    # uncommitted attempt rolls back (the version is recorded only on
    # commit) and the staging table is dropped first on retry.
    (
        "DROP TABLE IF EXISTS user_activity_events_new",
        """
        CREATE TABLE user_activity_events_new(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_ref TEXT NOT NULL UNIQUE,
          account_id TEXT NOT NULL REFERENCES accounts(id),
          environment TEXT NOT NULL,
          trade_ref TEXT,
          kind TEXT NOT NULL CHECK(kind IN ('POSITION_OPENED','POSITION_INCREASED',
            'POSITION_REDUCED','POSITION_CLOSED','HISTORY_CORRECTED','PROTECTION_SET',
            'PROTECTION_CHANGED','PROTECTION_UNCONFIRMED','RISK_CHANGED','COPY_PAUSED',
            'COPY_RESUMED','CONNECTIVITY_LOST','CONNECTIVITY_RESTORED','ENTRY_SKIPPED')),
          occurred_at INTEGER NOT NULL CHECK(occurred_at >= 0),
          recorded_at INTEGER NOT NULL CHECK(recorded_at >= 0),
          facts TEXT NOT NULL,
          dedup_key TEXT NOT NULL,
          notify_eligible INTEGER NOT NULL CHECK(notify_eligible IN (0,1)),
          UNIQUE(account_id, environment, dedup_key))
        """,
        "INSERT INTO user_activity_events_new(seq, event_ref, account_id, environment,"
        " trade_ref, kind, occurred_at, recorded_at, facts, dedup_key, notify_eligible)"
        " SELECT seq, event_ref, account_id, environment,"
        " trade_ref, kind, occurred_at, recorded_at, facts, dedup_key, notify_eligible"
        " FROM user_activity_events",
        "CREATE TEMP TABLE _c3_event_parity(ok INTEGER CHECK(ok))",
        "INSERT INTO _c3_event_parity(ok) SELECT (SELECT COUNT(*) FROM user_activity_events_new)"
        " = (SELECT COUNT(*) FROM user_activity_events)",
        "DROP TABLE user_activity_events",
        "ALTER TABLE user_activity_events_new RENAME TO user_activity_events",
        "CREATE INDEX idx_user_events_feed ON user_activity_events(account_id, environment, seq)",
        "CREATE INDEX idx_user_events_trade ON user_activity_events"
        "(account_id, environment, trade_ref)",
        "DROP TABLE _c3_event_parity",
    ),
    # C6 durable delivery cursor (plan §23.7): per-scope scan high-water for
    # the user-event outbox sweep. Rollout-floor eligibility lives separately
    # in app_state; this table only records committed scan progress.
    """
    CREATE TABLE IF NOT EXISTS user_event_delivery_state(
      account_id TEXT NOT NULL REFERENCES accounts(id),
      environment TEXT NOT NULL,
      scanned_seq INTEGER NOT NULL CHECK(scanned_seq >= 0),
      updated_at INTEGER NOT NULL CHECK(updated_at >= 0),
      PRIMARY KEY(account_id, environment))
    """,
    # Corrective G1 / N3: durable projection dirt.  A fills/coverage commit
    # advances facts_revision and moves dirty_from_ms backwards to the
    # earliest unconsumed change.  Existing history scopes are conservatively
    # marked from their earliest retained fill/complete coverage so an
    # upgraded process cannot treat pre-migration facts as already consumed.
    (
        "ALTER TABLE history_scope_state ADD COLUMN dirty_from_ms INTEGER "
        "CHECK(dirty_from_ms IS NULL OR dirty_from_ms >= 0)",
        "INSERT OR IGNORE INTO history_scope_state"
        "(account_id,environment,symbol,facts_revision,updated_at,dirty_from_ms) "
        "SELECT q.account_id,q.environment,q.symbol,1,0,("
        " SELECT MIN(ts) FROM ("
        "   SELECT MIN(f.executed_at) AS ts FROM follower_fills f "
        "   WHERE f.account_id=q.account_id AND f.environment=q.environment "
        "   AND f.symbol=q.symbol "
        "   UNION ALL "
        "   SELECT MIN(s.start_ms) AS ts FROM history_sync_state s "
        "   WHERE s.account_id=q.account_id AND s.environment=q.environment "
        "   AND s.symbol=q.symbol AND s.stream='fills' AND s.state='complete'"
        " )) FROM ("
        " SELECT account_id,environment,symbol FROM follower_fills "
        " UNION SELECT account_id,environment,symbol FROM history_sync_state "
        " WHERE stream='fills' AND state='complete'"
        ") q",
        "UPDATE history_scope_state SET dirty_from_ms=("
        " SELECT MIN(ts) FROM ("
        "   SELECT MIN(f.executed_at) AS ts FROM follower_fills f "
        "   WHERE f.account_id=history_scope_state.account_id "
        "   AND f.environment=history_scope_state.environment "
        "   AND f.symbol=history_scope_state.symbol "
        "   UNION ALL "
        "   SELECT MIN(s.start_ms) AS ts FROM history_sync_state s "
        "   WHERE s.account_id=history_scope_state.account_id "
        "   AND s.environment=history_scope_state.environment "
        "   AND s.symbol=history_scope_state.symbol "
        "   AND s.stream='fills' AND s.state='complete'"
        " )) WHERE EXISTS (SELECT 1 FROM follower_fills f "
        " WHERE f.account_id=history_scope_state.account_id "
        " AND f.environment=history_scope_state.environment "
        " AND f.symbol=history_scope_state.symbol) OR EXISTS ("
        " SELECT 1 FROM history_sync_state s "
        " WHERE s.account_id=history_scope_state.account_id "
        " AND s.environment=history_scope_state.environment "
        " AND s.symbol=history_scope_state.symbol "
        " AND s.stream='fills' AND s.state='complete')",
    ),
    # PM-B1 integration: keep the already-published history migration numbers
    # 33..58 stable, then add durable pre-mutation evidence for MARKET recovery.
    # Databases that ran the hardening branch already have this column recorded
    # under version 33, so migrate() treats the DDL as an idempotent compatibility
    # step while still recording version 59.
    "ALTER TABLE execution_commands ADD COLUMN recovery_context TEXT",
    # External follower-close policy: retain the last authoritative follower
    # quantity and the engine-close cursor for the exact master episode.  A
    # proven non-zero -> flat transition with no newer engine close latches
    # entry refusal across reconciles and process restarts.
    (
        "ALTER TABLE sync_basis ADD COLUMN episode_trade_id INTEGER",
        "ALTER TABLE sync_basis ADD COLUMN last_observed_qty TEXT",
        "ALTER TABLE sync_basis ADD COLUMN last_engine_close_key TEXT",
        "ALTER TABLE sync_basis ADD COLUMN external_close_blocked INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sync_basis ADD COLUMN external_close_reason TEXT",
    ),
    # A requested close is not fill evidence: -2022 can confirm with zero
    # executed, and a terminal MARKET order may fill only part of its request.
    # Protective fills need their own episode-bound durable entry refusal.
    (
        "ALTER TABLE execution_commands ADD COLUMN executed_qty TEXT",
        "ALTER TABLE sync_basis ADD COLUMN own_protective_filled INTEGER NOT NULL DEFAULT 0",
    ),
    # #61 cannot reconstruct a protective FILLED result already observed by
    # e6f0d0f: that query result was never stored. Quarantine its possible
    # episodes instead of treating a legacy observed flat as fresh entry
    # permission when the next order-history read is unavailable. This is a
    # separate migration because #61 may already have run on local databases.
    """
    UPDATE sync_basis SET external_close_blocked=1,
      external_close_reason='UPGRADE_FLAT_CAUSE_UNKNOWN'
    WHERE episode_trade_id IS NOT NULL
      AND last_observed_qty IS NOT NULL
      AND CAST(last_observed_qty AS REAL)=0
      AND zero_target=0 AND external_close_blocked=0 AND own_protective_filled=0
      AND EXISTS (
        SELECT 1 FROM copy_trades AS trade
        WHERE trade.trade_id=sync_basis.episode_trade_id
          AND trade.symbol=sync_basis.symbol AND trade.pos_side=sync_basis.pos_side
          AND trade.status='OPEN'
      )
      AND EXISTS (
        SELECT 1 FROM order_links AS link
        WHERE link.trade_id=sync_basis.episode_trade_id
          AND link.account_id=sync_basis.account_id
          AND link.kind IN ('PLACE_STOP','PLACE_TP')
      )
    """,
]
CASEFOLD_ADDRESS_INDEX_VERSION = next(
    version
    for version, statement in enumerate(MIGRATIONS)
    if "idx_accounts_user_address_lower" in statement
)
RECOVERY_CONTEXT_INDEX_VERSION = next(
    version
    for version, statement in enumerate(MIGRATIONS)
    if isinstance(statement, str) and "ADD COLUMN recovery_context" in statement
)
EXTERNAL_CLOSE_INDEX_VERSION = next(
    version
    for version, statement in enumerate(MIGRATIONS)
    if isinstance(statement, tuple)
    and any("ADD COLUMN external_close_blocked" in step for step in statement)
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Repositories deliberately keep each write short and commit it in
        # the same coroutine.  Explicit autocommit prevents an implicit
        # transaction from remaining open between the awaited ``execute``
        # and ``commit`` calls when many follower reconciliations fan out at
        # once.  Ordinary writes and nonce allocation are serialized through
        # ``write_lock`` below.
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        # concurrent writers: repository writes and nonce allocation share a
        # single connection-level lock.
        await self.conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")

    async def migrate(self) -> int:
        """Applies unapplied MIGRATIONS in order; returns number applied.

        A tuple entry runs its statements inside the same per-version
        transaction, so multi-statement rebuilds stay atomic; the version
        is recorded only on commit.
        """
        if self.conn is None:
            raise RuntimeError("Database.connect() must be called before migrate()")
        # bootstrap the migration bookkeeping table itself
        await self.conn.execute(MIGRATIONS[0])
        await self.conn.commit()
        cur = await self.conn.execute("SELECT COALESCE(MAX(version), -1) FROM schema_migrations")
        try:
            (last_version,) = await cur.fetchone()
        finally:
            await cur.close()
        applied = 0
        for version in range(last_version + 1, len(MIGRATIONS)):
            await self.conn.execute("BEGIN")
            try:
                if version == CASEFOLD_ADDRESS_INDEX_VERSION:
                    await self._assert_casefold_addresses_unique()
                entry = MIGRATIONS[version]
                steps = entry if isinstance(entry, tuple) else (entry,)
                recovery_context_exists = (
                    version == RECOVERY_CONTEXT_INDEX_VERSION
                    and await self._column_exists("execution_commands", "recovery_context")
                )
                if not recovery_context_exists:
                    for step in steps:
                        await self.conn.execute(step)
                await self.conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
                await self.conn.commit()
                applied += 1
            except Exception:
                await self.conn.rollback()
                raise
        return applied

    async def _column_exists(self, table: str, column: str) -> bool:
        """Return whether an exact column exists in a trusted internal table."""
        if self.conn is None:
            raise RuntimeError("Database.connect() must be called first")
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        return any(str(row["name"]) == column for row in rows)

    async def _assert_casefold_addresses_unique(self) -> None:
        """Explain legacy case-only duplicates before SQLite's opaque failure."""
        if self.conn is None:
            raise RuntimeError("Database.connect() must be called first")
        cursor = await self.conn.execute(
            "SELECT group_concat(id, ',') AS account_ids "
            "FROM accounts GROUP BY lower(user_address) HAVING COUNT(*) > 1 "
            "ORDER BY lower(user_address) LIMIT 10"
        )
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        if not rows:
            return
        conflicts = "; ".join(str(row["account_ids"]) for row in rows)
        raise MigrationConflictError(
            "case-insensitive duplicate Aster user addresses in accounts: "
            f"{conflicts}. Disable and resolve the duplicate account rows before restart"
        )

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        if self.conn is None:
            raise RuntimeError("Database.connect() must be called first")
        return await self.conn.execute(sql, params)

    async def execute_write(self, sql: str, params: tuple = ()) -> tuple[int, int | None]:
        """Execute one short write and finalize its cursor before commit.

        aiosqlite cursors are backed by real SQLite statements. Leaving a
        cursor alive across the commit boundary can retain a lock on some
        SQLite builds. Repositories use this helper for all ordinary writes so
        the statement lifetime is explicit and bounded.
        """
        async with self.write_lock:
            cur = await self.execute(sql, params)
            try:
                rowcount = cur.rowcount
                lastrowid = cur.lastrowid
            finally:
                await cur.close()
            if self.conn is None:
                raise RuntimeError("Database.connect() must be called first")
            await self.conn.commit()
        return rowcount, lastrowid

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = True) -> AsyncIterator[aiosqlite.Connection]:
        """Run a short explicit transaction under the process-wide write lock.

        The atomic Stage 2 credential/ownership write deliberately uses this
        primitive with direct statements on the yielded connection.  It must
        not call :meth:`execute_write` from inside the context: that helper
        takes the same lock and commits each statement independently.
        """
        if self.conn is None:
            raise RuntimeError("Database.connect() must be called first")

        async with self.write_lock:
            conn = self.conn
            begin_cursor = await conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            await begin_cursor.close()
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                try:
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise

    async def fetchone(self, sql: str, params: tuple = ()) -> object | None:
        cur = await self.execute(sql, params)
        try:
            return await cur.fetchone()
        finally:
            await cur.close()

    async def fetchall(self, sql: str, params: tuple = ()) -> list:
        cur = await self.execute(sql, params)
        try:
            return list(await cur.fetchall())
        finally:
            await cur.close()

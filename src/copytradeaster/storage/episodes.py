"""Owned episode persistence and stable references; no exchange IDs on public links."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

import aiosqlite

from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import (
    Episode,
    FillAllocation,
    PositionSeed,
    ProjectionBoundary,
    TradeResolutionDTO,
)
from copytradeaster.storage.database import Database
from copytradeaster.storage.events import EventDraft, EventRepository
from copytradeaster.storage.history import HistoryScope


class EpisodeRevisionConflict(ValueError):
    """Another projection won; caller must rebuild from current facts."""


@dataclass(frozen=True, slots=True)
class StoredEpisode:
    episode: Episode
    revision: int


@dataclass(frozen=True, slots=True)
class AllocatedFill:
    """One published episode share of one exchange fill (C2, plan §23.3).

    Exchange facts (``quantity``/``price``/``exchange_realized_pnl``) stay
    separate from the episode share (``allocated_qty``/``role``/
    ``allocated_realized_gross``). Shares of one fill across episodes sum to
    the source fill; a flip shows only its own episode share here.
    """

    trade_ref: str
    exchange_trade_id: str
    order_id: str
    side: str
    position_side: str
    quantity: Decimal
    price: Decimal
    exchange_realized_pnl: Decimal
    occurred_at: int
    role: str
    allocated_qty: Decimal
    allocated_realized_gross: Decimal


class EpisodeRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def reserve_ref(
        self,
        scope: HistoryScope,
        symbol: str,
        leg: PosSide,
        anchor: str,
    ) -> str:
        if not symbol or not anchor:
            raise ValueError("episode anchor required")
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO follower_episode_refs VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(account_id,environment,symbol,position_side,anchor) DO NOTHING",
                (uuid.uuid4().hex, scope.account_id, scope.environment, symbol, leg.value, anchor),
            )
            await cursor.close()
            row = await self._db.fetchone(
                "SELECT COALESCE(a.target_ref,r.public_ref) public_ref "
                "FROM follower_episode_refs r "
                "LEFT JOIN follower_episode_aliases a ON a.alias_ref=r.public_ref "
                "WHERE r.account_id=? AND r.environment=? "
                "AND r.symbol=? AND r.position_side=? AND r.anchor=?",
                (scope.account_id, scope.environment, symbol, leg.value, anchor),
            )
            return str(row["public_ref"])

    async def save(
        self,
        scope: HistoryScope,
        episode: Episode,
        *,
        expected_revision: int,
    ) -> StoredEpisode:
        async with self._db.transaction() as conn:
            return await self._save(conn, scope, episode, expected_revision)

    async def publish(
        self,
        scope: HistoryScope,
        symbol: str,
        episodes: Sequence[Episode],
        expected_revisions: Mapping[str, int],
        *,
        events: Sequence[EventDraft] = (),
        resolutions: Sequence[TradeResolutionDTO] = (),
        boundary: ProjectionBoundary | None = None,
        origin_seeds: tuple | None = None,
        origin_start_ms: int | None = None,
        now_ms: int | None = None,
    ) -> tuple[StoredEpisode, ...]:
        """One atomic publication: episodes, lineage, events, checkpoint.

        Resolutions with kind alias/merge delete the superseded episode row
        and flatten existing chains onto the surviving target; kind split
        only records the verdict for review. The boundary (origin on first
        acceptance, latest on every success) commits in the same
        transaction: a crash before commit leaves the previous publication
        and a dirty range for retry.
        """
        refs = [episode.public_ref for episode in episodes]
        if len(set(refs)) != len(refs) or set(refs) != set(expected_revisions):
            raise ValueError("publication requires exact unique revision set")
        if any(episode.symbol != symbol for episode in episodes):
            raise ValueError("publication symbol mismatch")
        if boundary is not None and (
            boundary.account_id != scope.account_id
            or boundary.environment != scope.environment
            or boundary.symbol != symbol
        ):
            raise ValueError("publication boundary outside scope")
        if (resolutions or boundary is not None) and now_ms is None:
            raise ValueError("lineage publication requires now_ms")
        async with self._db.transaction() as conn:
            saved = []
            for episode in episodes:
                stored = await self._save(
                    conn, scope, episode, expected_revisions[episode.public_ref]
                )
                # The allocation index mirrors the stored payload revision in
                # the same commit; readers never see a mixed revision.
                await self.replace_allocations(conn, scope, stored)
                saved.append(stored)
            if events:
                await EventRepository(self._db).insert_many(conn, scope, events)
            if resolutions:
                await self._record_resolutions(conn, scope, symbol, resolutions, now_ms)
            if boundary is not None:
                await self._store_boundary(
                    conn,
                    boundary,
                    now_ms,
                    origin_seeds=origin_seeds,
                    origin_start_ms=origin_start_ms,
                )
            return tuple(saved)

    async def _record_resolutions(
        self,
        conn: aiosqlite.Connection,
        scope: HistoryScope,
        symbol: str,
        resolutions: Sequence[TradeResolutionDTO],
        now_ms: int,
    ) -> None:
        for resolution in resolutions:
            if not resolution.ref or not resolution.reason:
                raise ValueError("resolution requires ref and reason")
            if resolution.kind in ("alias", "merge") and len(resolution.targets) != 1:
                raise ValueError("alias/merge resolution needs exactly one target")
            cursor = await conn.execute(
                "INSERT INTO follower_episode_resolutions VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,environment,ref) DO UPDATE SET "
                "kind=excluded.kind,targets_json=excluded.targets_json,"
                "reason=excluded.reason,revision=excluded.revision,updated_at=excluded.updated_at",
                (
                    scope.account_id,
                    scope.environment,
                    symbol,
                    resolution.ref,
                    resolution.kind,
                    json.dumps(list(resolution.targets)),
                    resolution.reason,
                    resolution.revision,
                    now_ms,
                ),
            )
            await cursor.close()
            if resolution.kind in ("alias", "merge"):
                (target,) = resolution.targets
                cursor = await conn.execute(
                    "UPDATE follower_episode_aliases SET target_ref=? WHERE target_ref=?",
                    (target, resolution.ref),
                )
                await cursor.close()
                cursor = await conn.execute(
                    "INSERT INTO follower_episode_aliases VALUES (?,?) "
                    "ON CONFLICT(alias_ref) DO UPDATE SET target_ref=excluded.target_ref",
                    (resolution.ref, target),
                )
                await cursor.close()
                cursor = await conn.execute(
                    "DELETE FROM follower_episodes WHERE public_ref=?", (resolution.ref,)
                )
                await cursor.close()
                cursor = await conn.execute(
                    "DELETE FROM follower_episode_allocations "
                    "WHERE account_id=? AND environment=? AND trade_ref=?",
                    (scope.account_id, scope.environment, resolution.ref),
                )
                await cursor.close()

    async def _store_boundary(
        self,
        conn: aiosqlite.Connection,
        boundary: ProjectionBoundary,
        now_ms: int,
        *,
        origin_seeds: tuple | None = None,
        origin_start_ms: int | None = None,
    ) -> None:
        def encode(
            seeds: tuple[tuple[PosSide, PositionSeed], ...],
        ) -> str:
            return json.dumps(
                [
                    {
                        "leg": leg.value,
                        "quantity": format(seed.quantity, "f")
                        if seed.quantity is not None
                        else None,
                        "public_ref": seed.public_ref,
                    }
                    for leg, seed in seeds
                ],
                sort_keys=True,
            )

        rows = [("latest", boundary.cutoff_ms, encode(boundary.seeds))]
        if origin_start_ms is not None:
            # The origin anchors the first replay start, not its cutoff: a
            # late change rolls replay back to where acceptance began, with
            # the initial seeds rather than the proven end state.
            initial = origin_seeds if origin_seeds is not None else boundary.seeds
            rows.append(("origin", origin_start_ms, encode(initial)))
        for kind, cutoff, seeds_json in rows:
            cursor = await conn.execute(
                "INSERT INTO history_projection_boundaries VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,environment,symbol,kind) DO UPDATE SET "
                "cutoff_ms=excluded.cutoff_ms,mode=excluded.mode,seeds_json=excluded.seeds_json,"
                "coverage_revision=excluded.coverage_revision,version=excluded.version,"
                "updated_at=excluded.updated_at",
                (
                    boundary.account_id,
                    boundary.environment,
                    boundary.symbol,
                    kind,
                    cutoff,
                    boundary.mode,
                    seeds_json,
                    boundary.coverage_revision,
                    boundary.version,
                    now_ms,
                ),
            )
            await cursor.close()

    async def _save(
        self,
        conn: aiosqlite.Connection,
        scope: HistoryScope,
        episode: Episode,
        expected_revision: int,
    ) -> StoredEpisode:
        payload = json.dumps(
            asdict(episode), default=_encode, sort_keys=True, separators=(",", ":")
        )
        ref = await self._db.fetchone(
            "SELECT * FROM follower_episode_refs WHERE public_ref=? AND account_id=? "
            "AND environment=? AND symbol=? AND position_side=?",
            (
                episode.public_ref,
                scope.account_id,
                scope.environment,
                episode.symbol,
                episode.position_side.value,
            ),
        )
        if ref is None:
            raise ValueError("episode reference outside scope")
        alias = await self._db.fetchone(
            "SELECT 1 FROM follower_episode_aliases WHERE alias_ref=?",
            (episode.public_ref,),
        )
        if alias is not None:
            raise ValueError("cannot update superseded episode")
        old = await self._db.fetchone(
            "SELECT revision,payload FROM follower_episodes WHERE public_ref=?",
            (episode.public_ref,),
        )
        revision = int(old["revision"]) if old else 0
        if revision != expected_revision:
            raise EpisodeRevisionConflict("episode revision changed")
        if old and old["payload"] == payload:
            return StoredEpisode(episode, revision)
        revision += 1
        cursor = await conn.execute(
            "INSERT INTO follower_episodes VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(public_ref) DO UPDATE SET opened_at=excluded.opened_at, "
            "closed_at=excluded.closed_at,revision=excluded.revision,payload=excluded.payload",
            (
                episode.public_ref,
                scope.account_id,
                scope.environment,
                episode.symbol,
                episode.position_side.value,
                episode.opened_at,
                episode.closed_at,
                revision,
                payload,
            ),
        )
        await cursor.close()
        return StoredEpisode(episode, revision)

    async def get(self, scope: HistoryScope, ref: str) -> StoredEpisode | None:
        # Resolve a single canonical hop. merge_ref flattens existing chains.
        row = await self._db.fetchone(
            "SELECT e.payload,e.revision FROM follower_episode_refs r "
            "LEFT JOIN follower_episode_aliases a ON a.alias_ref=r.public_ref "
            "JOIN follower_episodes e ON e.public_ref=COALESCE(a.target_ref,r.public_ref) "
            "WHERE r.public_ref=? AND r.account_id=? AND r.environment=? "
            "AND e.account_id=r.account_id AND e.environment=r.environment",
            (ref, scope.account_id, scope.environment),
        )
        return StoredEpisode(_decode(row["payload"]), int(row["revision"])) if row else None

    async def list_closed(
        self,
        scope: HistoryScope,
        *,
        symbol: str | None = None,
        since_ms: int | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[StoredEpisode], str | None]:
        """Closed episodes, newest first, over immutable ``(closed_at, ref)``.

        The cursor is an opaque ``"<closed_at_ms>:<ref>"`` position; pages
        stay stable because closed episodes never change their close time.
        """
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("episode page must be bounded")
        if symbol is not None and not symbol:
            raise ValueError("episode symbol filter must be nonempty")
        if since_ms is not None and (type(since_ms) is not int or since_ms < 0):
            raise ValueError("episode period must be a nonnegative time")
        params: list[object] = [scope.account_id, scope.environment]
        where = "account_id=? AND environment=? AND closed_at IS NOT NULL"
        if symbol is not None:
            where += " AND symbol=?"
            params.append(symbol)
        if since_ms is not None:
            where += " AND closed_at>=?"
            params.append(since_ms)
        if cursor is not None:
            closed_at, ref = _parse_cursor(cursor)
            where += " AND (closed_at<? OR (closed_at=? AND public_ref<?))"
            params.extend((closed_at, closed_at, ref))
        rows = await self._db.fetchall(
            "SELECT payload,revision,closed_at,public_ref FROM follower_episodes"
            f" WHERE {where} ORDER BY closed_at DESC, public_ref DESC LIMIT ?",
            (*params, limit + 1),
        )
        stored = [
            StoredEpisode(_decode(row["payload"]), int(row["revision"])) for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = f"{last['closed_at']}:{last['public_ref']}"
        return stored, next_cursor

    async def list_open(
        self, scope: HistoryScope, *, symbol: str | None = None
    ) -> list[StoredEpisode]:
        """Open episodes; the live set is small enough for one page."""
        if symbol is not None and not symbol:
            raise ValueError("episode symbol filter must be nonempty")
        params: list[object] = [scope.account_id, scope.environment]
        where = "account_id=? AND environment=? AND closed_at IS NULL"
        if symbol is not None:
            where += " AND symbol=?"
            params.append(symbol)
        rows = await self._db.fetchall(
            "SELECT payload,revision FROM follower_episodes"
            f" WHERE {where} ORDER BY opened_at DESC NULLS LAST, public_ref",
            tuple(params),
        )
        return [StoredEpisode(_decode(row["payload"]), int(row["revision"])) for row in rows]

    async def list_range(
        self, scope: HistoryScope, symbol: str, start_ms: int, end_ms: int
    ) -> list[StoredEpisode]:
        """Stored episodes overlapping ``[start_ms, end_ms]``, oldest first.

        Serves idempotent republication: when the replay starts past the
        requested end there is nothing new to project, so the caller gets
        back exactly what the range already shows.
        """
        if not symbol:
            raise ValueError("episode symbol required")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms < start_ms
        ):
            raise ValueError("episode range must be ordered")
        rows = await self._db.fetchall(
            "SELECT payload,revision FROM follower_episodes "
            "WHERE account_id=? AND environment=? AND symbol=? "
            "AND (closed_at IS NULL OR closed_at>=?) "
            "AND (opened_at IS NULL OR opened_at<=?) "
            "ORDER BY COALESCE(opened_at, 0), public_ref",
            (scope.account_id, scope.environment, symbol, start_ms, end_ms),
        )
        return [StoredEpisode(_decode(row["payload"]), int(row["revision"])) for row in rows]

    async def merge_ref(self, scope: HistoryScope, alias: str, target: str) -> None:
        """Explicit proven merge only; never infer that equal symbols are the same episode."""
        if alias == target:
            raise ValueError("self alias")
        async with self._db.transaction() as conn:
            rows = await self._db.fetchall(
                "SELECT public_ref,symbol,position_side FROM follower_episode_refs "
                "WHERE account_id=? AND environment=? AND public_ref IN (?,?)",
                (scope.account_id, scope.environment, alias, target),
            )
            if len(rows) != 2 or len({(r["symbol"], r["position_side"]) for r in rows}) != 1:
                raise ValueError("alias outside episode scope")
            if await self._db.fetchone(
                "SELECT 1 FROM follower_episode_aliases WHERE alias_ref IN (?,?)",
                (alias, target),
            ):
                raise ValueError("merge requires canonical references")
            if not await self._db.fetchone(
                "SELECT 1 FROM follower_episodes WHERE public_ref=?",
                (target,),
            ):
                raise ValueError("merge target is not published")
            cursor = await conn.execute(
                "UPDATE follower_episode_aliases SET target_ref=? WHERE target_ref=?",
                (target, alias),
            )
            await cursor.close()
            cursor = await conn.execute(
                "INSERT INTO follower_episode_aliases VALUES (?,?)", (alias, target)
            )
            await cursor.close()
            cursor = await conn.execute(
                "DELETE FROM follower_episodes WHERE public_ref=?", (alias,)
            )
            await cursor.close()
            cursor = await conn.execute(
                "DELETE FROM follower_episode_allocations "
                "WHERE account_id=? AND environment=? AND trade_ref=?",
                (scope.account_id, scope.environment, alias),
            )
            await cursor.close()

    async def load_boundary(
        self, scope: HistoryScope, symbol: str, *, kind: str = "latest"
    ) -> ProjectionBoundary | None:
        """Durable proven replay frontier; None means never reconciled."""
        if kind not in ("origin", "latest"):
            raise ValueError("boundary kind must be origin or latest")
        row = await self._db.fetchone(
            "SELECT cutoff_ms,mode,seeds_json,coverage_revision,version FROM "
            "history_projection_boundaries WHERE account_id=? AND environment=? "
            "AND symbol=? AND kind=?",
            (scope.account_id, scope.environment, symbol, kind),
        )
        if row is None:
            return None
        seeds = tuple(
            (
                PosSide(item["leg"]),
                PositionSeed(
                    Decimal(item["quantity"]) if item["quantity"] is not None else None,
                    item["public_ref"],
                ),
            )
            for item in json.loads(row["seeds_json"])
        )
        return ProjectionBoundary(
            account_id=scope.account_id,
            environment=scope.environment,
            symbol=symbol,
            cutoff_ms=int(row["cutoff_ms"]),
            mode=row["mode"],
            seeds=seeds,
            coverage_revision=int(row["coverage_revision"]),
            version=int(row["version"]),
        )

    async def get_resolution(self, scope: HistoryScope, ref: str) -> TradeResolutionDTO | None:
        """Typed identity verdict for one ref; None means never resolved."""
        row = await self._db.fetchone(
            "SELECT kind,targets_json,reason,revision FROM follower_episode_resolutions "
            "WHERE account_id=? AND environment=? AND ref=?",
            (scope.account_id, scope.environment, ref),
        )
        if row is None:
            return None
        return TradeResolutionDTO(
            ref=ref,
            kind=row["kind"],
            targets=tuple(json.loads(row["targets_json"])),
            reason=row["reason"],
            revision=int(row["revision"]),
        )

    async def list_symbol(self, scope: HistoryScope, symbol: str) -> list[StoredEpisode]:
        """Every stored episode for one scope/symbol, oldest first."""
        if not symbol:
            raise ValueError("episode symbol required")
        rows = await self._db.fetchall(
            "SELECT payload,revision FROM follower_episodes "
            "WHERE account_id=? AND environment=? AND symbol=? "
            "ORDER BY COALESCE(opened_at, 0), public_ref",
            (scope.account_id, scope.environment, symbol),
        )
        return [StoredEpisode(_decode(row["payload"]), int(row["revision"])) for row in rows]

    async def replace_allocations(
        self,
        conn: aiosqlite.Connection,
        scope: HistoryScope,
        stored: StoredEpisode,
    ) -> int:
        """Replace the allocation index rows for one stored episode revision.

        Runs on the caller's connection inside the publication transaction:
        ``publish`` calls it after every ``_save`` so the index never shows
        a mixed revision. Exit-role allocations share the episode
        ``realized_pnl_gross`` pro-rata by allocated qty over the total exit
        qty (the last exit absorbs the remainder so the shares sum exactly);
        entry and unknown roles carry gross zero. Returns the row count.
        """
        episode = stored.episode
        cursor = await conn.execute(
            "DELETE FROM follower_episode_allocations "
            "WHERE account_id=? AND environment=? AND trade_ref=?",
            (scope.account_id, scope.environment, episode.public_ref),
        )
        await cursor.close()
        count = 0
        for allocation, gross in _allocation_gross_shares(episode):
            cursor = await conn.execute(
                "INSERT INTO follower_episode_allocations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    scope.account_id,
                    scope.environment,
                    episode.symbol,
                    episode.position_side.value,
                    episode.public_ref,
                    str(allocation.trade_id),
                    allocation.role,
                    _money_text(allocation.quantity),
                    _money_text(gross),
                    allocation.occurred_at,
                ),
            )
            await cursor.close()
            count += 1
        return count

    async def rebuild_allocations(self, scope: HistoryScope, symbol: str) -> int:
        """Refill the allocation index from stored episode payloads.

        Maintenance path for the migration/tests only: it is never auto-run
        by publication, which keeps the index current on every ``publish``.
        Returns the total indexed allocation count.
        """
        if not symbol:
            raise ValueError("episode symbol required")
        stored = await self.list_symbol(scope, symbol)
        async with self._db.transaction() as conn:
            total = 0
            for item in stored:
                total += await self.replace_allocations(conn, scope, item)
        return total

    async def alias_family(self, scope: HistoryScope, canonical_ref: str) -> tuple[str, ...]:
        """Canonical ref plus every proven alias/merge ref resolving to it.

        Unions the typed resolutions table (publish path) with the raw alias
        links (explicit ``merge_ref`` path, which records no resolution row);
        alias links are re-scoped through the refs table because the links
        table itself carries no account columns. Chains are flattened at
        write time, but collection still resolves transitively with a cycle
        guard so a stale chain can never loop this read.
        """
        if not canonical_ref:
            raise ValueError("episode ref required")
        targets: dict[str, str] = {}
        for row in await self._db.fetchall(
            "SELECT ref,targets_json FROM follower_episode_resolutions "
            "WHERE account_id=? AND environment=? AND kind IN ('alias','merge')",
            (scope.account_id, scope.environment),
        ):
            try:
                (target,) = tuple(json.loads(row["targets_json"]))
            except (ValueError, TypeError):
                continue
            targets[str(row["ref"])] = str(target)
        for row in await self._db.fetchall(
            "SELECT a.alias_ref,a.target_ref FROM follower_episode_aliases a "
            "JOIN follower_episode_refs r ON r.public_ref=a.alias_ref "
            "WHERE r.account_id=? AND r.environment=?",
            (scope.account_id, scope.environment),
        ):
            targets[str(row["alias_ref"])] = str(row["target_ref"])

        def resolves_to(ref: str) -> bool:
            seen = {ref}
            while ref in targets:
                ref = targets[ref]
                if ref == canonical_ref:
                    return True
                if ref in seen:
                    return False
                seen.add(ref)
            return False

        return (canonical_ref, *sorted(ref for ref in targets if resolves_to(ref)))

    async def list_allocated_fills(
        self,
        scope: HistoryScope,
        canonical_refs: Sequence[str],
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[AllocatedFill], str | None]:
        """Episode-owned fills for a proven ref family, oldest first.

        Joins the allocation index to ``follower_fills`` on the full scope
        plus symbol and exchange fill id: never a time-range or side-only
        selection, so overlapping HEDGE legs cannot leak into each other and
        a shared flip fill appears once per episode with only its own share.
        The opaque cursor carries the family revision fingerprint plus the
        ``(occurred_at, trade_id, role)`` position; a changed fingerprint
        raises :class:`EpisodeRevisionConflict` instead of continuing a
        stale page.
        """
        refs = tuple(canonical_refs)
        if not refs or any(not ref for ref in refs) or len(set(refs)) != len(refs):
            raise ValueError("allocated fills require unique episode refs")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("allocated fills page must be bounded")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("allocated fills cursor is invalid")
        revisions = await self._allocation_revisions(scope, refs)
        after: tuple[int, str, str] | None = None
        if cursor is not None:
            wanted, after = _decode_alloc_cursor(cursor)
            if wanted != revisions:
                raise EpisodeRevisionConflict("episode revision changed")
        rows = await self._db.fetchall(
            "SELECT a.trade_ref,a.exchange_trade_id,a.position_side,a.role,"
            "a.allocated_qty,a.allocated_realized_gross,a.occurred_at,"
            "f.order_id,f.side,f.qty,f.price,f.realized_pnl,f.executed_at "
            "FROM follower_episode_allocations a "
            "JOIN follower_fills f ON f.account_id=a.account_id "
            "AND f.environment=a.environment AND f.symbol=a.symbol "
            "AND f.exchange_trade_id=a.exchange_trade_id "
            "WHERE a.account_id=? AND a.environment=? AND a.trade_ref IN ("
            + ",".join("?" * len(refs))
            + ") ORDER BY a.occurred_at,length(a.exchange_trade_id),"
            "a.exchange_trade_id,a.role",
            (scope.account_id, scope.environment, *refs),
        )
        items = [_decode_allocated(row) for row in rows]
        if after is not None:
            key = _alloc_key(*after)
            items = [item for item in items if _alloc_key_of(item) > key]
        shown = items[:limit]
        next_cursor = (
            _encode_alloc_cursor(
                revisions,
                (shown[-1].occurred_at, shown[-1].exchange_trade_id, shown[-1].role),
            )
            if len(items) > limit
            else None
        )
        return shown, next_cursor

    async def _allocation_revisions(
        self, scope: HistoryScope, refs: tuple[str, ...]
    ) -> dict[str, int]:
        """Per-ref revision fingerprint: episode, else resolution, else zero.

        Legacy ``merge_ref`` aliases have neither an episode row (deleted at
        merge) nor a resolution row; they contribute zero, which is stable
        because an alias mapping is immutable once recorded. Family
        membership itself is part of the cursor, so a new alias still trips
        the revision check through the changed ref set.
        """
        revisions: dict[str, int] = {}
        for ref in refs:
            row = await self._db.fetchone(
                "SELECT revision FROM follower_episodes "
                "WHERE account_id=? AND environment=? AND public_ref=?",
                (scope.account_id, scope.environment, ref),
            )
            if row is not None:
                revisions[ref] = int(row["revision"])
                continue
            resolution = await self._db.fetchone(
                "SELECT revision FROM follower_episode_resolutions "
                "WHERE account_id=? AND environment=? AND ref=?",
                (scope.account_id, scope.environment, ref),
            )
            revisions[ref] = int(resolution["revision"]) if resolution is not None else 0
        return revisions


def _money_text(value: Decimal) -> str:
    text = format(value, "f")
    return "0" if value == 0 else text


def _allocation_gross_shares(episode: Episode) -> tuple[tuple[FillAllocation, Decimal], ...]:
    """Per-allocation realized gross: exit shares episode gross pro-rata.

    Exit allocations split ``realized_pnl_gross`` by allocated qty over the
    total exit qty; the last exit absorbs the remainder so the shares sum
    exactly even when the division is not exact. Entry and unknown roles
    carry no realized gross.
    """
    exits = [item for item in episode.allocations if item.role == "exit"]
    total = episode.exit_qty
    out: list[tuple[FillAllocation, Decimal]] = []
    accrued = Decimal(0)
    seen = 0
    for allocation in episode.allocations:
        if allocation.role != "exit":
            out.append((allocation, Decimal(0)))
            continue
        seen += 1
        if seen < len(exits):
            share = (
                episode.realized_pnl_gross * allocation.quantity / total if total else Decimal(0)
            )
        else:
            share = episode.realized_pnl_gross - accrued
        accrued += share
        out.append((allocation, share))
    return tuple(out)


_ALLOC_CURSOR_VERSION = 1
_ALLOC_ROLES = ("entry", "exit", "unknown")


def _encode_alloc_cursor(revisions: Mapping[str, int], pos: tuple[int, str, str]) -> str:
    """Opaque fills cursor: family revision fingerprint plus page position."""
    payload = {
        "v": _ALLOC_CURSOR_VERSION,
        "rev": {ref: revisions[ref] for ref in sorted(revisions)},
        "pos": [pos[0], pos[1], pos[2]],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_alloc_cursor(cursor: str) -> tuple[dict[str, int], tuple[int, str, str]]:
    """Split a fills cursor; any malformed input is a plain ValueError."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        revisions = {str(ref): int(rev) for ref, rev in payload["rev"].items()}
        occurred, trade_id, role = payload["pos"]
        position = (int(occurred), str(trade_id), str(role))
        if payload.get("v") != _ALLOC_CURSOR_VERSION or not revisions:
            raise ValueError("allocated fills cursor is invalid")
        if position[2] not in _ALLOC_ROLES or position[0] < 0 or not position[1]:
            raise ValueError("allocated fills cursor is invalid")
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise ValueError("allocated fills cursor is invalid") from exc
    return revisions, position


def _alloc_key(occurred_at: int, trade_id: str, role: str) -> tuple[int, int, str, str]:
    return (occurred_at, len(trade_id), trade_id, role)


def _alloc_key_of(item: AllocatedFill) -> tuple[int, int, str, str]:
    return _alloc_key(item.occurred_at, item.exchange_trade_id, item.role)


def _decode_allocated(row: object) -> AllocatedFill:
    return AllocatedFill(
        trade_ref=str(row["trade_ref"]),
        exchange_trade_id=str(row["exchange_trade_id"]),
        order_id=str(row["order_id"]),
        side=str(row["side"]),
        position_side=str(row["position_side"]),
        quantity=Decimal(str(row["qty"])),
        price=Decimal(str(row["price"])),
        exchange_realized_pnl=Decimal(str(row["realized_pnl"])),
        occurred_at=int(row["occurred_at"]),
        role=str(row["role"]),
        allocated_qty=Decimal(str(row["allocated_qty"])),
        allocated_realized_gross=Decimal(str(row["allocated_realized_gross"])),
    )


def _encode(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError("unsupported episode value")


def _decode(payload: str) -> Episode:
    data = json.loads(payload)
    for key in (
        "remaining_qty",
        "entry_qty",
        "entry_quote",
        "exit_qty",
        "exit_quote",
        "realized_pnl_gross",
    ):
        if data[key] is not None:
            data[key] = Decimal(data[key])
    data["position_side"] = PosSide(data["position_side"])
    data["allocations"] = tuple(
        FillAllocation(**{**item, "quantity": Decimal(item["quantity"])})
        for item in data["allocations"]
    )
    return Episode(**data)


def _parse_cursor(cursor: str) -> tuple[int, str]:
    closed, _, ref = cursor.partition(":")
    if not closed.isdigit() or not ref:
        raise ValueError("episode cursor is invalid")
    return int(closed), ref

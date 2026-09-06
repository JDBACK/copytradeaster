"""Owned episode persistence and stable references; no exchange IDs on public links."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

import aiosqlite

from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import Episode, FillAllocation
from copytradeaster.storage.database import Database
from copytradeaster.storage.history import HistoryScope


class EpisodeRevisionConflict(ValueError):
    """Another projection won; caller must rebuild from current facts."""


@dataclass(frozen=True, slots=True)
class StoredEpisode:
    episode: Episode
    revision: int


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
    ) -> tuple[StoredEpisode, ...]:
        refs = [episode.public_ref for episode in episodes]
        if len(set(refs)) != len(refs) or set(refs) != set(expected_revisions):
            raise ValueError("publication requires exact unique revision set")
        if any(episode.symbol != symbol for episode in episodes):
            raise ValueError("publication symbol mismatch")
        async with self._db.transaction() as conn:
            saved = []
            for episode in episodes:
                saved.append(
                    await self._save(conn, scope, episode, expected_revisions[episode.public_ref])
                )
            return tuple(saved)

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

"""Atomic episode publication with linked user events."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

import pytest

from copytradeaster.domain.models import PosSide
from copytradeaster.history.models import PositionSeed
from copytradeaster.history.projection import HistoryProjection
from copytradeaster.history.reader import Window, WindowRead
from copytradeaster.storage.episodes import EpisodeRepository, EpisodeRevisionConflict
from copytradeaster.storage.events import EventDraft, EventRepository
from tests.unit.history.test_projector import fill, project


def draft(ref, **updates):
    params = {
        "kind": "POSITION_OPENED",
        "trade_ref": ref,
        "occurred_at": 1,
        "recorded_at": 2,
        "facts": {"symbol": "BTCUSDT"},
        "dedup_key": f"evt:open:{ref}",
        "notify_eligible": True,
    }
    params.update(updates)
    return EventDraft(**params)


async def create(repo, scope, anchor="fill-1"):
    ref = await repo.reserve_ref(scope, "BTCUSDT", PosSide.BOTH, anchor)
    episode = replace(project([fill(1, "BUY", "1")])[0], public_ref=ref)
    return await repo.save(scope, episode, expected_revision=0)


async def test_publish_persists_episodes_and_events_together(storage):
    db, _, scope = storage
    repo, events = EpisodeRepository(db), EventRepository(db)
    stored = await create(repo, scope)
    changed = replace(stored.episode, source="external")
    (saved,) = await repo.publish(
        scope,
        "BTCUSDT",
        [changed],
        {stored.episode.public_ref: 1},
        events=[draft(stored.episode.public_ref)],
    )
    assert saved.revision == 2
    (row,) = await events.list_owned(scope)
    assert row.event.trade_ref == stored.episode.public_ref
    assert row.event.kind == "POSITION_OPENED" and row.event.recorded_at == 2


async def test_publish_conflict_rolls_back_events(storage):
    db, _, scope = storage
    repo, events = EpisodeRepository(db), EventRepository(db)
    stored = await create(repo, scope)
    changed = replace(stored.episode, source="external")
    with pytest.raises(EpisodeRevisionConflict):
        await repo.publish(
            scope,
            "BTCUSDT",
            [changed],
            {stored.episode.public_ref: 0},
            events=[draft(stored.episode.public_ref)],
        )
    assert await repo.get(scope, stored.episode.public_ref) == stored
    assert await events.list_owned(scope) == []


async def test_publish_republish_dedups_events(storage):
    db, _, scope = storage
    repo, events = EpisodeRepository(db), EventRepository(db)
    stored = await create(repo, scope)
    kwargs = dict(
        scope=scope,
        symbol="BTCUSDT",
        episodes=[stored.episode],
        expected_revisions={stored.episode.public_ref: 1},
    )
    first = await repo.publish(events=[draft(stored.episode.public_ref)], **kwargs)
    second = await repo.publish(events=[draft(stored.episode.public_ref)], **kwargs)
    assert first == second
    assert len(await events.list_owned(scope)) == 1


async def test_publish_invalid_draft_aborts_episode_write(storage):
    db, _, scope = storage
    repo, events = EpisodeRepository(db), EventRepository(db)
    ref = await repo.reserve_ref(scope, "BTCUSDT", PosSide.BOTH, "fresh")
    episode = replace(project([fill(2, "BUY", "1")])[0], public_ref=ref)
    with pytest.raises(ValueError, match="unknown event kind"):
        await repo.publish(
            scope,
            "BTCUSDT",
            [episode],
            {ref: 0},
            events=[draft(ref, kind="MARKET_CLOSE")],
        )
    assert await repo.get(scope, ref) is None
    assert await events.list_owned(scope) == []


async def test_projection_emits_open_event_with_trade_link(storage):
    db, history, scope = storage
    fills = (fill(1, "BUY", "2"),)
    window = Window(0, 2)
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(fills, (window,), (), (), 1), checked_at=3
    )
    repo, events = EpisodeRepository(db), EventRepository(db)
    projection = HistoryProjection(history, repo, events)
    kwargs = dict(
        seeds={PosSide.BOTH: PositionSeed(D(0))},
        ending={PosSide.BOTH: D(2)},
        recorded_at=10,
        notify_eligible=True,
    )
    (stored,) = await projection.publish_window(scope, "BTCUSDT", window, **kwargs)
    (row,) = await events.list_owned(scope)
    assert row.event.kind == "POSITION_OPENED"
    assert row.event.trade_ref == stored.episode.public_ref
    assert row.event.recorded_at == 10 and row.event.notify_eligible is True
    await projection.publish_window(scope, "BTCUSDT", window, **kwargs)
    assert len(await events.list_owned(scope)) == 1


async def test_projection_without_recorded_at_refuses_emission(storage):
    db, history, scope = storage
    fills = (fill(1, "BUY", "2"),)
    window = Window(0, 2)
    await history.commit_fills(
        scope, "BTCUSDT", window, WindowRead(fills, (window,), (), (), 1), checked_at=3
    )
    repo, events = EpisodeRepository(db), EventRepository(db)
    projection = HistoryProjection(history, repo, events)
    with pytest.raises(ValueError, match="recorded_at"):
        await projection.publish_window(
            scope,
            "BTCUSDT",
            window,
            seeds={PosSide.BOTH: PositionSeed(D(0))},
            ending={PosSide.BOTH: D(2)},
        )
    assert await db.fetchall("SELECT * FROM follower_episodes") == []
    assert await events.list_owned(scope) == []

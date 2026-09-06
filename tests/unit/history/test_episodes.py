from dataclasses import replace

import pytest

from copytradeaster.domain.models import PosSide
from copytradeaster.storage.episodes import EpisodeRepository, EpisodeRevisionConflict
from tests.unit.history.test_projector import fill, project


async def create(repo, scope, anchor="fill-1"):
    ref = await repo.reserve_ref(scope, "BTCUSDT", PosSide.BOTH, anchor)
    episode = replace(project([fill(1, "BUY", "1")])[0], public_ref=ref)
    return await repo.save(scope, episode, expected_revision=0)


async def test_ref_and_episode_survive_restart_without_revision_churn(storage):
    db, _, scope = storage
    repo = EpisodeRepository(db)
    stored = await create(repo, scope)
    await db.close()
    await db.connect()
    assert (
        await repo.reserve_ref(scope, "BTCUSDT", PosSide.BOTH, "fill-1")
        == stored.episode.public_ref
    )
    assert await repo.get(scope, stored.episode.public_ref) == stored
    assert await repo.save(scope, stored.episode, expected_revision=1) == stored


async def test_stale_projection_cannot_overwrite_new_revision(storage):
    db, _, scope = storage
    repo = EpisodeRepository(db)
    stored = await create(repo, scope)
    changed = replace(stored.episode, source="external")
    updated = await repo.save(scope, changed, expected_revision=1)
    assert updated.revision == 2
    with pytest.raises(EpisodeRevisionConflict):
        await repo.save(scope, stored.episode, expected_revision=1)
    assert await repo.get(scope, stored.episode.public_ref) == updated


async def test_reference_cannot_cross_account_or_environment(storage):
    db, _, scope = storage
    repo = EpisodeRepository(db)
    stored = await create(repo, scope)
    for foreign in (replace(scope, account_id="b"), replace(scope, environment="mainnet")):
        assert await repo.get(foreign, stored.episode.public_ref) is None
        with pytest.raises(ValueError, match="scope"):
            await repo.save(foreign, stored.episode, expected_revision=1)


async def test_alias_merges_preserve_all_old_links_without_chains(storage):
    db, _, scope = storage
    repo = EpisodeRepository(db)
    a, b, c = [await create(repo, scope, str(n)) for n in range(3)]
    await repo.merge_ref(scope, a.episode.public_ref, b.episode.public_ref)
    await repo.merge_ref(scope, b.episode.public_ref, c.episode.public_ref)
    assert await repo.reserve_ref(scope, "BTCUSDT", PosSide.BOTH, "0") == c.episode.public_ref
    for old in (a, b, c):
        assert await repo.get(scope, old.episode.public_ref) == c
    with pytest.raises(ValueError):
        await repo.merge_ref(scope, c.episode.public_ref, a.episode.public_ref)
    with pytest.raises(ValueError, match="superseded"):
        await repo.save(scope, a.episode, expected_revision=0)


async def test_cross_account_alias_fails_without_erasing_source(storage):
    db, _, scope = storage
    repo = EpisodeRepository(db)
    a = await create(repo, scope)
    b = await create(repo, replace(scope, account_id="b"))
    with pytest.raises(ValueError, match="scope"):
        await repo.merge_ref(scope, a.episode.public_ref, b.episode.public_ref)
    assert await repo.get(scope, a.episode.public_ref) == a

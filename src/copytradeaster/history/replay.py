"""Pure replay planning over durable projection boundaries.

Seven days bound the exchange request size, never an episode lifetime and
never the public identity: replay restarts at the last proven boundary and
only a late fact at or before that boundary rolls back to the accepted
origin. Completed episodes outside the replay range are never rewritten.
"""

from __future__ import annotations

from copytradeaster.history.models import ImportDelta, ProjectionBoundary, ReplayDecision


def choose_replay_range(
    *,
    latest: ProjectionBoundary | None,
    origin: ProjectionBoundary | None,
    delta: ImportDelta,
    fallback_start_ms: int,
) -> ReplayDecision:
    """Plan where a projection replay must restart.

    The normal path resumes right after the latest proven cutoff with its
    seeds. A late change (new fact or newly verified coverage at or before
    the proven cutoff) invalidates the boundary as an immutable seed, so
    replay restarts at the accepted origin. Without any boundary the
    caller-supplied fallback starts the first acceptance.
    """
    if type(fallback_start_ms) is not int or fallback_start_ms < 0:
        raise ValueError("replay fallback must be a nonnegative time")
    if latest is None:
        return ReplayDecision(start_ms=fallback_start_ms, from_origin=False)
    late: int | None = None
    for candidate in (delta.changed_from_ms, delta.coverage_changed_from_ms):
        if candidate is not None and (late is None or candidate < late):
            late = candidate
    if late is not None and late <= latest.cutoff_ms:
        start = origin.cutoff_ms if origin is not None else fallback_start_ms
        return ReplayDecision(start_ms=start, from_origin=True)
    return ReplayDecision(start_ms=latest.cutoff_ms + 1, from_origin=False)

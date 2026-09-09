"""Shared production-App seams for controlled signed acceptance harnesses."""

from __future__ import annotations

from typing import Any

import pytest


async def arm_manual_master_admission(app: Any, client: Any) -> None:
    """Open the process-local entry boundary after a signed REST baseline.

    These focused Mini App acceptance tests intentionally do not run the
    background master WebSocket loop: every state transition is followed by
    an authoritative ``rebuild_full`` before reconciliation. Hardening added
    a separate process-local admission bit, so the controlled harness must
    establish its equivalent baseline explicitly instead of being silently
    PAUSED forever.
    """
    await app.projector.rebuild_full(client, "main")
    if app.projector.dirty_symbols:
        pytest.fail(f"master baseline stayed dirty: {sorted(app.projector.dirty_symbols)}")
    app._stream_connected = True  # noqa: SLF001 - explicit live harness seam
    app._entry_admission_ready = True  # noqa: SLF001 - explicit live harness seam
    if not app._master_entry_admitted():  # noqa: SLF001 - assert the exact production predicate
        pytest.fail("controlled master admission could not be established")


__all__ = ["arm_manual_master_admission"]

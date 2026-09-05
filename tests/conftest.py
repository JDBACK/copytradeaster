"""Fail-closed collection boundary for every signed testnet test."""

import os
from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    live_root = Path(__file__).parent / "testnet"
    for item in items:
        if not Path(item.path).is_relative_to(live_root):
            continue
        item.add_marker(pytest.mark.testnet)
        if os.environ.get("CT_TESTNET") != "1":
            item.add_marker(pytest.mark.skip(reason="signed tests require CT_TESTNET=1"))


@pytest.fixture(autouse=True)
def serialize_signed_campaign(request):
    """Different pytest processes must not trade the same provisioned accounts."""
    if request.node.get_closest_marker("testnet") is None or os.getenv("CT_TESTNET") != "1":
        yield
        return
    import fcntl

    lock_path = Path(__file__).parents[1] / "data" / "signed-testnet-campaign.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail("another signed pytest campaign already owns these accounts")
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

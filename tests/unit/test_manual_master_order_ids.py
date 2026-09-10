"""Manual testnet helper: client order ids must never repeat (-4015)."""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "manual_master_order.py"


@pytest.fixture(scope="module")
def manual():
    spec = importlib.util.spec_from_file_location("manual_master_order", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self) -> None:
        self.order_ids: list[str] = []

    async def place_order(self, account_id: str, request) -> None:
        self.order_ids.append(request.new_client_order_id)


def position(amount: str = "1"):
    from copytradeaster.domain.models import PosSide

    return type("P", (), {"position_amt": Decimal(amount), "position_side": PosSide.BOTH})()


async def test_protective_ids_stay_unique_for_one_price_at_one_instant(manual):
    """Two operations in the same second used to reuse one clientOrderId.

    Aster answers a repeated id with -4015, which is exactly the failure this
    helper script must not manufacture while a manual testnet run is in flight.
    """
    client = FakeClient()
    price, tick = Decimal("78000"), Decimal("0.1")
    await manual._place_protective(client, "BTCUSDT", position(), "STOP_MARKET", price, tick)
    await manual._place_protective(client, "BTCUSDT", position(), "STOP_MARKET", price, tick)

    assert len(client.order_ids) == 2
    assert client.order_ids[0] != client.order_ids[1]
    assert all(item.startswith("manual-STOP_MARKET-78000-") for item in client.order_ids)


def test_order_marker_is_not_the_old_second_counter(manual):
    markers = {manual._order_marker() for _ in range(50)}
    assert len(markers) == 50
    assert all(len(marker) == 8 for marker in markers)

import pytest

from copytradeaster.storage.database import Database
from copytradeaster.storage.history import HistoryRepository, HistoryScope


@pytest.fixture
async def storage(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    await db.connect()
    await db.migrate()
    for name in ("a", "b"):
        await db.execute_write(
            "INSERT INTO accounts(id,role,label,user_address) VALUES (?, 'follower',?,?)",
            (name, name, name),
        )
    yield db, HistoryRepository(db), HistoryScope("a", "testnet")
    await db.close()

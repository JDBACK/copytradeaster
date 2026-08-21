import asyncio
from pathlib import Path

from copytradeaster.cli.main import parse_wallet_file

WALLET_FIXTURE = """\
main test

evm adress: 0xAAAA1111aaaa2222bbbb3333cccc4444dddd5555

api wallet adress: 0x66667777eeee8888ffff00001111222233334444

privte key: 0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa


copyc1

evm adress: 0xBBBB1111aaaa2222bbbb3333cccc4444dddd5555

api wallet adress: 0x66668888eeee8888ffff00001111222233334444

privte key: 0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

main privte key: 0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
"""


def test_parse_five_blocks_with_typo(tmp_path: Path):
    # emulate the real file: five blocks
    content = (
        WALLET_FIXTURE + "\n\ncopyc2\n\nevm adress: 0xC\n\napi wallet adress: 0xD"
        "\n\nprivte key: 0xdd\n"
    )
    p = tmp_path / "wallet.txt"
    p.write_text(content)
    blocks = parse_wallet_file(p)
    assert len(blocks) == 3
    main = blocks[0]
    assert main["label"] == "main test"
    assert main["user"] == "0xAAAA1111aaaa2222bbbb3333cccc4444dddd5555"
    assert main["signer_address"] == "0x66667777eeee8888ffff00001111222233334444"
    assert main["signer_key"].startswith("0xaaa")
    assert "main_key" not in main  # absent line -> None semantics
    copyc1 = blocks[1]
    assert copyc1["label"] == "copyc1"
    assert copyc1["main_key"].startswith("0xccc")


def test_role_detection_main_prefix():
    assert "main test".lower().startswith("main")
    assert not "copyc1".lower().startswith("main")


def test_import_wallet_roundtrip(tmp_path: Path, monkeypatch):
    """import-wallet writes accounts+credentials; second import = upsert, no dupes."""
    from copytradeaster.credentials.service import SqliteCredentials
    from copytradeaster.storage.database import Database
    from copytradeaster.storage.repositories import AccountRepo

    async def run():
        p = tmp_path / "wallet.txt"
        p.write_text(WALLET_FIXTURE)
        blocks = parse_wallet_file(p)
        db = Database(tmp_path / "w.sqlite3")
        await db.connect()
        await db.migrate()
        creds = SqliteCredentials(db, bytes(range(32)))
        for block in blocks:
            label = block["label"]
            role = "master" if label.lower().startswith("main") else "follower"
            account_id = "main" if role == "master" else label
            await creds.import_identity(
                account_id=account_id,
                user=block["user"],
                signer_private_key_hex=block["signer_key"],
                role=role,
                label=label,
                main_private_key_hex=block.get("main_key"),
            )
        rows = await AccountRepo(db).get_all()
        assert len(rows) == len(blocks) == 2
        first_count = len(creds.account_ids())
        # re-import same file -> upsert without duplicates
        for block in blocks:
            label = block["label"]
            role = "master" if label.lower().startswith("main") else "follower"
            account_id = "main" if role == "master" else label
            await creds.import_identity(
                account_id=account_id,
                user=block["user"],
                signer_private_key_hex=block["signer_key"],
                role=role,
                label=label,
                main_private_key_hex=block.get("main_key"),
            )
        assert len(creds.account_ids()) == first_count
        rows_after = await AccountRepo(db).get_all()
        assert len(rows_after) == len(blocks)
        await db.close()

    asyncio.run(run())

from pathlib import Path

import pytest
from pydantic import ValidationError

from copytradeaster.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, encryption_key="ab" * 32, **overrides)


@pytest.mark.parametrize("assignment", ["BOT_KEY=", "BOT_KEY = "])
def test_botkey_compatibility_loader_and_redaction(tmp_path: Path, assignment: str) -> None:
    botkey = tmp_path / ".botkey"
    botkey.write_text(f"# local only\n{assignment}123456:dummy-secret\n", encoding="utf-8")

    settings = _settings(telegram_bot_key_file=botkey)

    assert settings.telegram_token_value() == "123456:dummy-secret"
    redacted = settings.redacted_dump()
    assert redacted["telegram_bot_token"] == "***"
    assert "dummy-secret" not in repr(redacted)


def test_explicit_token_wins_over_compatibility_file(tmp_path: Path) -> None:
    botkey = tmp_path / ".botkey"
    botkey.write_text("BOT_KEY=file-token\n", encoding="utf-8")

    settings = _settings(
        telegram_bot_token="env-token",
        telegram_bot_key_file=botkey,
    )

    assert settings.telegram_token_value() == "env-token"


def test_admin_ids_and_egress_ips_are_normalized() -> None:
    settings = _settings(
        telegram_admin_ids="[100, 100, 200]",
        aster_egress_ips="10.0.0.1, 10.0.0.1 10.0.0.2",
    )

    assert settings.telegram_admin_ids == [100, 200]
    assert settings.aster_egress_ips == ["10.0.0.1", "10.0.0.2"]


def test_egress_ip_validation_rejects_non_network_values() -> None:
    with pytest.raises(ValidationError, match="CT_ASTER_EGRESS_IPS"):
        _settings(aster_egress_ips="definitely-not-an-ip")


def test_testnet_ip_whitelist_override_is_fail_closed() -> None:
    with pytest.raises(ValidationError, match="only for testnet"):
        _settings(
            rest_base_url="https://fapi.asterdex.com",
            ws_user_base="wss://fstream.asterdex.com/ws",
            allow_mainnet=True,
            testnet_ip_whitelist_override=True,
        )

    with pytest.raises(ValidationError, match="only for testnet"):
        _settings(
            rest_base_url="https://fapi.asterdex-testnet.com.attacker.example",
            ws_user_base="wss://fstream.asterdex-testnet.com.attacker.example/ws",
            testnet_ip_whitelist_override=True,
        )
    with pytest.raises(ValidationError, match="refuses non-testnet"):
        _settings(
            rest_base_url="https://fapi.asterdex-testnet.com.attacker.example",
            ws_user_base="wss://fstream.asterdex-testnet.com.attacker.example/ws",
        )

    settings = _settings(testnet_ip_whitelist_override=True)
    assert settings.testnet_ip_whitelist_override is True


def test_telegram_limits_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="telegram_polling_timeout_s"):
        _settings(telegram_polling_timeout_s=0)
    with pytest.raises(ValidationError, match="telegram_tasks_concurrency_limit"):
        _settings(telegram_tasks_concurrency_limit=0)


def test_missing_token_error_is_safe(tmp_path: Path) -> None:
    settings = _settings(telegram_bot_key_file=tmp_path / "missing")

    with pytest.raises(ValueError, match="CT_TELEGRAM_BOT_TOKEN"):
        settings.telegram_token_value()

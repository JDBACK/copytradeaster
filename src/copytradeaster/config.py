"""Application settings loaded from environment (prefix CT_) and .env file."""

import ipaddress
import json
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from copytradeaster.logging_utils import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_LOG_PATH,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CT_", env_file=".env", extra="ignore")

    rest_base_url: str = "https://fapi.asterdex-testnet.com"
    ws_user_base: str = "wss://fstream.asterdex-testnet.com/ws"
    db_path: Path = Path("data/copytrade.sqlite3")
    encryption_key: SecretStr  # hex, decodes to exactly 32 bytes; required
    log_level: str = "INFO"
    log_path: Path = DEFAULT_LOG_PATH
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT
    reconcile_interval_s: int = 15
    listenkey_keepalive_min: int = 50
    weight_limit_per_min: int = 2400  # testnet reports sentinel -2; mainnet value as default
    orders_limit_per_min: int = 1200
    orders_limit_per_10s: int = 300
    fee_buffer_pct: Decimal = Decimal("0.001")  # fee+slippage buffer on unit price
    working_type: str = "MARK_PRICE"  # trigger price type for follower protective orders
    position_mode: str = "ONE_WAY"  # ONE_WAY | HEDGE
    symbol_whitelist: list[str] = ["BTCUSDT", "ETHUSDT"]
    registration_chain_id: int = 56  # EIP-712 domain for ApproveAgent/ApproveBuilder
    builder_address: str = ""  # empty -> approve_agent goes without builder
    builder_name: str = "copytradeaster"
    max_fee_rate: str = "0.0001"  # builder fee cap (futures limit 0.001)
    dry_run: bool = False  # True: commands are logged, no HTTP sent
    allow_mainnet: bool = False
    shadow_accounts_enabled: bool = False
    # Shadow load runs may disable synthetic market-triggered exits so a
    # live testnet price move cannot invalidate a deterministic copy cycle.
    shadow_auto_trigger_conditionals: bool = True
    # Explicit load-profile optimization. Empty keeps the authoritative
    # all-symbol follower snapshot used by normal production reconciliation.
    follower_snapshot_symbols: list[str] = []

    # Stage 2 Telegram transport.  The token is optional for Stage 1 CLI/core
    # processes; Telegram runtime raises a clear error if it is not configured.
    # Prefer CT_TELEGRAM_BOT_TOKEN.  For local compatibility only, a file with
    # one ``BOT_KEY=...`` line (normally .botkey) is read without ever logging
    # its value.
    telegram_bot_token: SecretStr | None = None
    telegram_bot_key_file: Path = Path(".botkey")
    telegram_admin_ids: list[int] | str = []
    telegram_polling_timeout_s: int = 10
    telegram_tasks_concurrency_limit: int = 16
    telegram_notifications_enabled: bool = True
    telegram_outbox_poll_interval_s: Decimal = Decimal("1")
    aster_egress_ips: list[str] | str = []
    require_agent_ip_whitelist: bool = True
    testnet_ip_whitelist_override: bool = False

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, v: SecretStr | str) -> SecretStr:
        value = v.get_secret_value() if isinstance(v, SecretStr) else v
        try:
            raw = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("CT_ENCRYPTION_KEY must be a hex string") from exc
        if len(raw) != 32:
            raise ValueError("CT_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return SecretStr(value)

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("CT_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return normalized

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _normalize_bot_token(cls, value: SecretStr | str | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        raw = raw.strip()
        return SecretStr(raw) if raw else None

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: list[int] | str | None) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = re.split(r"[\s,]+", raw.strip("[]"))
            value = decoded if isinstance(decoded, list) else [decoded]
        try:
            result = [int(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise ValueError("CT_TELEGRAM_ADMIN_IDS must contain integer IDs") from exc
        if any(item <= 0 for item in result):
            raise ValueError("CT_TELEGRAM_ADMIN_IDS must contain positive IDs")
        return list(dict.fromkeys(result))

    @field_validator("aster_egress_ips", mode="before")
    @classmethod
    def _parse_egress_ips(cls, value: list[str] | str | None) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = re.split(r"[\s,]+", raw.strip("[]"))
            value = decoded if isinstance(decoded, list) else [decoded]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("CT_ASTER_EGRESS_IPS must be a list or string")
        normalized = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        for item in normalized:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError("CT_ASTER_EGRESS_IPS must contain IPs or CIDRs") from exc
        return normalized

    @field_validator("position_mode")
    @classmethod
    def _validate_position_mode(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"ONE_WAY", "HEDGE"}:
            raise ValueError("CT_POSITION_MODE must be ONE_WAY or HEDGE")
        return normalized

    @field_validator("working_type")
    @classmethod
    def _validate_working_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"MARK_PRICE", "CONTRACT_PRICE"}:
            raise ValueError("CT_WORKING_TYPE must be MARK_PRICE or CONTRACT_PRICE")
        return normalized

    @field_validator("symbol_whitelist", "follower_snapshot_symbols")
    @classmethod
    def _normalize_symbols(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(symbol.strip().upper() for symbol in values if symbol.strip()))

    @model_validator(mode="after")
    def _load_local_botkey(self) -> "Settings":
        if self.telegram_bot_token is None:
            token = _read_botkey(self.telegram_bot_key_file)
            if token:
                self.telegram_bot_token = SecretStr(token)
        return self

    @model_validator(mode="after")
    def _validate_runtime_guards(self) -> "Settings":
        positive = {
            "reconcile_interval_s": self.reconcile_interval_s,
            "listenkey_keepalive_min": self.listenkey_keepalive_min,
            "weight_limit_per_min": self.weight_limit_per_min,
            "orders_limit_per_min": self.orders_limit_per_min,
            "orders_limit_per_10s": self.orders_limit_per_10s,
            "registration_chain_id": self.registration_chain_id,
            "telegram_polling_timeout_s": self.telegram_polling_timeout_s,
            "telegram_tasks_concurrency_limit": self.telegram_tasks_concurrency_limit,
            "log_max_bytes": self.log_max_bytes,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"settings must be positive: {', '.join(invalid)}")
        if not Decimal(0) <= self.fee_buffer_pct < Decimal(1):
            raise ValueError("CT_FEE_BUFFER_PCT must be in [0, 1)")
        if not self.symbol_whitelist:
            raise ValueError("CT_SYMBOL_WHITELIST must not be empty")
        if self.telegram_outbox_poll_interval_s <= 0:
            raise ValueError("CT_TELEGRAM_OUTBOX_POLL_INTERVAL_S must be positive")
        if self.telegram_polling_timeout_s <= 0:
            raise ValueError("CT_TELEGRAM_POLLING_TIMEOUT_S must be positive")
        if self.telegram_tasks_concurrency_limit <= 0:
            raise ValueError("CT_TELEGRAM_TASKS_CONCURRENCY_LIMIT must be positive")
        if self.log_backup_count < 0:
            raise ValueError("CT_LOG_BACKUP_COUNT must be non-negative")
        if self.testnet_ip_whitelist_override and not self.is_testnet:
            raise ValueError(
                "CT_TESTNET_IP_WHITELIST_OVERRIDE is allowed only for testnet REST/WS URLs"
            )
        if not self.allow_mainnet and not self.is_testnet:
            raise ValueError(
                "Stage 1 refuses non-testnet REST/WS URLs; "
                "CT_ALLOW_MAINNET is a future-stage override"
            )
        return self

    def encryption_key_bytes(self) -> bytes:
        return bytes.fromhex(self.encryption_key.get_secret_value())

    @property
    def is_testnet(self) -> bool:
        """Whether both exchange endpoints are the exact official testnet hosts."""
        rest_host = (urlparse(self.rest_base_url).hostname or "").lower().rstrip(".")
        ws_host = (urlparse(self.ws_user_base).hostname or "").lower().rstrip(".")
        return (
            rest_host == "fapi.asterdex-testnet.com" and ws_host == "fstream.asterdex-testnet.com"
        )

    def telegram_token_value(self) -> str:
        if self.telegram_bot_token is None:
            raise ValueError(
                "Telegram bot token is not configured; set CT_TELEGRAM_BOT_TOKEN "
                "or create a local .botkey containing BOT_KEY=..."
            )
        return self.telegram_bot_token.get_secret_value()

    def redacted_dump(self) -> dict:
        """Return settings suitable for status/log output without secrets."""
        data = self.model_dump()
        data["encryption_key"] = "***"
        data["telegram_bot_token"] = "***" if self.telegram_bot_token else None
        return data


def _read_botkey(path: Path) -> str | None:
    """Read the explicit local ``BOT_KEY=...`` compatibility format.

    The value is intentionally returned only to the settings object and is
    never logged, included in an exception, or exposed in ``redacted_dump``.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw_value = stripped.partition("=")
        # Accept the conventional ``BOT_KEY=value`` and the human-written
        # ``BOT_KEY = value`` form.  Match the complete key so similarly named
        # variables cannot be mistaken for the bot token.
        if separator and key.strip() == "BOT_KEY":
            candidate = raw_value.strip()
            return candidate or None
    return None

"""Application settings loaded from environment (prefix CT_) and .env file."""

import ipaddress
import json
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from copytradeaster.aster.network_profile import AsterNetworkProfile
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
    # v1 wire policy (Package E, single policy): priceProtect is NEVER sent on
    # the wire. Deleted from the planner contract (behavior-neutral: the
    # executor mapping and PlaceOrderRequest.to_params never carried it, so
    # the exchange default already governed every STOP/TP order).
    position_mode: str = "ONE_WAY"  # ONE_WAY | HEDGE
    symbol_whitelist: list[str] = ["BTCUSDT", "ETHUSDT"]
    trading_chain_id: int = 714  # EIP-712 trading domain (714 testnet, 1666 mainnet)
    registration_chain_id: int = 56  # EIP-712 domain for ApproveAgent/ApproveBuilder
    builder_address: str = ""  # empty -> approve_agent goes without builder
    builder_name: str = "copytradeaster"
    max_fee_rate: str = "0.0001"  # builder fee cap (futures limit 0.001)
    # Stage 3 Builder Mini App flow
    builder_agent_expiry_days: int = 30  # approveAgent `expired` lifetime set by the backend
    builder_approval_ttl_s: int = 600  # prepare -> wallet-signature submit window
    # Stage 3 Mini App transport (aiohttp in the same process)
    miniapp_enabled: bool = False
    miniapp_bind_host: str = "127.0.0.1"
    miniapp_port: int = 8080
    miniapp_static_dir: Path | None = None  # built Vite assets (miniapp/dist)
    # Public https URL of the deployed Mini App; when set, the bot installs it
    # as the private-chat menu button (WebApp) instead of the command list.
    miniapp_public_url: str = ""
    # Telegram signs initData once per launch; replayed strings older than this
    # are rejected server-side (Telegram docs recommend checking auth_date).
    miniapp_init_data_ttl_s: int = 86_400
    # Local development only: a plain browser has no Telegram context, so the
    # transport may accept a declared dev user id instead of signed initData.
    # Fail-closed: requires the explicit flag, an explicit user id, and a
    # testnet REST base URL; the header is ignored in every other case.
    miniapp_dev_unsigned: bool = False
    miniapp_dev_user_id: int | None = None
    dry_run: bool = False  # True: commands are logged, no HTTP sent
    allow_mainnet: bool = False
    shadow_accounts_enabled: bool = False
    # Shadow load runs may disable synthetic market-triggered exits so a
    # live testnet price move cannot invalidate a deterministic copy cycle.
    shadow_auto_trigger_conditionals: bool = True
    # Explicit load-profile optimization. Empty keeps the authoritative
    # all-symbol follower snapshot used by normal production reconciliation.
    follower_snapshot_symbols: list[str] = []
    # v1 launch cap: maximum followers in the runtime registry, derived from
    # the measured weight envelope (scoped snapshot ≈ whitelist weight per
    # follower per sweep). Enforced fail-closed at register_follower.
    max_followers: int = 10
    operator_max_leverage: int | None = None
    max_total_risk_pct: Decimal = Decimal("0.10")

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

    @field_validator("miniapp_public_url")
    @classmethod
    def _validate_miniapp_public_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("CT_MINIAPP_PUBLIC_URL must be an https URL")
        return normalized

    @field_validator("miniapp_init_data_ttl_s")
    @classmethod
    def _validate_miniapp_ttl(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("CT_MINIAPP_INIT_DATA_TTL_S must be positive")
        return value

    @field_validator("builder_address")
    @classmethod
    def _validate_builder_address(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        from eth_utils import is_address, to_checksum_address

        if not is_address(normalized):
            raise ValueError("CT_BUILDER_ADDRESS must be a valid EVM address")
        return to_checksum_address(normalized)

    @field_validator("builder_name")
    @classmethod
    def _validate_builder_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("CT_BUILDER_NAME must not be empty")
        if len(normalized) > 64 or not normalized.isprintable():
            raise ValueError("CT_BUILDER_NAME must be printable and at most 64 characters")
        return normalized

    @field_validator("max_fee_rate")
    @classmethod
    def _validate_max_fee_rate(cls, value: str) -> str:
        try:
            rate = Decimal(str(value).strip())
        except Exception as exc:
            raise ValueError("CT_MAX_FEE_RATE must be a valid decimal") from exc
        if not rate.is_finite() or rate < Decimal(0) or rate > Decimal("0.001"):
            raise ValueError("CT_MAX_FEE_RATE must be in [0, 0.001]")
        return format(rate, "f")

    @field_validator("trading_chain_id")
    @classmethod
    def _validate_trading_chain_id(cls, value: int) -> int:
        if value not in {714, 1666}:
            raise ValueError("CT_TRADING_CHAIN_ID must be 714 (testnet) or 1666 (mainnet)")
        return value

    @field_validator("registration_chain_id")
    @classmethod
    def _validate_registration_chain_id(cls, value: int) -> int:
        if value != 56:
            raise ValueError("CT_REGISTRATION_CHAIN_ID must be 56 for the testnet builder flow")
        return value

    @field_validator("builder_agent_expiry_days")
    @classmethod
    def _validate_agent_expiry(cls, value: int) -> int:
        if not 1 <= value <= 365:
            raise ValueError("CT_BUILDER_AGENT_EXPIRY_DAYS must be between 1 and 365")
        return value

    @field_validator("builder_approval_ttl_s")
    @classmethod
    def _validate_approval_ttl(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("CT_BUILDER_APPROVAL_TTL_S must be between 30 and 3600 seconds")
        return value

    @model_validator(mode="after")
    def _validate_miniapp_dev_guard(self) -> "Settings":
        if self.miniapp_dev_unsigned and self.miniapp_dev_user_id is None:
            raise ValueError("CT_MINIAPP_DEV_UNSIGNED requires an explicit CT_MINIAPP_DEV_USER_ID")
        if self.operator_max_leverage is not None and self.operator_max_leverage <= 0:
            raise ValueError("CT_OPERATOR_MAX_LEVERAGE must be positive")
        if not Decimal(0) < self.max_total_risk_pct <= Decimal(1):
            raise ValueError("CT_MAX_TOTAL_RISK_PCT must be in (0, 1]")
        return self

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
            "max_followers": self.max_followers,
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

    @model_validator(mode="after")
    def _validate_network_profile(self) -> "Settings":
        _ = self.network_profile
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

    @property
    def is_mainnet(self) -> bool:
        """Whether both exchange endpoints are the exact official mainnet hosts."""
        rest_host = (urlparse(self.rest_base_url).hostname or "").lower().rstrip(".")
        ws_host = (urlparse(self.ws_user_base).hostname or "").lower().rstrip(".")
        return rest_host == "fapi.asterdex.com" and ws_host == "fstream.asterdex.com"

    @property
    def network_profile(self) -> AsterNetworkProfile:
        """Single-injection network identity for App/credentials/client/onboarding.

        Fail-closed: mixed combinations (mainnet hosts with the testnet trading
        chain and vice versa, or any unofficial host) raise ``ValueError`` here
        instead of composing a plausible-looking but wrong runtime.
        """
        return AsterNetworkProfile.resolve(
            self.rest_base_url,
            self.ws_user_base,
            self.trading_chain_id,
            self.registration_chain_id,
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

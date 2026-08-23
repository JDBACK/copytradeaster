"""Application settings loaded from environment (prefix CT_) and .env file."""

from decimal import Decimal
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CT_", env_file=".env", extra="ignore")

    rest_base_url: str = "https://fapi.asterdex-testnet.com"
    ws_user_base: str = "wss://fstream.asterdex-testnet.com/ws"
    db_path: Path = Path("data/copytrade.sqlite3")
    encryption_key: str  # hex, decodes to exactly 32 bytes; required
    log_level: str = "INFO"
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

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, v: str) -> str:
        try:
            raw = bytes.fromhex(v)
        except ValueError as exc:
            raise ValueError("CT_ENCRYPTION_KEY must be a hex string") from exc
        if len(raw) != 32:
            raise ValueError("CT_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return v

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
    def _validate_runtime_guards(self) -> "Settings":
        positive = {
            "reconcile_interval_s": self.reconcile_interval_s,
            "listenkey_keepalive_min": self.listenkey_keepalive_min,
            "weight_limit_per_min": self.weight_limit_per_min,
            "orders_limit_per_min": self.orders_limit_per_min,
            "orders_limit_per_10s": self.orders_limit_per_10s,
            "registration_chain_id": self.registration_chain_id,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"settings must be positive: {', '.join(invalid)}")
        if not Decimal(0) <= self.fee_buffer_pct < Decimal(1):
            raise ValueError("CT_FEE_BUFFER_PCT must be in [0, 1)")
        if not self.symbol_whitelist:
            raise ValueError("CT_SYMBOL_WHITELIST must not be empty")
        if not self.allow_mainnet and (
            "testnet" not in self.rest_base_url.lower()
            or "testnet" not in self.ws_user_base.lower()
        ):
            raise ValueError(
                "Stage 1 refuses non-testnet REST/WS URLs; "
                "CT_ALLOW_MAINNET is a future-stage override"
            )
        return self

    def encryption_key_bytes(self) -> bytes:
        return bytes.fromhex(self.encryption_key)

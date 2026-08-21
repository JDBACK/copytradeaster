"""Application settings loaded from environment (prefix CT_) and .env file."""

from decimal import Decimal
from pathlib import Path

from pydantic import field_validator
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

    def encryption_key_bytes(self) -> bytes:
        return bytes.fromhex(self.encryption_key)

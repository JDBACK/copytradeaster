"""Single source of truth for Aster network endpoints and chain ids.

Two official networks exist — TESTNET and MAINNET — and every exchange-facing
component (REST/WS base URLs, EIP-712 trading chain, registration chain,
onboarding guards, preflight) must derive from exactly one
:class:`AsterNetworkProfile`.  Mixed combinations (testnet host with mainnet
chain, mainnet host with testnet chain, any unofficial/dev host) fail closed
at construction: :meth:`AsterNetworkProfile.resolve` and the ``TESTNET`` /
``MAINNET`` constructors raise :exc:`ValueError` instead of building a
plausible-looking but wrong profile.

Official values (see docs/ASTER_API_RESEARCH_2026-08.md):
- TESTNET: ``https://fapi.asterdex-testnet.com``,
  ``wss://fstream.asterdex-testnet.com/ws``, trading chain 714, registration 56.
- MAINNET: ``https://fapi.asterdex.com``, ``wss://fstream.asterdex.com/ws``,
  trading chain 1666, registration 56.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

TESTNET_REST_HOST = "fapi.asterdex-testnet.com"
TESTNET_WS_HOST = "fstream.asterdex-testnet.com"
MAINNET_REST_HOST = "fapi.asterdex.com"
MAINNET_WS_HOST = "fstream.asterdex.com"

TRADING_CHAIN_ID_TESTNET = 714
TRADING_CHAIN_ID_MAINNET = 1666
REGISTRATION_CHAIN_ID = 56

_OFFICIAL_REST_HOSTS = frozenset({TESTNET_REST_HOST, MAINNET_REST_HOST})
_OFFICIAL_WS_HOSTS = frozenset({TESTNET_WS_HOST, MAINNET_WS_HOST})


def _host(value: str) -> str:
    return (urlsplit(value).hostname or "").lower().rstrip(".")


@dataclass(frozen=True, slots=True)
class AsterNetworkProfile:
    """Immutable Aster network identity: hosts plus both EIP-712 chain ids."""

    name: str
    rest_base_url: str
    ws_user_base: str
    trading_chain_id: int
    registration_chain_id: int = REGISTRATION_CHAIN_ID

    def __post_init__(self) -> None:
        name = self.name.upper()
        if name not in {"TESTNET", "MAINNET"}:
            raise ValueError(f"unknown Aster network {self.name!r}; want TESTNET or MAINNET")
        object.__setattr__(self, "name", name)
        rest_host = _host(self.rest_base_url)
        ws_host = _host(self.ws_user_base)
        rest_scheme = urlsplit(self.rest_base_url).scheme.lower()
        ws_scheme = urlsplit(self.ws_user_base).scheme.lower()
        if rest_scheme != "https" or rest_host not in _OFFICIAL_REST_HOSTS:
            raise ValueError(
                f"refusing unofficial Aster REST base {self.rest_base_url!r}: "
                "dev overrides and lookalike hosts are banned"
            )
        if ws_scheme != "wss" or ws_host not in _OFFICIAL_WS_HOSTS:
            raise ValueError(
                f"refusing unofficial Aster WS base {self.ws_user_base!r}: "
                "dev overrides and lookalike hosts are banned"
            )
        if self.registration_chain_id != REGISTRATION_CHAIN_ID:
            raise ValueError(
                f"refusing registration chain {self.registration_chain_id}: "
                f"must be {REGISTRATION_CHAIN_ID}"
            )
        if name == "TESTNET":
            if rest_host != TESTNET_REST_HOST or ws_host != TESTNET_WS_HOST:
                raise ValueError(
                    "refusing mixed TESTNET profile: hosts must be "
                    f"{TESTNET_REST_HOST}/{TESTNET_WS_HOST}"
                )
            if self.trading_chain_id != TRADING_CHAIN_ID_TESTNET:
                raise ValueError(
                    "refusing mixed TESTNET profile: trading chain must be "
                    f"{TRADING_CHAIN_ID_TESTNET}"
                )
        else:
            if rest_host != MAINNET_REST_HOST or ws_host != MAINNET_WS_HOST:
                raise ValueError(
                    "refusing mixed MAINNET profile: hosts must be "
                    f"{MAINNET_REST_HOST}/{MAINNET_WS_HOST}"
                )
            if self.trading_chain_id != TRADING_CHAIN_ID_MAINNET:
                raise ValueError(
                    "refusing mixed MAINNET profile: trading chain must be "
                    f"{TRADING_CHAIN_ID_MAINNET}"
                )

    @property
    def is_testnet(self) -> bool:
        return self.name == "TESTNET"

    @property
    def is_mainnet(self) -> bool:
        return self.name == "MAINNET"

    def require_mainnet(self) -> AsterNetworkProfile:
        if not self.is_mainnet:
            raise ValueError(f"mainnet-only operation on {self.name} profile")
        return self

    def require_testnet(self) -> AsterNetworkProfile:
        if not self.is_testnet:
            raise ValueError(f"testnet-only operation on {self.name} profile")
        return self

    @classmethod
    def testnet(cls) -> AsterNetworkProfile:
        return cls(
            name="TESTNET",
            rest_base_url=f"https://{TESTNET_REST_HOST}",
            ws_user_base=f"wss://{TESTNET_WS_HOST}/ws",
            trading_chain_id=TRADING_CHAIN_ID_TESTNET,
            registration_chain_id=REGISTRATION_CHAIN_ID,
        )

    @classmethod
    def mainnet(cls) -> AsterNetworkProfile:
        return cls(
            name="MAINNET",
            rest_base_url=f"https://{MAINNET_REST_HOST}",
            ws_user_base=f"wss://{MAINNET_WS_HOST}/ws",
            trading_chain_id=TRADING_CHAIN_ID_MAINNET,
            registration_chain_id=REGISTRATION_CHAIN_ID,
        )

    @classmethod
    def resolve(
        cls,
        rest_base_url: str,
        ws_user_base: str,
        trading_chain_id: int,
        registration_chain_id: int = REGISTRATION_CHAIN_ID,
    ) -> AsterNetworkProfile:
        """Build the profile implied by the REST host; anything mixed raises."""
        rest_host = _host(rest_base_url)
        if rest_host == TESTNET_REST_HOST:
            name = "TESTNET"
        elif rest_host == MAINNET_REST_HOST:
            name = "MAINNET"
        else:
            raise ValueError(
                f"refusing unofficial Aster REST base {rest_base_url!r}: "
                "dev overrides and lookalike hosts are banned"
            )
        return cls(
            name=name,
            rest_base_url=rest_base_url,
            ws_user_base=ws_user_base,
            trading_chain_id=trading_chain_id,
            registration_chain_id=registration_chain_id,
        )


def is_official_combination(base_url: str, trading_chain_id: int) -> bool:
    """Whether (REST host, trading chain) is exactly one official combo."""
    host = _host(base_url)
    return (host == TESTNET_REST_HOST and trading_chain_id == TRADING_CHAIN_ID_TESTNET) or (
        host == MAINNET_REST_HOST and trading_chain_id == TRADING_CHAIN_ID_MAINNET
    )


__all__ = [
    "MAINNET_REST_HOST",
    "MAINNET_WS_HOST",
    "REGISTRATION_CHAIN_ID",
    "TESTNET_REST_HOST",
    "TESTNET_WS_HOST",
    "TRADING_CHAIN_ID_MAINNET",
    "TRADING_CHAIN_ID_TESTNET",
    "AsterNetworkProfile",
    "is_official_combination",
]

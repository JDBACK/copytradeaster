"""Registration-context API: ApproveAgent / ApproveBuilder (chainId 56).

Signatures are produced by the account's MAIN wallet (KeyProvider.main_private_key);
business params + asterChain + user + nonce are signed with dynamically
inferred field types per the official aster-code-demo.
"""

from __future__ import annotations

import httpx

from copytradeaster.aster.ports import IdentityProvider, KeyProvider, NonceSource
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import (
    build_query_string,
    sign_registration,
    stringify_param_value,
)
from copytradeaster.config import Settings
from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)


async def post_signed_registration(
    http: httpx.AsyncClient,
    base_url: str,
    limiter: RateLimits,
    path: str,
    params: dict,
    weight: int = 50,
) -> dict:
    """Submit a registration request whose signature came from the user's wallet.

    The Builder flow never holds the user's main private key: the frontend
    wallet signs the EIP-712 payload and this helper only carries the prepared
    params plus ``signature``/``signatureChainId`` to the exchange.  Params
    order is preserved because the demo reference keeps business fields first
    and appends ``asterChain``, ``user``, ``nonce``, ``signature``,
    ``signatureChainId``.
    """
    if "signature" not in params or "signatureChainId" not in params:
        raise ManagementError("no-signature", "registration params miss user signature")
    await limiter.acquire_request(weight)
    # Booleans must serialize exactly like the official demo (URLSearchParams):
    # str(True) would produce "True", which the exchange does not parse as the
    # signed business value.
    qs = build_query_string(
        [(str(k), stringify_param_value(v)) for k, v in params.items()],
    )
    url = f"{base_url.rstrip('/')}{path}?{qs}"
    try:
        resp = await http.post(url, content=b"")
    except httpx.TransportError as exc:
        raise ManagementError(-1006, f"transport error: {type(exc).__name__}") from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise ManagementError(resp.status_code, resp.text[:200]) from exc
    if resp.status_code >= 400 or (
        isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0
    ):
        code = int(body.get("code", 0) or 0) if isinstance(body, dict) else 0
        raise ManagementError(code or resp.status_code, str(body.get("msg", ""))[:120])
    # Registration responses may contain exchange payloads and addresses;
    # only retain the action/status trace in application logs.
    log.info(
        "management user-signed request completed path=%s status=%s",
        path,
        resp.status_code,
    )
    return body


class ManagementError(Exception):
    def __init__(self, code: int | str, msg: str) -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class ManagementClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        limiter: RateLimits,
        identities: IdentityProvider,
        keys: KeyProvider,
        nonces: NonceSource,
        cfg: Settings,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._limiter = limiter
        self._identities = identities
        self._keys = keys
        self._nonces = nonces
        self._cfg = cfg

    async def _post_registration(
        self, account_id: str, path: str, params: dict, weight: int = 50
    ) -> dict:
        main_key = self._keys.main_private_key(account_id)
        if main_key is None:
            raise ManagementError("no-main-key", f"account {account_id} has no main private key")
        identity = self._identities.identity(account_id)
        params.setdefault("asterChain", "Testnet")
        params["user"] = identity.user
        if "nonce" not in params:
            params["nonce"] = await self._nonces.next_nonce(identity.signer)
        primary_type = "ApproveBuilder" if path.endswith("approveBuilder") else "ApproveAgent"
        signed, qs = sign_registration(
            params, main_key, primary_type=primary_type, chain_id=self._cfg.registration_chain_id
        )
        url = f"{self._base_url}{path}?{qs}"
        resp = await self._http.post(url, content=b"")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ManagementError(resp.status_code, resp.text[:200]) from exc
        # Registration responses may contain exchange payloads and addresses;
        # only retain the action/status trace in application logs.
        log.info(
            "management request completed path=%s account_id=%s status=%s",
            path,
            account_id,
            resp.status_code,
        )
        return body

    async def approve_agent(
        self,
        account_id: str,
        *,
        agent_name: str,
        expired_ms: int,
        can_perp_trade: bool = True,
        can_spot_trade: bool = False,
        can_withdraw: bool = False,
        ip_whitelist: str = "",
        with_builder: bool = True,
    ) -> dict:
        identity = self._identities.identity(account_id)
        params: dict = {
            "agentName": agent_name,
            "agentAddress": identity.signer,
            "ipWhitelist": ip_whitelist,
            "expired": expired_ms,
            "canSpotTrade": can_spot_trade,
            "canPerpTrade": can_perp_trade,
            "canWithdraw": can_withdraw,
        }
        if with_builder and self._cfg.builder_address:
            params["builder"] = self._cfg.builder_address
            params["maxFeeRate"] = self._cfg.max_fee_rate
            params["builderName"] = self._cfg.builder_name
        await self._limiter.acquire_request(50)
        return await self._post_registration(account_id, "/fapi/v3/approveAgent", params)

    async def approve_builder(self, account_id: str, max_fee_rate: str | None = None) -> dict:
        params: dict = {
            "maxFeeRate": max_fee_rate or self._cfg.max_fee_rate,
        }
        if self._cfg.builder_address:
            params["builder"] = self._cfg.builder_address
        await self._limiter.acquire_request(50)
        return await self._post_registration(account_id, "/fapi/v3/approveBuilder", params)

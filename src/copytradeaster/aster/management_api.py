"""Registration-context API: ApproveAgent / ApproveBuilder (chainId 56).

Signatures are produced by the account's MAIN wallet (KeyProvider.main_private_key);
business params + asterChain + user + nonce are signed with dynamically
inferred field types per the official aster-code-demo.
"""

from __future__ import annotations

import httpx

from copytradeaster.aster.ports import IdentityProvider, KeyProvider, NonceSource
from copytradeaster.aster.rate_limits import RateLimits
from copytradeaster.aster.signing import sign_registration
from copytradeaster.config import Settings
from copytradeaster.logging_utils import get_logger

log = get_logger(__name__)


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
        log.info("%s -> %s", path, body)
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

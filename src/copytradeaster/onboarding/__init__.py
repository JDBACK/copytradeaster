"""Application-level direct Aster V3 onboarding primitives.

The package intentionally has no Telegram or storage imports.  Transport
adapters provide a signed-read client, while the application composition root
provides the atomic store and runtime activation callback.
"""

from copytradeaster.onboarding.direct import (
    EPHEMERAL_ACCOUNT_ID,
    DirectOnboardingService,
    validate_agent_permissions,
)
from copytradeaster.onboarding.errors import OnboardingError, OnboardingErrorCode
from copytradeaster.onboarding.models import (
    AgentValidationSnapshot,
    DirectOnboardingConfig,
    DirectOnboardingRequest,
    DirectOnboardingResult,
    DirectPreflightResult,
    SignedReadBundle,
    mask_address,
)
from copytradeaster.onboarding.preflight import (
    CLOCK_DRIFT_MAX_MS,
    MainnetPreflightReport,
    PreflightCheck,
    PreflightCommands,
    PreflightExchangeReads,
    PreflightSettings,
    run_mainnet_preflight,
)
from copytradeaster.onboarding.providers import (
    ActivationCallback,
    EphemeralIdentityProvider,
    EphemeralKeyProvider,
    OnboardingStore,
    SignedReadClient,
)

__all__ = [
    "AgentValidationSnapshot",
    "ActivationCallback",
    "DirectOnboardingConfig",
    "DirectOnboardingRequest",
    "DirectOnboardingResult",
    "DirectOnboardingService",
    "DirectPreflightResult",
    "EPHEMERAL_ACCOUNT_ID",
    "EphemeralIdentityProvider",
    "EphemeralKeyProvider",
    "CLOCK_DRIFT_MAX_MS",
    "MainnetPreflightReport",
    "PreflightCheck",
    "PreflightCommands",
    "PreflightExchangeReads",
    "PreflightSettings",
    "run_mainnet_preflight",
    "OnboardingError",
    "OnboardingErrorCode",
    "OnboardingStore",
    "SignedReadBundle",
    "SignedReadClient",
    "mask_address",
    "validate_agent_permissions",
]

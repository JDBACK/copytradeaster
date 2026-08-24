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
    OnboardingCommit,
    SignedReadBundle,
    mask_address,
)
from copytradeaster.onboarding.providers import (
    ActivationCallback,
    EphemeralIdentityProvider,
    EphemeralKeyProvider,
    OnboardingClientFactory,
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
    "OnboardingCommit",
    "OnboardingClientFactory",
    "OnboardingError",
    "OnboardingErrorCode",
    "OnboardingStore",
    "SignedReadBundle",
    "SignedReadClient",
    "mask_address",
    "validate_agent_permissions",
]

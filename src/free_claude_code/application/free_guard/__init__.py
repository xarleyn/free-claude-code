"""FreeGuard: prove a model is genuinely free before spending a cent on it.

The package is deliberately I/O free. Storage lives in
``free_claude_code.runtime.free_guard_store``; HTTP stays in
``free_claude_code.providers``; the request path only ever calls
:class:`RoutingGuard`.
"""

from .account import account_binding_id, credential_fingerprint
from .adapters import (
    EligibilityAdapterRegistry,
    GenericUnknownAdapter,
    MachineReadablePriceAdapter,
    ProviderEligibilityAdapter,
    StaticRuleAdapter,
    classify_provider_error,
    known_provider_ids,
)
from .errors import GUARD_MESSAGE_PREFIX, GuardBlockedError, guard_message
from .models import (
    BLOCKED_ACCESS_STATUSES,
    BLOCKED_BILLING_CLASSES,
    COOLDOWN_ACCESS_STATUSES,
    AccessStatus,
    AccountConstraints,
    BillingClass,
    EligibilityDecision,
    EligibilityRecord,
    EvidenceSource,
    FreeGuardMode,
    NormalizedErrorCode,
    NormalizedProviderError,
    PricingEvidence,
    ReasonCode,
    parse_utc_text,
    utc_now,
    utc_text,
)
from .policy import (
    FreeGuardConfig,
    ModelEligibilityPolicy,
    RoutingGuard,
    record_from_evidence,
)

__all__ = [
    "BLOCKED_ACCESS_STATUSES",
    "BLOCKED_BILLING_CLASSES",
    "COOLDOWN_ACCESS_STATUSES",
    "GUARD_MESSAGE_PREFIX",
    "AccessStatus",
    "AccountConstraints",
    "BillingClass",
    "EligibilityAdapterRegistry",
    "EligibilityDecision",
    "EligibilityRecord",
    "EvidenceSource",
    "FreeGuardConfig",
    "FreeGuardMode",
    "GenericUnknownAdapter",
    "GuardBlockedError",
    "MachineReadablePriceAdapter",
    "ModelEligibilityPolicy",
    "NormalizedErrorCode",
    "NormalizedProviderError",
    "PricingEvidence",
    "ProviderEligibilityAdapter",
    "ReasonCode",
    "RoutingGuard",
    "StaticRuleAdapter",
    "account_binding_id",
    "classify_provider_error",
    "credential_fingerprint",
    "guard_message",
    "known_provider_ids",
    "parse_utc_text",
    "record_from_evidence",
    "utc_now",
    "utc_text",
]

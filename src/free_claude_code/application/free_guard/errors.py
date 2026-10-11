"""The failure raised when FreeGuard refuses to route a request.

It is a plain :class:`~free_claude_code.core.failures.ExecutionFailure` in the
``permission`` family, so every existing wire adapter already knows how to render it
(403 ``permission_error`` on both the Anthropic Messages and OpenAI Responses wires).
The added fields carry the machine-readable verdict, which the Admin UI, the analytics
store and the tests consume without parsing the human message.

The message always starts with ``free_guard_blocked:`` so a client can detect the
block without new protocol support. No payment URL and no upstream traffic are
involved.
"""

from free_claude_code.core.failures import ExecutionFailure, FailureKind

from .models import EligibilityDecision, ReasonCode

GUARD_MESSAGE_PREFIX = "free_guard_blocked"


class GuardBlockedError(ExecutionFailure):
    """One provider/model pair was denied by the free-only routing policy."""

    __slots__ = (
        "guard_access_status",
        "guard_account_binding_id",
        "guard_billing_class",
        "guard_provider_id",
        "guard_provider_model",
        "reason_code",
    )

    def __init__(self, decision: EligibilityDecision, message: str) -> None:
        super().__init__(
            kind=FailureKind.PERMISSION,
            status_code=403,
            message=f"{GUARD_MESSAGE_PREFIX}: {message}",
            retryable=False,
        )
        self.reason_code = decision.reason_code
        self.guard_provider_id = decision.provider_id
        self.guard_provider_model = decision.provider_model
        self.guard_account_binding_id = decision.account_binding_id
        self.guard_billing_class = decision.billing_class
        self.guard_access_status = decision.access_status


def guard_message(decision: EligibilityDecision) -> str:
    """Return the client-safe explanation for one denial."""

    reason = decision.reason_code
    target = f"{decision.provider_id}/{decision.provider_model}".strip("/")
    explanations: dict[ReasonCode, str] = {
        ReasonCode.EVIDENCE_MISSING: (
            f"FreeGuard has no evidence that {target!r} is free without a card, "
            "top-up or subscription, so it was not called."
        ),
        ReasonCode.EVIDENCE_STALE: (
            f"FreeGuard's evidence for {target!r} is out of date; refresh prices and "
            "eligibility, then retry."
        ),
        ReasonCode.ACCOUNT_BINDING_CHANGED: (
            f"The account credentials for {target!r} changed, so previous proof no "
            "longer applies."
        ),
        ReasonCode.BILLING_CLASS_BLOCKED: (
            f"{target!r} is not a confirmed free model "
            f"({decision.billing_class.value}), so FreeGuard did not call it."
        ),
        ReasonCode.REQUIRES_CARD: (
            f"{target!r} requires a payment card on this account."
        ),
        ReasonCode.REQUIRES_TOPUP: (
            f"{target!r} requires a purchased balance on this account."
        ),
        ReasonCode.REQUIRES_SUBSCRIPTION: (
            f"{target!r} requires a paid subscription on this account."
        ),
        ReasonCode.TRIAL_OR_PROMO_BALANCE: (
            f"{target!r} consumes trial or promotional credit, which strict free "
            "mode does not allow."
        ),
        ReasonCode.LOCAL_NOT_ALLOWED: (
            f"{target!r} is a local model and local models are disabled by policy."
        ),
        ReasonCode.ACCESS_BLOCKED: (
            f"{target!r} is unavailable ({decision.access_status.value})."
        ),
        ReasonCode.PAYMENT_LOCKED: (
            f"{target!r} requires a purchase on this account. Buying credit or "
            "switching to a verified free model is required; retrying will not help."
        ),
        ReasonCode.MANUAL_APPROVAL_REQUIRED: (
            f"{target!r} needs an explicit free-tier attestation in Admin."
        ),
        ReasonCode.NO_ELIGIBLE_FREE_MODELS: (
            "No verified free model is available for this request."
        ),
        ReasonCode.ELIGIBLE: f"{target!r} is eligible.",
        ReasonCode.MODE_OFF: "FreeGuard is off.",
        ReasonCode.CATALOG_ONLY: (
            "FreeGuard only filters the catalog in this mode; direct calls are not "
            "blocked."
        ),
    }
    return explanations.get(reason, decision.detail or f"{target!r} is not eligible.")

"""FreeGuard policy contracts: what may be routed, and what must not.

These tests pin the deny-by-default semantics that the whole feature rests on:

* a price of ``$0`` never proves "no card";
* a purchase lock inside an HTTP 429 is a purchase lock, not a rate limit;
* a genuine rate limit is a cooldown, never a reclassification to paid;
* unknown means blocked, always.
"""

from datetime import timedelta

import pytest

from free_claude_code.application.free_guard.models import (
    AccessStatus,
    AccountConstraints,
    BillingClass,
    EligibilityRecord,
    EvidenceSource,
    FreeGuardMode,
    NormalizedErrorCode,
    PricingEvidence,
    ReasonCode,
    utc_now,
)
from free_claude_code.application.free_guard.policy import (
    FreeGuardConfig,
    ModelEligibilityPolicy,
    RoutingGuard,
    free_guard_config,
    record_from_evidence,
)

_NOW = utc_now()
_STRICT = FreeGuardConfig(mode=FreeGuardMode.STRICT_FREE_ONLY)


def _record(
    *,
    billing_class: BillingClass = BillingClass.FREE_PERMANENT,
    access_status: AccessStatus = AccessStatus.VERIFIED_WORKING,
    requires_card: bool = False,
    requires_topup: bool = False,
    requires_subscription: bool = False,
    trial_or_promo: bool = False,
    expires_in_hours: int = 24,
) -> EligibilityRecord:
    return EligibilityRecord(
        provider_id="groq",
        upstream_model_id="llama-3.3-70b",
        account_binding_id="groq:abc123",
        billing_class=billing_class,
        access_status=access_status,
        requires_card=requires_card,
        requires_topup=requires_topup,
        requires_subscription=requires_subscription,
        uses_trial_or_promotional_balance=trial_or_promo,
        evidence_json="{}",
        evidence_source=EvidenceSource.DOCUMENTED_PUBLIC,
        evidence_checked_at_utc=_NOW,
        evidence_expires_at_utc=_NOW + timedelta(hours=expires_in_hours),
    )


@pytest.mark.parametrize(
    "billing_class",
    [
        BillingClass.PAID,
        BillingClass.SUBSCRIPTION,
        BillingClass.REQUIRES_CARD,
        BillingClass.REQUIRES_PURCHASE,
        BillingClass.FREE_TRIAL,
        BillingClass.PROMO_CREDIT,
        BillingClass.UNKNOWN,
    ],
)
def test_blocked_billing_classes_are_denied(billing_class: BillingClass) -> None:
    decision = ModelEligibilityPolicy(_STRICT).evaluate(
        _record(billing_class=billing_class), now=_NOW
    )
    assert decision.blocked
    assert decision.reason_code is ReasonCode.BILLING_CLASS_BLOCKED


def test_confirmed_free_tier_without_card_is_allowed() -> None:
    decision = ModelEligibilityPolicy(_STRICT).evaluate(_record(), now=_NOW)
    assert decision.allowed
    assert decision.reason_code is ReasonCode.ELIGIBLE
    assert decision.available_now


def test_local_model_allowed_only_when_policy_permits() -> None:
    record = _record(billing_class=BillingClass.LOCAL)
    allowed = ModelEligibilityPolicy(_STRICT).evaluate(record, now=_NOW)
    assert allowed.allowed

    denied = ModelEligibilityPolicy(
        FreeGuardConfig(mode=FreeGuardMode.STRICT_FREE_ONLY, allow_local_models=False)
    ).evaluate(record, now=_NOW)
    assert denied.blocked
    assert denied.reason_code is ReasonCode.LOCAL_NOT_ALLOWED


def test_unknown_evidence_is_denied_even_with_no_record() -> None:
    decision = ModelEligibilityPolicy(_STRICT).evaluate(None, now=_NOW)
    assert decision.blocked
    assert decision.reason_code is ReasonCode.EVIDENCE_MISSING
    assert decision.billing_class is BillingClass.UNKNOWN


def test_expired_evidence_is_denied() -> None:
    record = _record(expires_in_hours=-1)
    decision = ModelEligibilityPolicy(_STRICT).evaluate(record, now=_NOW)
    assert decision.blocked
    assert decision.reason_code is ReasonCode.EVIDENCE_STALE


def test_expired_evidence_may_be_kept_when_configured() -> None:
    record = _record(expires_in_hours=-1)
    config = FreeGuardConfig(
        mode=FreeGuardMode.STRICT_FREE_ONLY, block_when_evidence_stale=False
    )
    assert ModelEligibilityPolicy(config).evaluate(record, now=_NOW).allowed


def test_card_requirement_blocks_otherwise_free_model() -> None:
    decision = ModelEligibilityPolicy(_STRICT).evaluate(
        _record(requires_card=True), now=_NOW
    )
    assert decision.blocked
    assert decision.reason_code is ReasonCode.REQUIRES_CARD


def test_topup_and_subscription_requirements_block() -> None:
    policy = ModelEligibilityPolicy(_STRICT)
    assert (
        policy.evaluate(_record(requires_topup=True), now=_NOW).reason_code
        is ReasonCode.REQUIRES_TOPUP
    )
    assert (
        policy.evaluate(_record(requires_subscription=True), now=_NOW).reason_code
        is ReasonCode.REQUIRES_SUBSCRIPTION
    )


def test_trial_or_promotional_balance_blocks_unless_allowed() -> None:
    record = _record(trial_or_promo=True)
    assert (
        ModelEligibilityPolicy(_STRICT).evaluate(record, now=_NOW).reason_code
        is ReasonCode.TRIAL_OR_PROMO_BALANCE
    )
    permissive = FreeGuardConfig(mode=FreeGuardMode.STRICT_FREE_ONLY, allow_trials=True)
    assert ModelEligibilityPolicy(permissive).evaluate(record, now=_NOW).allowed


@pytest.mark.parametrize(
    "access_status",
    [
        AccessStatus.AUTH_REQUIRED,
        AccessStatus.PAYMENT_LOCKED,
        AccessStatus.REGION_BLOCKED,
        AccessStatus.INVALID_MODEL,
    ],
)
def test_blocking_access_statuses_are_denied(access_status: AccessStatus) -> None:
    decision = ModelEligibilityPolicy(_STRICT).evaluate(
        _record(access_status=access_status), now=_NOW
    )
    assert decision.blocked


def test_cooldown_keeps_model_free_but_unavailable() -> None:
    decision = ModelEligibilityPolicy(_STRICT).evaluate(
        _record(access_status=AccessStatus.TEMPORARILY_RATE_LIMITED), now=_NOW
    )
    assert decision.allowed
    assert not decision.available_now


def test_off_and_catalog_only_do_not_block() -> None:
    for mode in (FreeGuardMode.OFF, FreeGuardMode.CATALOG_FILTER_ONLY):
        decision = ModelEligibilityPolicy(FreeGuardConfig(mode=mode)).evaluate(
            _record(), now=_NOW
        )
        assert decision.allowed


def test_routing_guard_filters_and_preserves_order() -> None:
    guard = RoutingGuard(ModelEligibilityPolicy(_STRICT), lambda *_args: None)

    class Target:
        def __init__(self, provider_id: str, provider_model: str) -> None:
            self.provider_id = provider_id
            self.provider_model = provider_model
            self.provider_model_ref = f"{provider_id}/{provider_model}"

    targets = [
        Target("groq", "free-model"),
        Target("groq", "unknown-model"),
        Target("open_router", "another"),
    ]
    # A lookup that only knows about the first target.
    guard = RoutingGuard(
        ModelEligibilityPolicy(_STRICT),
        lambda provider_id, model, _binding: (
            _record() if (provider_id, model) == ("groq", "free-model") else None
        ),
    )
    allowed, blocked = guard.filter_targets(targets)
    assert [target.provider_model for target in allowed] == ["free-model"]
    assert len(blocked) == 2
    assert blocked[0][1].reason_code is ReasonCode.EVIDENCE_MISSING


def test_record_from_evidence_merges_account_constraints() -> None:
    pricing = PricingEvidence(
        provider_id="open_router",
        upstream_model_id="some/model",
        billing_class=BillingClass.PAID,
        source=EvidenceSource.MACHINE_READABLE,
        source_url="https://openrouter.ai/api/v1/models",
        machine_code="price>0",
    )
    constraints = AccountConstraints(requires_topup=True)
    record = record_from_evidence(
        provider_id="open_router",
        upstream_model_id="some/model",
        account_binding_id="open_router:deadbeef",
        pricing=pricing,
        account=constraints,
        ttl=timedelta(hours=24),
        now=_NOW,
    )
    assert record.billing_class is BillingClass.PAID
    assert record.requires_topup
    assert record.evidence_expires_at_utc == _NOW + timedelta(hours=24)


def test_zero_price_is_not_proof_of_free() -> None:
    """Spec §3.2 item 5: a ``:free`` suffix or ``$0`` price is only a candidate."""

    pricing = PricingEvidence(
        provider_id="open_router",
        upstream_model_id="some/model:free",
        billing_class=BillingClass.UNKNOWN,
        source=EvidenceSource.UNVERIFIED_LIST,
        machine_code="price==0",
    )
    # The source downgrades the claim to unknown, whatever the caller asked for.
    assert pricing.billing_class is BillingClass.UNKNOWN
    record = record_from_evidence(
        provider_id="open_router",
        upstream_model_id="some/model:free",
        account_binding_id="open_router:deadbeef",
        pricing=pricing,
        account=AccountConstraints(),
        ttl=timedelta(hours=24),
        now=_NOW,
    )
    decision = ModelEligibilityPolicy(_STRICT).evaluate(record, now=_NOW)
    assert decision.blocked
    assert decision.reason_code is ReasonCode.BILLING_CLASS_BLOCKED


def test_manual_attestation_is_the_only_route_to_free_permanent() -> None:
    constraints = AccountConstraints()
    pricing = PricingEvidence(
        provider_id="groq",
        upstream_model_id="llama-3.3-70b",
        billing_class=BillingClass.FREE_PERMANENT,
        source=EvidenceSource.MANUAL_ATTESTATION,
        source_url="https://console.groq.com/docs/models",
    )
    record = record_from_evidence(
        provider_id="groq",
        upstream_model_id="llama-3.3-70b",
        account_binding_id="groq:abc123",
        pricing=pricing,
        account=constraints,
        ttl=timedelta(hours=24),
        now=_NOW,
    )
    assert record.evidence_source is EvidenceSource.MANUAL_ATTESTATION
    assert ModelEligibilityPolicy(_STRICT).evaluate(record, now=_NOW).allowed


def test_settings_drive_the_policy() -> None:
    from free_claude_code.config.settings import Settings

    settings = Settings.model_validate(
        {"MODEL": "nvidia_nim/test", "FCC_FREE_GUARD_MODE": "strict_free_only"}
    )
    config = free_guard_config(settings)
    assert config.strict
    assert config.allow_local_models
    assert config.eligibility_ttl_hours == 24


def test_normalized_purchase_inside_429_is_not_a_rate_limit() -> None:
    from free_claude_code.application.free_guard.adapters import classify_provider_error

    normalized = classify_provider_error(
        429,
        {
            "error": {
                "code": "model_requires_purchase",
                "message": "locked on your account until you make a purchase",
            }
        },
    )
    assert normalized.code is NormalizedErrorCode.PAYMENT_REQUIRED
    assert normalized.billing_class is BillingClass.REQUIRES_PURCHASE
    assert normalized.access_status is AccessStatus.PAYMENT_LOCKED
    # No cooldown is offered: retrying cannot help.
    assert normalized.retry_after_seconds is None


def test_genuine_rate_limit_keeps_a_cooldown() -> None:
    from free_claude_code.application.free_guard.adapters import classify_provider_error

    normalized = classify_provider_error(
        429, {"error": {"code": "rate_limit_exceeded"}}
    )
    assert normalized.code is NormalizedErrorCode.RATE_LIMITED
    assert normalized.access_status is AccessStatus.TEMPORARILY_RATE_LIMITED
    assert normalized.billing_class is None

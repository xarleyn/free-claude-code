"""FreeGuard domain types: how "free" is judged, and what blocks a model.

The vocabulary is deliberately split in two, because the two questions are
independent:

* ``billing_class`` — *commercial* conditions: does reaching this model cost money,
  need a card, need a top-up, need a subscription, or run on trial/promo credit?
* ``access_status`` — *technical* availability right now: does the request work?

``pricing == 0`` proves neither. A model can be free today and paid tomorrow, and a
model can be free forever while being temporarily rate limited. ``eligible`` is
therefore a computed value, never a stored switch.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from free_claude_code.config.free_guard import FreeGuardMode

__all__ = [
    "AccessStatus",
    "AccountConstraints",
    "BillingClass",
    "EligibilityDecision",
    "EligibilityRecord",
    "EvidenceSource",
    "FreeGuardMode",
    "NormalizedErrorCode",
    "NormalizedProviderError",
    "PricingEvidence",
    "PricingSnapshot",
    "ReasonCode",
    "parse_utc_text",
    "utc_now",
    "utc_text",
]


def utc_now() -> datetime:
    """Return one timezone-aware UTC timestamp."""

    return datetime.now(UTC)


def utc_text(moment: datetime) -> str:
    """Serialize one UTC timestamp as RFC3339 with a ``Z`` suffix."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_text(value: str) -> datetime:
    """Parse an RFC3339 timestamp produced by :func:`utc_text`."""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class BillingClass(StrEnum):
    """Commercial conditions attached to one provider/model pair."""

    #: Confirmed free tier: no card, no top-up, no paid subscription, no trial or
    #: promotional balance consumed.
    FREE_PERMANENT = "free_permanent"
    #: Local inference: no external paid API is involved. This does not claim the
    #: hardware is free, only that no external bill can be produced.
    LOCAL = "local"
    FREE_TRIAL = "free_trial"
    PROMO_CREDIT = "promo_credit"
    REQUIRES_CARD = "requires_card"
    REQUIRES_PURCHASE = "requires_purchase"
    PAID = "paid"
    SUBSCRIPTION = "subscription"
    #: Nothing is known. Denied in strict mode.
    UNKNOWN = "unknown"


class AccessStatus(StrEnum):
    """Technical availability of one provider/model pair."""

    #: A recent real request succeeded. This is liveness, never proof of zero cost.
    VERIFIED_WORKING = "verified_working"
    NOT_PROBED = "not_probed"
    AUTH_REQUIRED = "auth_required"
    #: A genuine rate limit with a cooldown. The model may still be free.
    TEMPORARILY_RATE_LIMITED = "temporarily_rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    REGION_BLOCKED = "region_blocked"
    #: The account must buy something before this model can be used.
    PAYMENT_LOCKED = "payment_locked"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_MODEL = "invalid_model"
    UNKNOWN = "unknown"


class ReasonCode(StrEnum):
    """Why a policy decision was made. Stable, machine-readable, client-safe."""

    ELIGIBLE = "eligible"
    MODE_OFF = "guard_mode_off"
    CATALOG_ONLY = "catalog_filter_only"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_STALE = "evidence_stale"
    ACCOUNT_BINDING_CHANGED = "account_binding_changed"
    BILLING_CLASS_BLOCKED = "billing_class_blocked"
    REQUIRES_CARD = "requires_card"
    REQUIRES_TOPUP = "requires_topup"
    REQUIRES_SUBSCRIPTION = "requires_subscription"
    TRIAL_OR_PROMO_BALANCE = "trial_or_promo_balance"
    LOCAL_NOT_ALLOWED = "local_models_not_allowed"
    ACCESS_BLOCKED = "access_status_blocked"
    PAYMENT_LOCKED = "payment_locked"
    MANUAL_APPROVAL_REQUIRED = "manual_approval_required"
    NO_ELIGIBLE_FREE_MODELS = "no_eligible_free_models"


#: Billing classes that can never be routed in strict mode.
BLOCKED_BILLING_CLASSES = frozenset(
    {
        BillingClass.FREE_TRIAL,
        BillingClass.PROMO_CREDIT,
        BillingClass.REQUIRES_CARD,
        BillingClass.REQUIRES_PURCHASE,
        BillingClass.PAID,
        BillingClass.SUBSCRIPTION,
        BillingClass.UNKNOWN,
    }
)

#: Access states that mean "do not even try right now".
BLOCKED_ACCESS_STATUSES = frozenset(
    {
        AccessStatus.AUTH_REQUIRED,
        AccessStatus.PAYMENT_LOCKED,
        AccessStatus.REGION_BLOCKED,
        AccessStatus.INVALID_MODEL,
    }
)

#: Access states that mean "free, but wait a while". Not blocked, only cooldown.
COOLDOWN_ACCESS_STATUSES = frozenset(
    {
        AccessStatus.TEMPORARILY_RATE_LIMITED,
        AccessStatus.QUOTA_EXHAUSTED,
        AccessStatus.PROVIDER_UNAVAILABLE,
    }
)


class EvidenceSource(StrEnum):
    """How a piece of eligibility evidence was obtained (priority ordered)."""

    MACHINE_READABLE = "machine_readable"
    DOCUMENTED_PUBLIC = "documented_public"
    ACCOUNT_SIGNAL = "account_signal"
    PROBE = "probe"
    MANUAL_ATTESTATION = "manual_attestation"
    UNVERIFIED_LIST = "unverified_list"


class NormalizedErrorCode(StrEnum):
    """Provider-independent failure categories (spec §4.3)."""

    PAYMENT_REQUIRED = "payment_required"
    TRIAL_EXHAUSTED = "trial_exhausted"
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED_FREE_TIER = "quota_exhausted_free_tier"
    REGION_BLOCKED = "region_blocked"
    MODEL_NOT_FOUND = "model_not_found"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    GUARD_BLOCKED = "guard_blocked"
    CANCELED = "canceled"
    STREAM_ERROR = "stream_error"


@dataclass(frozen=True, slots=True)
class PricingEvidence:
    """One documented observation of a model's commercial conditions."""

    provider_id: str
    upstream_model_id: str
    billing_class: BillingClass
    source: EvidenceSource
    source_url: str | None = None
    observed_at_utc: datetime | None = None
    requires_card: bool = False
    requires_topup: bool = False
    requires_subscription: bool = False
    uses_trial_or_promotional_balance: bool = False
    #: Machine code observed alongside the price, e.g. ``model_requires_purchase``.
    machine_code: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.observed_at_utc is None:
            object.__setattr__(self, "observed_at_utc", utc_now())
        if self.source is EvidenceSource.UNVERIFIED_LIST:
            # An unverified list is a candidate, never proof. Never let it claim more.
            object.__setattr__(self, "billing_class", BillingClass.UNKNOWN)


@dataclass(frozen=True, slots=True)
class AccountConstraints:
    """Account-specific conditions that no price list can reveal."""

    requires_card: bool = False
    requires_topup: bool = False
    requires_subscription: bool = False
    uses_trial_or_promotional_balance: bool = False
    access_status: AccessStatus = AccessStatus.UNKNOWN
    source: EvidenceSource = EvidenceSource.DOCUMENTED_PUBLIC
    source_url: str | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedProviderError:
    """A provider failure restated without depending on its HTTP status."""

    code: NormalizedErrorCode
    #: Populated only for genuine rate limits; never for purchase failures.
    retry_after_seconds: int | None = None
    billing_class: BillingClass | None = None
    access_status: AccessStatus | None = None
    #: A short, safe classification note. Raw provider blobs may hold secrets and
    #: are never stored or forwarded.
    summary: str = ""


@dataclass(frozen=True, slots=True)
class EligibilityRecord:
    """Durable eligibility state for one canonical provider/model/account triple."""

    provider_id: str
    upstream_model_id: str
    account_binding_id: str
    billing_class: BillingClass = BillingClass.UNKNOWN
    access_status: AccessStatus = AccessStatus.UNKNOWN
    requires_card: bool = False
    requires_topup: bool = False
    requires_subscription: bool = False
    uses_trial_or_promotional_balance: bool = False
    evidence_json: str = "{}"
    evidence_source: EvidenceSource = EvidenceSource.UNVERIFIED_LIST
    evidence_checked_at_utc: datetime | None = None
    evidence_expires_at_utc: datetime | None = None
    last_probe_at_utc: datetime | None = None
    blocked_reason_code: ReasonCode | None = None
    #: True only for a human attestation that a payment is genuinely not required.
    manually_approved: bool = False
    manual_attestation_url: str | None = None
    manual_attestation_by: str | None = None
    manual_attestation_at_utc: datetime | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider_id, self.upstream_model_id, self.account_binding_id)


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    """A price observation kept for cost estimation."""

    id: int | None
    provider_id: str
    upstream_model_id: str
    price_info_json: str
    currency: str
    source_url: str | None
    fetched_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """One policy verdict for one canonical provider/model/account triple."""

    allowed: bool
    reason_code: ReasonCode
    provider_id: str
    provider_model: str
    account_binding_id: str
    billing_class: BillingClass
    access_status: AccessStatus
    checked_at_utc: datetime
    evidence_source: EvidenceSource | None = None
    evidence_expires_at_utc: datetime | None = None
    #: Seconds a caller should wait before re-evaluating a cooldown target.
    retry_after_seconds: int | None = None
    #: Present when the model is free but currently cooling down.
    available_now: bool = True
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def as_log_fields(self) -> dict[str, object]:
        """Return non-sensitive fields for logs, traces and the audit trail."""

        return {
            "provider_id": self.provider_id,
            "upstream_model_id": self.provider_model,
            "account_binding_id": self.account_binding_id,
            "allowed": self.allowed,
            "reason_code": self.reason_code.value,
            "billing_class": self.billing_class.value,
            "access_status": self.access_status.value,
            "available_now": self.available_now,
        }

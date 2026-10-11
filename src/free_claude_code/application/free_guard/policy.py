"""Deny-by-default eligibility policy and the routing guard built on it.

Nothing in this module performs I/O. The policy is a pure function of one
:class:`EligibilityRecord`, the active configuration and a clock, so it can be tested
exhaustively and called from the request path without blocking.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from free_claude_code.config.free_guard import FreeGuardMode
from free_claude_code.core.trace import trace_event

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
    PricingEvidence,
    ReasonCode,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class FreeGuardConfig:
    """Effective FreeGuard configuration for one settings generation."""

    mode: FreeGuardMode = FreeGuardMode.OFF
    allow_local_models: bool = True
    require_no_card: bool = True
    require_no_topup: bool = True
    require_no_subscription: bool = True
    allow_trials: bool = False
    allow_promotional_credits: bool = False
    allow_manual_approval: bool = True
    eligibility_ttl_hours: int = 24
    block_when_evidence_stale: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode is not FreeGuardMode.OFF

    @property
    def strict(self) -> bool:
        return self.mode is FreeGuardMode.STRICT_FREE_ONLY

    @property
    def ttl(self) -> timedelta:
        return timedelta(hours=max(1, int(self.eligibility_ttl_hours)))


class ModelEligibilityPolicy:
    """Evaluate one canonical provider/model pair against the free-only contract."""

    def __init__(self, config: FreeGuardConfig) -> None:
        self._config = config

    @property
    def config(self) -> FreeGuardConfig:
        return self._config

    def evaluate(
        self,
        record: EligibilityRecord | None,
        *,
        now: datetime | None = None,
    ) -> EligibilityDecision:
        """Return the verdict for ``record``; ``None`` means nothing is known."""

        moment = _as_datetime(now)
        if record is None:
            return _deny(
                record=None,
                moment=moment,
                reason=ReasonCode.EVIDENCE_MISSING,
                detail="No eligibility evidence exists for this provider/model.",
            )
        if not self._config.strict:
            return EligibilityDecision(
                allowed=True,
                reason_code=(
                    ReasonCode.MODE_OFF
                    if not self._config.enabled
                    else ReasonCode.CATALOG_ONLY
                ),
                provider_id=record.provider_id,
                provider_model=record.upstream_model_id,
                account_binding_id=record.account_binding_id,
                billing_class=record.billing_class,
                access_status=record.access_status,
                checked_at_utc=moment,
                evidence_source=record.evidence_source,
                evidence_expires_at_utc=record.evidence_expires_at_utc,
                available_now=record.access_status not in COOLDOWN_ACCESS_STATUSES,
                detail=(
                    "FreeGuard does not block calls in this mode; the catalog may "
                    "still hide unconfirmed models."
                ),
            )

        if self._is_stale(record, moment):
            return _deny(
                record=record,
                moment=moment,
                reason=ReasonCode.EVIDENCE_STALE,
                detail=(
                    "Eligibility evidence expired at "
                    f"{_text(record.evidence_expires_at_utc)}; refresh prices and "
                    "eligibility before routing here."
                ),
            )

        if record.billing_class in BLOCKED_BILLING_CLASSES:
            return _deny(
                record=record,
                moment=moment,
                reason=ReasonCode.BILLING_CLASS_BLOCKED,
                detail=(
                    f"Billing class {record.billing_class.value!r} is not a "
                    "confirmed no-card, no-top-up, no-subscription free tier."
                ),
            )

        if (
            record.billing_class is BillingClass.LOCAL
            and not self._config.allow_local_models
        ):
            return _deny(
                record=record,
                moment=moment,
                reason=ReasonCode.LOCAL_NOT_ALLOWED,
                detail="Local models are disabled by policy.",
            )

        if self._config.require_no_card and record.requires_card:
            return _deny(
                record=record,
                moment=moment,
                reason=ReasonCode.REQUIRES_CARD,
                detail="This provider/model requires a payment card on the account.",
            )
        if self._config.require_no_topup and record.requires_topup:
            return _deny(
                record=record,
                moment=moment,
                reason=ReasonCode.REQUIRES_TOPUP,
                detail="This provider/model requires a purchased balance or top-up.",
            )
        if self._config.require_no_subscription and record.requires_subscription:
            return _deny(
                record=record,
                moment=moment,
                reason=ReasonCode.REQUIRES_SUBSCRIPTION,
                detail="This provider/model requires a paid subscription.",
            )
        if record.uses_trial_or_promotional_balance and not (
            self._config.allow_trials or self._config.allow_promotional_credits
        ):
            return _deny(
                record=record,
                moment=moment,
                reason=ReasonCode.TRIAL_OR_PROMO_BALANCE,
                detail=(
                    "This provider/model consumes trial or promotional credit, which "
                    "is not the same as a permanent free tier."
                ),
            )

        if record.access_status in BLOCKED_ACCESS_STATUSES:
            reason = (
                ReasonCode.PAYMENT_LOCKED
                if record.access_status is AccessStatus.PAYMENT_LOCKED
                else ReasonCode.ACCESS_BLOCKED
            )
            return _deny(
                record=record,
                moment=moment,
                reason=reason,
                detail=(
                    f"Access status {record.access_status.value!r} prevents any call "
                    "to this provider/model."
                ),
            )

        cooling = record.access_status in COOLDOWN_ACCESS_STATUSES
        return EligibilityDecision(
            allowed=True,
            reason_code=ReasonCode.ELIGIBLE,
            provider_id=record.provider_id,
            provider_model=record.upstream_model_id,
            account_binding_id=record.account_binding_id,
            billing_class=record.billing_class,
            access_status=record.access_status,
            checked_at_utc=moment,
            evidence_source=record.evidence_source,
            evidence_expires_at_utc=record.evidence_expires_at_utc,
            available_now=not cooling,
            detail=(
                "Confirmed free tier without card, top-up, subscription or trial "
                "balance."
                if not cooling
                else "Eligible free tier, but the provider is cooling this model down."
            ),
        )

    def _is_stale(self, record: EligibilityRecord, moment: datetime) -> bool:
        if not self._config.block_when_evidence_stale:
            return False
        expires = record.evidence_expires_at_utc
        if expires is None:
            return True
        return _as_datetime(expires) <= moment


def _deny(
    *,
    record: EligibilityRecord | None,
    moment: datetime,
    reason: ReasonCode,
    detail: str,
) -> EligibilityDecision:
    return EligibilityDecision(
        allowed=False,
        reason_code=reason,
        provider_id=record.provider_id if record is not None else "",
        provider_model=record.upstream_model_id if record is not None else "",
        account_binding_id=(record.account_binding_id if record is not None else ""),
        billing_class=record.billing_class
        if record is not None
        else BillingClass.UNKNOWN,
        access_status=(
            record.access_status if record is not None else AccessStatus.UNKNOWN
        ),
        checked_at_utc=moment,
        evidence_source=record.evidence_source if record is not None else None,
        evidence_expires_at_utc=(
            record.evidence_expires_at_utc if record is not None else None
        ),
        available_now=False,
        detail=detail,
    )


EvidenceLookup = Callable[[str, str, str], EligibilityRecord | None]


class RoutingGuard:
    """Apply one eligibility policy to route construction and to every call site.

    Two independent lines of defence:

    1. :meth:`filter_targets` keeps a resolved route from advertising a blocked
       primary or fallback in the first place.
    2. :meth:`authorize` re-checks the exact canonical pair immediately before the
       outbound HTTP request, so direct model IDs, aliases, retries, fallbacks and
       configuration changes all pass the same gate.
    """

    def __init__(
        self,
        policy: ModelEligibilityPolicy,
        lookup: EvidenceLookup,
        *,
        account_binding_id: Callable[[str], str] | None = None,
    ) -> None:
        self._policy = policy
        self._lookup = lookup
        self._account_binding_id = account_binding_id or (lambda _provider_id: "")

    @property
    def policy(self) -> ModelEligibilityPolicy:
        return self._policy

    def authorize(
        self,
        *,
        provider_id: str,
        provider_model: str,
        account_binding_id: str | None = None,
        now: datetime | None = None,
    ) -> EligibilityDecision:
        """Return the verdict for one canonical pair, whether or not it is stored."""

        binding = (
            account_binding_id
            if account_binding_id is not None
            else self._account_binding_id(provider_id)
        )
        record = self._lookup(provider_id, provider_model, binding)
        return self._policy.evaluate(record, now=now)

    def filter_targets[T](
        self,
        targets: Iterable[T],
        *,
        account_binding_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[list[T], list[tuple[T, EligibilityDecision]]]:
        """Split ``targets`` into allowed ones and blocked ones with their reasons.

        The relative order of the allowed targets is preserved, so fallback
        semantics are unchanged for targets that survive.
        """

        allowed: list[T] = []
        blocked: list[tuple[T, EligibilityDecision]] = []
        for target in targets:
            # Targets are routing candidates; the guard reads their canonical pair.
            provider_id = cast(str, getattr(target, "provider_id", ""))
            provider_model = cast(str, getattr(target, "provider_model", ""))
            decision = self.authorize(
                provider_id=provider_id,
                provider_model=provider_model,
                account_binding_id=account_binding_id,
                now=now,
            )
            if decision.allowed:
                allowed.append(target)
            else:
                blocked.append((target, decision))
        return allowed, blocked

    def trace_blocked_route(
        self,
        *,
        request_id: str | None,
        blocked: Sequence[tuple[object, EligibilityDecision]],
        wire_api: str | None = None,
    ) -> None:
        """Emit one trace event describing the targets a guard removed."""

        if not blocked:
            return
        fields: dict[str, object] = {
            "stage": "routing",
            "event": "free_claude_code.free_guard.route_filtered",
            "source": "application",
            "blocked": [
                {
                    "provider_model_ref": (
                        f"{decision.provider_id}/{decision.provider_model}"
                    ),
                    "reason_code": decision.reason_code.value,
                    "billing_class": decision.billing_class.value,
                    "access_status": decision.access_status.value,
                }
                for _target, decision in blocked
            ],
        }
        if request_id is not None:
            fields["request_id"] = request_id
        if wire_api is not None:
            fields["wire_api"] = wire_api
        trace_event(**fields)


def record_from_evidence(
    *,
    provider_id: str,
    upstream_model_id: str,
    account_binding_id: str,
    pricing: PricingEvidence,
    account: AccountConstraints,
    ttl: timedelta,
    now: datetime | None = None,
) -> EligibilityRecord:
    """Merge machine-readable price evidence with account constraints."""

    moment = _as_datetime(now)
    return EligibilityRecord(
        provider_id=provider_id,
        upstream_model_id=upstream_model_id,
        account_binding_id=account_binding_id,
        billing_class=pricing.billing_class,
        access_status=account.access_status,
        requires_card=pricing.requires_card or account.requires_card,
        requires_topup=pricing.requires_topup or account.requires_topup,
        requires_subscription=(
            pricing.requires_subscription or account.requires_subscription
        ),
        uses_trial_or_promotional_balance=(
            pricing.uses_trial_or_promotional_balance
            or account.uses_trial_or_promotional_balance
        ),
        evidence_json=_evidence_json(pricing, account),
        evidence_source=(
            pricing.source
            if pricing.source is not EvidenceSource.UNVERIFIED_LIST
            else account.source
        ),
        evidence_checked_at_utc=moment,
        evidence_expires_at_utc=moment + ttl,
    )


def _evidence_json(pricing: PricingEvidence, account: AccountConstraints) -> str:
    """Serialize only non-sensitive evidence metadata."""

    import json

    return json.dumps(
        {
            "pricing": {
                "billing_class": pricing.billing_class.value,
                "source": pricing.source.value,
                "source_url": pricing.source_url,
                "observed_at_utc": (
                    pricing.observed_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if pricing.observed_at_utc is not None
                    else None
                ),
                "machine_code": pricing.machine_code,
                "note": pricing.note,
            },
            "account": {
                "access_status": account.access_status.value,
                "source": account.source.value,
                "source_url": account.source_url,
                "note": account.note,
            },
        },
        sort_keys=True,
    )


def _as_datetime(value: datetime | None) -> datetime:
    if value is None:
        return utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _text(moment: datetime | None) -> str:
    if moment is None:
        return "unknown"
    return _as_datetime(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def free_guard_config(settings: object) -> FreeGuardConfig:
    """Build the effective policy configuration from one settings generation."""

    from free_claude_code.config.free_guard import FreeGuardMode as _Mode

    mode = getattr(settings, "free_guard_mode", _Mode.OFF)
    if isinstance(mode, str):
        from free_claude_code.config.free_guard import parse_free_guard_mode

        mode = parse_free_guard_mode(mode)
    local = bool(getattr(settings, "free_guard_allow_local_models", True))
    return FreeGuardConfig(
        mode=mode,
        allow_local_models=local,
        require_no_card=bool(getattr(settings, "free_guard_require_no_card", True)),
        require_no_topup=bool(getattr(settings, "free_guard_require_no_topup", True)),
        require_no_subscription=bool(
            getattr(settings, "free_guard_require_no_subscription", True)
        ),
        allow_trials=bool(getattr(settings, "free_guard_allow_trials", False)),
        allow_promotional_credits=bool(
            getattr(settings, "free_guard_allow_promotional_credits", False)
        ),
        allow_manual_approval=bool(
            getattr(settings, "free_guard_allow_manual_approval", True)
        ),
        eligibility_ttl_hours=int(
            getattr(settings, "free_guard_eligibility_ttl_hours", 24)
        ),
        block_when_evidence_stale=bool(
            getattr(settings, "free_guard_block_when_evidence_stale", True)
        ),
    )

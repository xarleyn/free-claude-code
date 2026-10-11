"""Provider eligibility adapters.

An adapter answers three questions for one provider:

1. ``discover_pricing`` — what does the provider *publish* about each model's price and
   free tier?
2. ``discover_account_constraints`` — what does *this account* require?
3. ``parse_provider_error`` — what does one failure actually mean?

The critical rule, taken from spec §3.2: a price of ``$0`` is **not** evidence that a
model is free without a card. Machines that merely list prices, or whose only signal is
a ``:free`` suffix, can at best prove that a model is *paid*. They can never prove that
it is free. Adapters therefore return ``BillingClass.UNKNOWN`` for anything they cannot
positively confirm, and ``BillingClass.UNKNOWN`` is denied in strict mode.

No adapter here performs a paid probe. ``discover_pricing`` reads only machine-readable
price/free-tier endpoints on the provider's own approved host, or applies an explicit
documented static rule carrying a URL and a review date.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from free_claude_code.config.provider_catalog import PROVIDER_CATALOG

from .models import (
    AccessStatus,
    AccountConstraints,
    BillingClass,
    EvidenceSource,
    NormalizedErrorCode,
    NormalizedProviderError,
    PricingEvidence,
    ReasonCode,
)

#: Machine codes that mean "buy something first", regardless of the HTTP status.
_PURCHASE_CODES = frozenset(
    {
        "model_requires_purchase",
        "requires_purchase",
        "purchase_required",
        "payment_required",
        "insufficient_credits_for_model",
        "model_locked_until_purchase",
        "subscription_required",
    }
)
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "tokens_per_minute_exceeded"})
_AUTH_CODES = frozenset({"invalid_api_key", "unauthorized", "authentication_error"})
_REGION_CODES = frozenset({"region_not_supported", "forbidden_region", "geo_blocked"})
_MODEL_CODES = frozenset({"model_not_found", "unknown_model", "invalid_model"})


@dataclass(frozen=True, slots=True)
class _StaticRule:
    """One documented, reviewable statement about a provider's commercial terms."""

    provider_id: str
    source_url: str
    reviewed_on: str
    summary: str
    billing_class: BillingClass = BillingClass.UNKNOWN
    requires_card: bool = False
    requires_topup: bool = False
    requires_subscription: bool = False
    uses_trial_or_promotional_balance: bool = False
    #: Provider-wide conditions, applied to every model of that provider.
    account_wide: bool = True


#: Documented static rules. Each entry names the public page that justifies it and
#: the date it was last reviewed. Everything not listed here stays ``unknown``.
#:
#: Groq, OpenRouter, OpenCode Zen and Experiential Labs are the providers the
#: specification names for the first adapter set. Their terms change, so these
#: statements are deliberately conservative: where a public page cannot *prove* a
#: no-card free tier for a specific model, the record stays ``unknown`` and the model
#: stays blocked in strict mode until someone attests to it in Admin.
_STATIC_RULES: tuple[_StaticRule, ...] = (
    _StaticRule(
        provider_id="groq",
        source_url="https://console.groq.com/docs/rate-limits",
        reviewed_on="2026-10-11",
        summary=(
            "Groq publishes a free developer tier, but per-model availability and "
            "quotas are account specific and are not published as a machine-readable "
            "free/no-card flag. Unattested models stay unknown."
        ),
    ),
    _StaticRule(
        provider_id="open_router",
        source_url="https://openrouter.ai/docs/api-reference/list-available-models",
        reviewed_on="2026-10-11",
        summary=(
            "OpenRouter publishes per-model pricing, which proves which models are "
            "paid. A zero price does not prove that no card or credit is needed, so "
            "zero-priced models stay unknown until attested."
        ),
    ),
    _StaticRule(
        provider_id="opencode_zen",
        source_url="https://opencode.ai/docs/zen/",
        reviewed_on="2026-10-11",
        summary=(
            "OpenCode Zen bills per token against an opencode.ai account; no "
            "machine-readable no-card free tier is published."
        ),
        requires_card=True,
    ),
    _StaticRule(
        provider_id="experiential",
        source_url="https://docs.experiential.ai/",
        reviewed_on="2026-10-11",
        summary=(
            "Experiential Labs gates individual models behind account purchases; the "
            "API reports model_requires_purchase when a model is locked."
        ),
    ),
    _StaticRule(
        provider_id="opencode_go",
        source_url="https://opencode.ai/docs/go/",
        reviewed_on="2026-10-11",
        summary=("OpenCode Go is a paid subscription plan; not a free tier."),
        billing_class=BillingClass.SUBSCRIPTION,
        requires_subscription=True,
    ),
    _StaticRule(
        provider_id="lmstudio",
        source_url="https://lmstudio.ai/docs",
        reviewed_on="2026-10-11",
        summary="LM Studio runs inference on the local machine; no external bill.",
        billing_class=BillingClass.LOCAL,
    ),
    _StaticRule(
        provider_id="llamacpp",
        source_url="https://github.com/ggml-org/llama.cpp/tree/master/tools/server",
        reviewed_on="2026-10-11",
        summary="llama.cpp runs inference on the local machine; no external bill.",
        billing_class=BillingClass.LOCAL,
    ),
    _StaticRule(
        provider_id="ollama",
        source_url="https://ollama.com/",
        reviewed_on="2026-10-11",
        summary="Ollama runs inference on the local machine; no external bill.",
        billing_class=BillingClass.LOCAL,
    ),
)

_RULES_BY_PROVIDER: dict[str, _StaticRule] = {
    rule.provider_id: rule for rule in _STATIC_RULES
}


def known_provider_ids() -> tuple[str, ...]:
    """Provider ids that have a dedicated adapter rule or fetch strategy."""

    return tuple(_RULES_BY_PROVIDER)


@runtime_checkable
class ProviderEligibilityAdapter(Protocol):
    """The eligibility capability one provider must expose."""

    provider_id: str

    def discover_pricing(self, provider_id: str) -> list[PricingEvidence]: ...

    def discover_account_constraints(self, provider_id: str) -> AccountConstraints: ...

    def parse_provider_error(
        self, status: int, payload: object
    ) -> NormalizedProviderError: ...


class StaticRuleAdapter:
    """Documented static rules for a provider with no machine-readable prices.

    This is the specification's "explicit documented static rules + TTL + manual
    confirmation" path: it never guesses, and it never turns an unproven model into a
    free one.
    """

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    def discover_pricing(self, provider_id: str) -> list[PricingEvidence]:
        """Return the documented statement, or an explicit ``unknown``."""

        rule = _RULES_BY_PROVIDER.get(provider_id)
        if rule is None:
            return []
        return [
            PricingEvidence(
                provider_id=provider_id,
                upstream_model_id="*",
                billing_class=rule.billing_class,
                source=EvidenceSource.DOCUMENTED_PUBLIC,
                source_url=rule.source_url,
                requires_card=rule.requires_card,
                requires_topup=rule.requires_topup,
                requires_subscription=rule.requires_subscription,
                uses_trial_or_promotional_balance=rule.uses_trial_or_promotional_balance,
                note=f"{rule.summary} (reviewed {rule.reviewed_on})",
            )
        ]

    def discover_account_constraints(self, provider_id: str) -> AccountConstraints:
        rule = _RULES_BY_PROVIDER.get(provider_id)
        if rule is None:
            return AccountConstraints(
                access_status=AccessStatus.UNKNOWN,
                note="No documented rules; treat as unknown.",
            )
        status = AccessStatus.NOT_PROBED
        return AccountConstraints(
            access_status=status,
            source=EvidenceSource.DOCUMENTED_PUBLIC,
            source_url=rule.source_url,
            note=rule.summary,
        )

    def parse_provider_error(
        self, status: int, payload: object
    ) -> NormalizedProviderError:
        return classify_provider_error(status, payload)


class MachineReadablePriceAdapter(StaticRuleAdapter):
    """Merge a machine-readable price list with documented account conditions.

    The price list is authoritative for *paid* models: a positive price is proof of
    cost. It is never proof of *free*: a zero price only means nobody published a
    number, which leaves the model ``unknown`` and therefore blocked.
    """

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id)
        #: ``model id -> (price, currency)`` extracted from the provider's endpoint.
        self.prices: Mapping[str, tuple[str, str]] = {}

    def load_prices(self, prices: Mapping[str, tuple[str, str]]) -> None:
        """Record the latest machine-readable price observations."""

        self.prices = dict(prices)

    def discover_pricing(self, provider_id: str) -> list[PricingEvidence]:
        evidence: list[PricingEvidence] = []
        rule = _RULES_BY_PROVIDER.get(provider_id)
        for model_id, (price, _currency) in sorted(self.prices.items()):
            positive = _is_positive_price(price)
            evidence.append(
                PricingEvidence(
                    provider_id=provider_id,
                    upstream_model_id=model_id,
                    billing_class=(
                        BillingClass.PAID if positive else BillingClass.UNKNOWN
                    ),
                    source=EvidenceSource.MACHINE_READABLE,
                    source_url=rule.source_url if rule is not None else None,
                    requires_card=rule.requires_card if rule is not None else False,
                    requires_topup=bool(positive and rule is None),
                    machine_code="price>0" if positive else "price==0",
                    note=(
                        f"Published price {price}. A positive price proves this model "
                        "is paid; a zero price proves nothing about cards."
                    ),
                )
            )
        if rule is not None and not self.prices:
            evidence.extend(super().discover_pricing(provider_id))
        return evidence

    def discover_account_constraints(self, provider_id: str) -> AccountConstraints:
        return super().discover_account_constraints(provider_id)


class GenericUnknownAdapter:
    """Default adapter: everything is unknown, so everything is blocked in strict."""

    provider_id = "*"

    def discover_pricing(self, provider_id: str) -> list[PricingEvidence]:
        return []

    def discover_account_constraints(self, provider_id: str) -> AccountConstraints:
        return AccountConstraints(
            access_status=AccessStatus.UNKNOWN,
            source=EvidenceSource.UNVERIFIED_LIST,
            note=(
                "No eligibility adapter for this provider yet. Its models cannot be "
                "proven free, so strict mode blocks them."
            ),
        )

    def parse_provider_error(
        self, status: int, payload: object
    ) -> NormalizedProviderError:
        return classify_provider_error(status, payload)


class EligibilityAdapterRegistry:
    """Choose one adapter per provider without hardcoding anything in the UI."""

    def __init__(
        self, adapters: Iterable[ProviderEligibilityAdapter] | None = None
    ) -> None:
        self._adapters: dict[str, ProviderEligibilityAdapter] = {}
        for provider_id in known_provider_ids():
            self._adapters[provider_id] = self._build(provider_id)
        for adapter in adapters or ():
            self._adapters[adapter.provider_id] = adapter

    @staticmethod
    def _build(provider_id: str) -> ProviderEligibilityAdapter:
        if provider_id in _MACHINE_READABLE_PROVIDERS:
            return MachineReadablePriceAdapter(provider_id)
        return StaticRuleAdapter(provider_id)

    def adapter(self, provider_id: str) -> ProviderEligibilityAdapter:
        """Return the dedicated adapter, or the generic unknown one."""

        return self._adapters.get(provider_id, GenericUnknownAdapter())

    def has_adapter(self, provider_id: str) -> bool:
        return provider_id in self._adapters

    def register(self, adapter: ProviderEligibilityAdapter) -> None:
        self._adapters[adapter.provider_id] = adapter


#: Providers whose public API publishes per-model prices we can read.
_MACHINE_READABLE_PROVIDERS = frozenset({"open_router"})


def classify_provider_error(status: int, payload: object) -> NormalizedProviderError:
    """Classify one provider failure without trusting its HTTP status alone.

    A ``429`` carrying ``model_requires_purchase`` is a purchase lock, not a rate
    limit: retrying cannot help and must not be attempted. Only a genuine rate limit
    produces a cooldown.
    """

    code = _machine_code(payload)
    lowered = code.casefold()
    message = _safe_message(payload)
    haystack = f"{lowered} {message.casefold()}"

    if any(token in haystack for token in _PURCHASE_CODES):
        return NormalizedProviderError(
            code=NormalizedErrorCode.PAYMENT_REQUIRED,
            billing_class=BillingClass.REQUIRES_PURCHASE,
            access_status=AccessStatus.PAYMENT_LOCKED,
            summary="The provider reports this model requires a purchase.",
        )
    if any(token in haystack for token in _REGION_CODES) or "region" in haystack:
        return NormalizedProviderError(
            code=NormalizedErrorCode.REGION_BLOCKED,
            access_status=AccessStatus.REGION_BLOCKED,
            summary="The provider reports the region is not served.",
        )
    if any(token in haystack for token in _AUTH_CODES):
        return NormalizedProviderError(
            code=NormalizedErrorCode.AUTH_REQUIRED,
            access_status=AccessStatus.AUTH_REQUIRED,
            summary="The provider rejected the credential.",
        )
    if any(token in haystack for token in _MODEL_CODES):
        return NormalizedProviderError(
            code=NormalizedErrorCode.MODEL_NOT_FOUND,
            access_status=AccessStatus.INVALID_MODEL,
            summary="The provider does not recognize this model id.",
        )
    if any(token in haystack for token in _RATE_LIMIT_CODES):
        return NormalizedProviderError(
            code=NormalizedErrorCode.RATE_LIMITED,
            access_status=AccessStatus.TEMPORARILY_RATE_LIMITED,
            retry_after_seconds=_retry_after(payload),
            summary="The provider rate limited this account.",
        )
    if status == 429:
        return NormalizedProviderError(
            code=NormalizedErrorCode.RATE_LIMITED,
            access_status=AccessStatus.TEMPORARILY_RATE_LIMITED,
            retry_after_seconds=_retry_after(payload),
            summary="The provider rate limited this account.",
        )
    if status in (401, 403):
        return NormalizedProviderError(
            code=NormalizedErrorCode.AUTH_REQUIRED,
            access_status=AccessStatus.AUTH_REQUIRED,
            summary="The provider rejected the credential.",
        )
    if status in (500, 502, 503, 504):
        return NormalizedProviderError(
            code=(
                NormalizedErrorCode.PROVIDER_TIMEOUT
                if status == 504
                else NormalizedErrorCode.PROVIDER_ERROR
            ),
            access_status=AccessStatus.PROVIDER_UNAVAILABLE,
            summary="The provider is unavailable.",
        )
    if status == 404:
        return NormalizedProviderError(
            code=NormalizedErrorCode.MODEL_NOT_FOUND,
            access_status=AccessStatus.INVALID_MODEL,
            summary="The provider does not recognize this model id.",
        )
    return NormalizedProviderError(
        code=NormalizedErrorCode.PROVIDER_ERROR,
        summary="The provider reported an unrecognized failure.",
    )


def _machine_code(payload: object) -> str:
    if isinstance(payload, Mapping):
        for key in ("code", "error_code", "type", "reason", "machine_code"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = payload.get("error")
        if isinstance(nested, Mapping):
            return _machine_code(nested)
        nested = payload.get("detail")
        if isinstance(nested, Mapping):
            return _machine_code(nested)
    if isinstance(payload, str):
        return payload.strip()[:200]
    return ""


def _safe_message(payload: object) -> str:
    """Return at most a bounded message fragment used only for classification."""

    if isinstance(payload, Mapping):
        for key in ("message", "detail", "summary", "error"):
            value = payload.get(key)
            if isinstance(value, str):
                return value.strip()[:300]
            if isinstance(value, Mapping):
                nested = _safe_message(value)
                if nested:
                    return nested
    if isinstance(payload, str):
        return payload.strip()[:300]
    return ""


def _retry_after(payload: object) -> int | None:
    if isinstance(payload, Mapping):
        for key in ("retry_after", "retry_after_seconds", "Retry-After"):
            value = payload.get(key)
            if isinstance(value, int | float) and value >= 0:
                return int(value)
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
    return None


def _is_positive_price(price: str) -> bool:
    try:
        return float(price) > 0.0
    except TypeError, ValueError:
        return False


def provider_display_name(provider_id: str) -> str:
    descriptor = PROVIDER_CATALOG.get(provider_id)
    return descriptor.display_name if descriptor is not None else provider_id


def eligibility_reason_labels() -> dict[str, str]:
    """Short, UI-friendly labels for every reason code."""

    return {
        ReasonCode.ELIGIBLE.value: "Eligible free model",
        ReasonCode.EVIDENCE_MISSING.value: "No evidence yet",
        ReasonCode.EVIDENCE_STALE.value: "Evidence expired",
        ReasonCode.BILLING_CLASS_BLOCKED.value: "Not a free tier",
        ReasonCode.REQUIRES_CARD.value: "Requires a card",
        ReasonCode.REQUIRES_TOPUP.value: "Requires a top-up",
        ReasonCode.REQUIRES_SUBSCRIPTION.value: "Requires a subscription",
        ReasonCode.TRIAL_OR_PROMO_BALANCE.value: "Trial or promo credit",
        ReasonCode.LOCAL_NOT_ALLOWED.value: "Local models disabled",
        ReasonCode.ACCESS_BLOCKED.value: "Access blocked",
        ReasonCode.PAYMENT_LOCKED.value: "Requires purchase",
        ReasonCode.ACCOUNT_BINDING_CHANGED.value: "Account credentials changed",
        ReasonCode.MANUAL_APPROVAL_REQUIRED.value: "Needs attestation",
        ReasonCode.NO_ELIGIBLE_FREE_MODELS.value: "No eligible free model",
        ReasonCode.MODE_OFF.value: "Guard off",
        ReasonCode.CATALOG_ONLY.value: "Catalog filter only",
    }

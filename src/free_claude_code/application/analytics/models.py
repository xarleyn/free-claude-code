"""Analytics event vocabulary.

The central accounting rule: **one client request is not one provider attempt.**
``logical_request`` is what a client asked FCC for; ``upstream_attempt`` is one actual
send to one provider. A retry or a fallback is a new attempt inside the same logical
request, and the UI must say so everywhere.

Token counts are nullable. ``None`` means "unknown", which is *not* zero: an attempt
that reported no usage must never contribute ``0`` to an input-token total, because
that would silently understate consumption. Cost is a decimal string for the same
reason — floats would round ``0.0000004`` away.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from free_claude_code.application.free_guard.models import utc_now


class ClientType(StrEnum):
    """Which integration the request came from, only when it is knowable."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    VSCODE_CHAT = "vscode_chat"
    DSH = "dsh"
    OPENCODE = "opencode"
    BROWSER = "browser"
    OTHER = "other"
    UNKNOWN = "unknown"


class Origin(StrEnum):
    """Who spent the tokens."""

    USER = "user"
    HEALTH_PROBE = "health_probe"
    INTERNAL = "internal"


class WireProtocol(StrEnum):
    MESSAGES = "messages"
    RESPONSES = "responses"
    OTHER = "other"


class RequestStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    PARTIAL_STREAM = "partial_stream"


class AttemptStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"
    CANCELED = "canceled"
    PARTIAL_STREAM = "partial_stream"
    ABANDONED = "abandoned"


class UsageSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    LOCAL_ESTIMATE = "local_estimate"
    UNKNOWN = "unknown"


class UsageCompleteness(StrEnum):
    FINAL = "final"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class CostConfidence(StrEnum):
    VERIFIED = "verified"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Normalized token counts. ``None`` means the provider did not report it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    @property
    def known(self) -> bool:
        """Whether any count is known at all."""

        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.reasoning_output_tokens,
            )
        )

    def merged_with(self, other: TokenUsage) -> TokenUsage:
        """Return the union of two observations, preferring known values.

        Providers often report input tokens once at the start of a stream and output
        tokens again at the end. Merging keeps both without double counting, because
        each slot is written at most once.
        """

        return TokenUsage(
            input_tokens=(
                self.input_tokens
                if self.input_tokens is not None
                else other.input_tokens
            ),
            output_tokens=(
                self.output_tokens
                if self.output_tokens is not None
                else other.output_tokens
            ),
            cache_read_tokens=(
                self.cache_read_tokens
                if self.cache_read_tokens is not None
                else other.cache_read_tokens
            ),
            cache_write_tokens=(
                self.cache_write_tokens
                if self.cache_write_tokens is not None
                else other.cache_write_tokens
            ),
            reasoning_output_tokens=(
                self.reasoning_output_tokens
                if self.reasoning_output_tokens is not None
                else other.reasoning_output_tokens
            ),
        )


@dataclass(slots=True)
class LogicalRequestRecord:
    """One external request from one client."""

    request_id: str
    started_at_utc: object = None
    ended_at_utc: object = None
    origin: Origin = Origin.USER
    client_type: ClientType = ClientType.UNKNOWN
    wire_protocol: WireProtocol = WireProtocol.OTHER
    requested_model: str | None = None
    status: RequestStatus = RequestStatus.SUCCESS
    normalized_error_code: str | None = None
    duration_ms: int | None = None
    ttft_ms: int | None = None
    fallback_count: int = 0
    retry_count: int = 0
    request_streamed: bool = False
    resolved_provider_id: str | None = None
    resolved_upstream_model_id: str | None = None
    final_attempt_id: str | None = None
    client_label: str | None = None
    finalized: bool = False

    def __post_init__(self) -> None:
        if self.started_at_utc is None:
            self.started_at_utc = utc_now()


@dataclass(slots=True)
class UpstreamAttemptRecord:
    """One actual send to one provider; retries and fallbacks are separate rows."""

    attempt_id: str
    request_id: str
    attempt_number: int
    provider_id: str
    upstream_model_id: str
    started_at_utc: object = None
    ended_at_utc: object = None
    status: AttemptStatus = AttemptStatus.SUCCESS
    upstream_http_status: int | None = None
    normalized_error_code: str | None = None
    retry_after_seconds: int | None = None
    fallback_from_attempt_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    usage_source: UsageSource = UsageSource.UNKNOWN
    usage_completeness: UsageCompleteness = UsageCompleteness.UNKNOWN
    duration_ms: int | None = None
    ttft_ms: int | None = None
    price_snapshot_id: int | None = None
    estimated_cost_usd: str | None = None
    cost_confidence: CostConfidence = CostConfidence.UNKNOWN
    finalized: bool = False
    #: Session-local bookkeeping; not persisted.
    usage: TokenUsage = field(default_factory=TokenUsage)
    #: Wall-clock start used for TTFT and duration; never written to SQLite.
    started_monotonic: float = 0.0

    def __post_init__(self) -> None:
        if self.started_at_utc is None:
            self.started_at_utc = utc_now()


@dataclass(frozen=True, slots=True)
class PricePoint:
    """A price snapshot entry used to estimate cost."""

    input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    reasoning_per_million: Decimal | None = None
    verified_free: bool = False

    @property
    def known(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_per_million,
                self.output_per_million,
                self.cache_read_per_million,
                self.cache_write_per_million,
                self.reasoning_per_million,
            )
        )


def estimate_cost(
    usage: TokenUsage, price: PricePoint
) -> tuple[str | None, CostConfidence]:
    """Estimate cost, returning ``None`` whenever any input is unknown.

    For a verified free model the result is exactly ``"0.00"`` with ``verified``
    confidence. For unknown prices or unknown token counts the result is ``None`` —
    never ``0``.
    """

    if price.verified_free and not usage.known:
        return "0.00", CostConfidence.VERIFIED
    if not usage.known:
        return None, CostConfidence.UNKNOWN
    if price.verified_free:
        return "0.00", CostConfidence.VERIFIED
    if not price.known:
        return None, CostConfidence.UNKNOWN

    total = Decimal("0")
    for tokens, rate in (
        (usage.input_tokens, price.input_per_million),
        (usage.output_tokens, price.output_per_million),
        (usage.cache_read_tokens, price.cache_read_per_million),
        (usage.cache_write_tokens, price.cache_write_per_million),
        (usage.reasoning_output_tokens, price.reasoning_per_million),
    ):
        if tokens is None:
            continue
        if rate is None:
            # A component without a price cannot be costed; do not pretend it is free.
            return None, CostConfidence.UNKNOWN
        total += Decimal(tokens) * rate / Decimal(1_000_000)
    return format(total.quantize(Decimal("0.000001"))), CostConfidence.ESTIMATED


def sum_known(values: Sequence[int | None]) -> tuple[int, int]:
    """Return ``(known_total, unknown_count)`` without treating unknown as zero."""

    total = 0
    unknown = 0
    for value in values:
        if value is None:
            unknown += 1
        else:
            total += value
    return total, unknown

"""Typed capabilities consumed by the request path for analytics."""

from typing import Protocol, runtime_checkable

from ..free_guard.models import EligibilityDecision
from .models import (
    AttemptStatus,
    CostConfidence,
    LogicalRequestRecord,
    RequestStatus,
    TokenUsage,
    UpstreamAttemptRecord,
    UsageCompleteness,
    UsageSource,
)


@runtime_checkable
class AttemptRecorder(Protocol):
    """The attempt-level sink the executor writes to while it streams."""

    def begin(
        self,
        *,
        request_id: str,
        attempt_number: int,
        provider_id: str,
        upstream_model_id: str,
        fallback_from_attempt_id: str | None = None,
        streamed: bool = False,
    ) -> UpstreamAttemptRecord: ...

    def mark_first_token(self, attempt: UpstreamAttemptRecord) -> None: ...

    def record_usage(
        self,
        attempt: UpstreamAttemptRecord,
        *,
        usage: TokenUsage,
        source: UsageSource,
        completeness: UsageCompleteness,
        price_snapshot_id: int | None = None,
        estimated_cost_usd: str | None = None,
        cost_confidence: CostConfidence = CostConfidence.UNKNOWN,
    ) -> None: ...

    def finish(
        self,
        attempt: UpstreamAttemptRecord,
        *,
        status: AttemptStatus,
        upstream_http_status: int | None = None,
        normalized_error_code: str | None = None,
        retry_after_seconds: int | None = None,
        usage_source: UsageSource | None = None,
        usage_completeness: UsageCompleteness | None = None,
        cost_confidence: CostConfidence | None = None,
    ) -> None: ...

    def blocked(
        self,
        *,
        request_id: str,
        attempt_number: int,
        provider_id: str,
        upstream_model_id: str,
        decision: EligibilityDecision,
    ) -> None: ...


@runtime_checkable
class AnalyticsRecorder(Protocol):
    """The request-level analytics sink. Never allowed to fail a request."""

    enabled: bool

    def start_request(
        self,
        *,
        request_id: str,
        client_type: object,
        wire_protocol: object,
        requested_model: str | None,
        streamed: bool = False,
        origin: object = None,
        client_label: str | None = None,
    ) -> LogicalRequestRecord | None: ...

    def attempt_recorder(
        self, request: LogicalRequestRecord
    ) -> AttemptRecorder | None: ...

    def route_selected(
        self,
        request: LogicalRequestRecord,
        *,
        provider_id: str,
        upstream_model_id: str,
    ) -> None: ...

    def complete_request(
        self,
        request: LogicalRequestRecord,
        *,
        status: RequestStatus,
        normalized_error_code: str | None = None,
        provider_id: str | None = None,
        upstream_model_id: str | None = None,
    ) -> None: ...

    def abandon_request(self, request: LogicalRequestRecord) -> None: ...

    def dropped_events(self) -> int: ...

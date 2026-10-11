"""Analytics runtime service: the recorder the request path calls, and the read
models the Admin API renders.

Separation of concerns:

* :class:`AnalyticsService` is the only object the request path sees. It records, it
  never queries.
* Queries live in :mod:`free_claude_code.api.analytics_routes` over the store, with
  allowlisted filter fields and parameterized SQL only.

Nothing in here can fail a request: every recording method swallows storage errors and
counts a dropped event instead.
"""

import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import cast

from loguru import logger

from free_claude_code.api.client_attribution import client_label
from free_claude_code.application.analytics.models import (
    AttemptStatus,
    ClientType,
    CostConfidence,
    LogicalRequestRecord,
    Origin,
    RequestStatus,
    TokenUsage,
    UpstreamAttemptRecord,
    UsageCompleteness,
    UsageSource,
    WireProtocol,
    utc_now,
)
from free_claude_code.application.analytics.ports import AttemptRecorder
from free_claude_code.application.analytics.queries import AnalyticsQueries
from free_claude_code.application.free_guard.models import EligibilityDecision, utc_text
from free_claude_code.core.json_types import JsonObject
from free_claude_code.runtime.analytics_store import AnalyticsStore, day_bucket
from free_claude_code.runtime.analytics_writer import AnalyticsWriter, RequestBuffer


class NullAttemptRecorder:
    """Attempt recorder used when analytics is disabled."""

    """Attempt recorder used when analytics is disabled."""

    def begin(self, **_kwargs) -> None:
        return None

    def mark_first_token(self, _attempt) -> None:
        return None

    def finish(self, _attempt, **_kwargs) -> None:
        return None

    def blocked(self, **_kwargs) -> None:
        return None


class AttemptRecorderImpl:
    """Attempt-level sink bound to one request buffer."""

    def __init__(self, service: AnalyticsService, buffer: RequestBuffer | None) -> None:
        self._service = service
        self._buffer = buffer

    def begin(
        self,
        *,
        request_id: str,
        attempt_number: int,
        provider_id: str,
        upstream_model_id: str,
        fallback_from_attempt_id: str | None = None,
        streamed: bool = False,
    ) -> UpstreamAttemptRecord:
        record = UpstreamAttemptRecord(
            attempt_id=f"{request_id}:{attempt_number}",
            request_id=request_id,
            attempt_number=attempt_number,
            provider_id=provider_id,
            upstream_model_id=upstream_model_id,
            fallback_from_attempt_id=fallback_from_attempt_id,
            started_monotonic=time.monotonic(),
        )
        self._service._register_attempt(self._buffer, record)
        return record

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
    ) -> None:
        self._service._record_attempt_usage(
            self._buffer,
            attempt,
            usage=usage,
            source=source,
            completeness=completeness,
            price_snapshot_id=price_snapshot_id,
            estimated_cost_usd=estimated_cost_usd,
            cost_confidence=cost_confidence,
        )

    def mark_first_token(self, attempt: UpstreamAttemptRecord) -> None:
        self._service._mark_first_token(self._buffer, attempt)

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
    ) -> None:
        self._service._finish_attempt(
            self._buffer,
            attempt,
            status=status,
            upstream_http_status=upstream_http_status,
            normalized_error_code=normalized_error_code,
            retry_after_seconds=retry_after_seconds,
            usage_source=usage_source,
            usage_completeness=usage_completeness,
            cost_confidence=cost_confidence,
        )

    def blocked(
        self,
        *,
        request_id: str,
        attempt_number: int,
        provider_id: str,
        upstream_model_id: str,
        decision: EligibilityDecision,
    ) -> None:
        self._service._record_blocked_attempt(
            self._buffer,
            request_id=request_id,
            attempt_number=attempt_number,
            provider_id=provider_id,
            upstream_model_id=upstream_model_id,
            decision=decision,
        )


_NULL_RECORDER = NullAttemptRecorder()


class AnalyticsService:
    """Request-path recorder and Admin read model."""

    def __init__(
        self,
        settings_provider,
        *,
        store: AnalyticsStore | None = None,
        writer: AnalyticsWriter | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._store = store or AnalyticsStore(None)
        self._writer = writer or AnalyticsWriter(self._store)
        self._queries = AnalyticsQueries(self._store)

    # ------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings_provider(), "analytics_enabled", False))

    @property
    def durable(self) -> bool:
        return self._store.durable

    @property
    def queries(self) -> AnalyticsQueries | None:
        """Read models for the Admin API, or ``None`` when storage is off."""

        if not self._store.durable:
            return None
        return self._queries

    async def start(self) -> None:
        if not self.enabled:
            return
        if not self._store.durable:
            logger.info(
                "Analytics storage is not available; accounting is disabled but "
                "requests are unaffected."
            )
            return
        self._writer.start()

    async def close(self) -> None:
        await self._writer.close()

    # -------------------------------------------------------------- recorder

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
    ) -> LogicalRequestRecord | None:
        if not self.enabled:
            return None
        record = LogicalRequestRecord(
            request_id=request_id,
            started_at_utc=utc_now(),
            origin=_enum(origin, Origin, Origin.USER),
            client_type=_enum(client_type, ClientType, ClientType.UNKNOWN),
            wire_protocol=_enum(wire_protocol, WireProtocol, WireProtocol.OTHER),
            requested_model=requested_model,
            request_streamed=streamed,
            client_label=client_label,
        )
        buffer = self._writer.track(record)
        self._writer.submit(buffer)
        return record

    def attempt_recorder(self, request: LogicalRequestRecord | None) -> AttemptRecorder:
        if request is None or not self.enabled:
            return cast(AttemptRecorder, NullAttemptRecorder())
        return AttemptRecorderImpl(self, self._writer.buffer(request.request_id))

    def route_selected(
        self,
        request: LogicalRequestRecord,
        *,
        provider_id: str,
        upstream_model_id: str,
    ) -> None:
        if request is None or not self.enabled:
            return
        request.resolved_provider_id = provider_id
        request.resolved_upstream_model_id = upstream_model_id
        self._touch(request)

    def complete_request(
        self,
        request: LogicalRequestRecord,
        *,
        status: RequestStatus,
        normalized_error_code: str | None = None,
        provider_id: str | None = None,
        upstream_model_id: str | None = None,
    ) -> None:
        if request is None or not self.enabled:
            return
        if request.finalized:
            # Finalization is idempotent: a repeated stream end must not double count.
            return
        request.status = _enum(status, RequestStatus, RequestStatus.SUCCESS)
        request.normalized_error_code = normalized_error_code
        if provider_id is not None:
            request.resolved_provider_id = provider_id
        if upstream_model_id is not None:
            request.resolved_upstream_model_id = upstream_model_id
        request.ended_at_utc = utc_now()
        self._finalize(request)

    def abandon_request(self, request: LogicalRequestRecord) -> None:
        if request is None or not self.enabled or request.finalized:
            return
        request.status = RequestStatus.ERROR
        request.ended_at_utc = utc_now()
        self._finalize(request)

    def dropped_events(self) -> int:
        return self._writer.dropped_events

    def stats(self) -> JsonObject:
        payload: JsonObject = dict(self._writer.stats())
        payload["enabled"] = self.enabled
        return payload

    # ------------------------------------------------------------ internals

    def _touch(self, request: LogicalRequestRecord) -> None:
        buffer = self._writer.buffer(request.request_id)
        if buffer is not None:
            self._writer.submit(buffer)

    def _finalize(self, request: LogicalRequestRecord) -> None:
        request.finalized = True
        buffer = self._writer.buffer(request.request_id)
        if buffer is None:
            return
        attempts = sorted(
            buffer.attempts.values(), key=lambda item: item.attempt_number
        )
        request.fallback_count = max(0, len(attempts) - 1)
        if attempts:
            last = attempts[-1]
            request.final_attempt_id = last.attempt_id
            if not request.resolved_provider_id:
                request.resolved_provider_id = last.provider_id
            if not request.resolved_upstream_model_id:
                request.resolved_upstream_model_id = last.upstream_model_id
        self._writer.submit(buffer)

    def _register_attempt(
        self, buffer: RequestBuffer | None, record: UpstreamAttemptRecord
    ) -> None:
        if buffer is None:
            return
        buffer.attempts[record.attempt_id] = record
        self._writer.submit(buffer)

    def _mark_first_token(
        self, buffer: RequestBuffer | None, attempt: UpstreamAttemptRecord
    ) -> None:
        if buffer is None:
            return
        if attempt.ttft_ms is None:
            elapsed = time.monotonic() - attempt.started_monotonic
            attempt.ttft_ms = max(0, int(elapsed * 1000))
            if buffer.record.ttft_ms is None:
                buffer.record.ttft_ms = attempt.ttft_ms
        self._writer.submit(buffer)

    def _finish_attempt(
        self,
        buffer: RequestBuffer | None,
        attempt: UpstreamAttemptRecord,
        *,
        status: AttemptStatus,
        upstream_http_status: int | None = None,
        normalized_error_code: str | None = None,
        retry_after_seconds: int | None = None,
        usage_source: UsageSource | None = None,
        usage_completeness: UsageCompleteness | None = None,
        cost_confidence: CostConfidence | None = None,
    ) -> None:
        if buffer is None:
            return
        now = time.monotonic()
        attempt.status = _enum(status, AttemptStatus, AttemptStatus.SUCCESS)
        attempt.upstream_http_status = upstream_http_status
        attempt.normalized_error_code = normalized_error_code
        attempt.retry_after_seconds = retry_after_seconds
        attempt.ended_at_utc = utc_now()
        if attempt.started_monotonic:
            attempt.duration_ms = max(0, int((now - attempt.started_monotonic) * 1000))
        if usage_source is not None:
            attempt.usage_source = _enum(usage_source, UsageSource, UsageSource.UNKNOWN)
        if usage_completeness is not None:
            attempt.usage_completeness = usage_completeness
        if cost_confidence is not None:
            attempt.cost_confidence = _enum(
                cost_confidence, CostConfidence, CostConfidence.UNKNOWN
            )
        attempt.finalized = True
        self._writer.submit(buffer)

    def _record_attempt_usage(
        self,
        buffer: RequestBuffer | None,
        attempt: UpstreamAttemptRecord,
        *,
        usage: TokenUsage,
        source: UsageSource,
        completeness: UsageCompleteness,
        price_snapshot_id: int | None,
        estimated_cost_usd: str | None,
        cost_confidence: CostConfidence,
    ) -> None:
        if buffer is None:
            return
        attempt.usage = usage
        attempt.input_tokens = usage.input_tokens
        attempt.output_tokens = usage.output_tokens
        attempt.cache_read_tokens = usage.cache_read_tokens
        attempt.cache_write_tokens = usage.cache_write_tokens
        attempt.reasoning_output_tokens = usage.reasoning_output_tokens
        attempt.usage_source = _enum(source, UsageSource, UsageSource.UNKNOWN)
        attempt.usage_completeness = completeness
        attempt.price_snapshot_id = price_snapshot_id
        attempt.estimated_cost_usd = estimated_cost_usd
        attempt.cost_confidence = _enum(
            cost_confidence, CostConfidence, CostConfidence.UNKNOWN
        )
        self._writer.submit(buffer)

    def _record_blocked_attempt(
        self,
        buffer: RequestBuffer | None,
        *,
        request_id: str,
        attempt_number: int,
        provider_id: str,
        upstream_model_id: str,
        decision: EligibilityDecision,
    ) -> None:
        if buffer is None:
            return
        record = UpstreamAttemptRecord(
            attempt_id=f"{request_id}:{attempt_number}",
            request_id=request_id,
            attempt_number=attempt_number,
            provider_id=provider_id,
            upstream_model_id=upstream_model_id,
            started_at_utc=utc_now(),
            ended_at_utc=utc_now(),
            status=AttemptStatus.BLOCKED,
            normalized_error_code=decision.reason_code.value,
        )
        buffer.attempts[record.attempt_id] = record
        buffer.record.status = RequestStatus.BLOCKED
        buffer.record.normalized_error_code = decision.reason_code.value
        if buffer.record.ended_at_utc is None:
            buffer.record.ended_at_utc = utc_now()
        self._writer.submit(buffer)

    # ----------------------------------------------------------- maintenance

    def note_settings_changed(self) -> None:
        """Settings reloads need no cached state on the recorder side."""

        return None

    async def mark_abandoned(self, *, older_than_minutes: int = 60) -> int:
        """Mark requests that never finalized, without inventing token counts."""

        if not self._store.durable:
            return 0
        cutoff = utc_now() - timedelta(minutes=max(1, older_than_minutes))
        try:
            return await self._store.mark_abandoned(cutoff)
        except Exception:
            return 0

    async def prune(self) -> int:
        if not self.enabled or not self._store.durable:
            return 0
        settings = self._settings_provider()
        retention = int(getattr(settings, "analytics_retention_days", 90))
        rollup_retention = int(
            getattr(settings, "analytics_rollup_retention_days", 365)
        )
        now = utc_now()
        try:
            return await self._store.prune(
                raw_cutoff=now - timedelta(days=retention),
                rollup_cutoff=now - timedelta(days=rollup_retention),
            )
        except Exception:
            return 0

    async def rebuild_rollups(self, *, days: int = 400) -> int:
        """Recompute the materialized daily rollups from raw rows."""

        if not self._store.durable:
            return 0
        cutoff = utc_now() - timedelta(days=max(1, days))

        def operation(connection) -> int:
            connection.execute(
                "DELETE FROM analytics_daily_rollups WHERE day_utc >= ?",
                (day_bucket(cutoff),),
            )
            cursor = connection.execute(
                """
                INSERT INTO analytics_daily_rollups (
                    day_utc, provider_id, upstream_model_id, client_type, status, origin,
                    logical_requests, upstream_attempts, fallback_requests,
                    success_count, error_count, blocked_count,
                    input_tokens_known, output_tokens_known,
                    cache_read_tokens_known, cache_write_tokens_known,
                    unknown_usage_attempts, estimated_cost_usd
                )
                SELECT
                    substr(r.started_at_utc, 1, 10) AS day_utc,
                    COALESCE(a.provider_id, '') AS provider_id,
                    COALESCE(a.upstream_model_id, '') AS upstream_model_id,
                    r.client_type AS client_type,
                    r.status AS status,
                    r.origin AS origin,
                    1 AS logical_requests,
                    (SELECT COUNT(*) FROM upstream_attempts x
                      WHERE x.request_id = r.request_id) AS upstream_attempts,
                    CASE WHEN r.fallback_count > 0 THEN 1 ELSE 0 END AS fallback_requests,
                    CASE WHEN r.status = 'success' THEN 1 ELSE 0 END AS success_count,
                    CASE WHEN r.status = 'error' THEN 1 ELSE 0 END AS error_count,
                    CASE WHEN r.status = 'blocked' THEN 1 ELSE 0 END AS blocked_count,
                    COALESCE((SELECT SUM(x.input_tokens) FROM upstream_attempts x
                       WHERE x.request_id = r.request_id AND x.input_tokens IS NOT NULL), 0),
                    COALESCE((SELECT SUM(x.output_tokens) FROM upstream_attempts x
                       WHERE x.request_id = r.request_id AND x.output_tokens IS NOT NULL), 0),
                    COALESCE((SELECT SUM(x.cache_read_tokens) FROM upstream_attempts x
                       WHERE x.request_id = r.request_id AND x.cache_read_tokens IS NOT NULL), 0),
                    COALESCE((SELECT SUM(x.cache_write_tokens) FROM upstream_attempts x
                       WHERE x.request_id = r.request_id AND x.cache_write_tokens IS NOT NULL), 0),
                    (SELECT COUNT(*) FROM upstream_attempts x
                       WHERE x.request_id = r.request_id AND x.usage_source = 'unknown')
                        AS unknown_usage_attempts,
                    NULL AS estimated_cost_usd
                FROM logical_requests r
                LEFT JOIN upstream_attempts a ON a.request_id = r.request_id
                     AND a.attempt_number = (
                         SELECT MAX(y.attempt_number) FROM upstream_attempts y
                          WHERE y.request_id = r.request_id
                     )
                WHERE r.started_at_utc >= ?
                GROUP BY day_utc, provider_id, upstream_model_id, r.client_type,
                         r.status, r.origin
                """,
                (day_bucket(cutoff),),
            )
            return int(cursor.rowcount or 0)

        try:
            return await self._store.write_operation(operation)
        except Exception as exc:
            logger.warning(
                "Analytics rollup rebuild failed: exc_type={}", type(exc).__name__
            )
            return 0

    async def read[T](self, operation) -> T:
        """Run one parameterized read operation against the analytics store."""

        return await self._store.query(operation)


def _enum(value, enum_type, default):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except ValueError, KeyError:
        return default


def client_label_for_request(headers: Mapping[str, str] | None) -> str | None:
    return client_label(headers)


def format_datetime(value: datetime | None) -> str | None:
    return utc_text(value) if value is not None else None


def now_utc() -> datetime:
    return utc_now()


__all__ = [
    "AnalyticsService",
    "AttemptRecorderImpl",
    "NullAttemptRecorder",
    "client_label_for_request",
]

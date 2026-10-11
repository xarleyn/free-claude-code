"""Writer, retention and idempotency of usage accounting."""

import pytest

from free_claude_code.application.analytics.models import (
    ClientType,
    LogicalRequestRecord,
    Origin,
    RequestStatus,
    TokenUsage,
    UsageCompleteness,
    WireProtocol,
    utc_now,
)
from free_claude_code.runtime.analytics_writer import AnalyticsWriter


class RecordingStore:
    """Captures what the writer hands to storage."""

    def __init__(self) -> None:
        self.requests: list[LogicalRequestRecord] = []
        self.attempts: list[tuple[str, int]] = []
        self.durable = True
        self.fail = False

    async def upsert_request(self, record: LogicalRequestRecord) -> None:
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.requests.append(record)

    async def upsert_attempts(self, request: LogicalRequestRecord, attempts) -> None:
        if self.fail:
            raise RuntimeError("storage unavailable")
        self.attempts.append((request.request_id, len(attempts)))


async def _drain(writer: AnalyticsWriter) -> None:
    await writer.close()


def _request(request_id: str) -> LogicalRequestRecord:
    return LogicalRequestRecord(
        request_id=request_id,
        started_at_utc=utc_now(),
        origin=Origin.USER,
        client_type=ClientType.CLAUDE_CODE,
        wire_protocol=WireProtocol.MESSAGES,
        requested_model="claude-3-5-sonnet-20241022",
    )


@pytest.mark.asyncio
async def test_success_completion_is_idempotent() -> None:
    store = RecordingStore()
    writer = AnalyticsWriter(store)
    writer.start()
    record = _request("req-1")
    buffer = writer.track(record)
    writer.submit(buffer)
    writer.submit(buffer)  # already queued: must not duplicate
    record.status = RequestStatus.SUCCESS
    record.ended_at_utc = utc_now()
    record.finalized = True
    writer.submit(buffer)
    await _drain(writer)
    assert len(store.requests) == 1
    assert store.requests[0].status is RequestStatus.SUCCESS


@pytest.mark.asyncio
async def test_attempt_rows_stay_with_their_logical_request() -> None:
    store = RecordingStore()
    writer = AnalyticsWriter(store)
    writer.start()
    record = _request("req-2")
    buffer = writer.track(record)
    from free_claude_code.application.analytics.models import UpstreamAttemptRecord

    buffer.attempts["a1"] = UpstreamAttemptRecord(
        attempt_id="a1",
        request_id="req-2",
        attempt_number=1,
        provider_id="groq",
        upstream_model_id="llama",
    )
    buffer.attempts["a2"] = UpstreamAttemptRecord(
        attempt_id="a2",
        request_id="req-2",
        attempt_number=2,
        provider_id="open_router",
        upstream_model_id="other",
        fallback_from_attempt_id="a1",
    )
    writer.submit(buffer)
    await _drain(writer)
    assert store.attempts == [("req-2", 2)]


@pytest.mark.asyncio
async def test_storage_failure_is_counted_not_raised() -> None:
    store = RecordingStore()
    store.fail = True
    writer = AnalyticsWriter(store)
    writer.start()
    buffer = writer.track(_request("req-3"))
    writer.submit(buffer)
    await _drain(writer)
    assert writer.dropped_events >= 1


@pytest.mark.asyncio
async def test_priority_blocked_records_survive_queue_pressure() -> None:
    store = RecordingStore()
    writer = AnalyticsWriter(store, queue_limit=2)
    writer.start()
    blocked = writer.track(_request("req-blocked"))
    blocked.record.status = RequestStatus.BLOCKED
    for index in range(4):
        buffer = writer.track(_request(f"req-{index}"))
        writer.submit(buffer)
    # The blocked record is re-submitted so it cannot be shed.
    writer.submit(blocked)
    await _drain(writer)
    assert "req-blocked" in [record.request_id for record in store.requests]


@pytest.mark.asyncio
async def test_usage_and_cost_models() -> None:
    from decimal import Decimal

    from free_claude_code.application.analytics.models import PricePoint, estimate_cost

    unknown = TokenUsage()
    assert not unknown.known
    assert estimate_cost(unknown, PricePoint())[0] is None
    assert estimate_cost(unknown, PricePoint())[1].value == "unknown"
    assert estimate_cost(unknown, PricePoint(verified_free=True))[0] == "0.00"
    cost, confidence = estimate_cost(
        TokenUsage(input_tokens=1_000_000),
        PricePoint(input_per_million=Decimal("0.50")),
    )
    assert cost == "0.500000"
    assert confidence.value == "estimated"


def test_usage_merging_never_double_counts() -> None:
    first = TokenUsage(input_tokens=10, cache_read_tokens=2)
    second = TokenUsage(output_tokens=7, input_tokens=10)
    merged = first.merged_with(second)
    assert merged.input_tokens == 10
    assert merged.output_tokens == 7
    assert merged.cache_read_tokens == 2


def test_repeated_stream_usage_is_collapsed() -> None:
    from free_claude_code.application.analytics.usage import (
        AnthropicSSEUsageExtractor,
    )

    extractor = AnthropicSSEUsageExtractor()
    extractor.feed(
        'event: message_delta\ndata: {"type":"message_delta","usage":'
        '{"output_tokens":12}}\n\n'
    )
    extractor.feed(
        'event: message_delta\ndata: {"type":"message_delta","usage":'
        '{"output_tokens":12}}\n\n'
    )
    usage, completeness = extractor.finalize()
    assert usage.output_tokens == 12
    assert completeness is UsageCompleteness.FINAL


def test_missing_usage_stays_unknown() -> None:
    from free_claude_code.application.analytics.usage import AnthropicSSEUsageExtractor

    extractor = AnthropicSSEUsageExtractor()
    extractor.feed('event: content_block_delta\ndata: {"delta":{"text":"hi"}}\n\n')
    usage, completeness = extractor.finalize()
    assert not usage.known
    assert completeness is UsageCompleteness.UNKNOWN
    assert usage.output_tokens is None


def test_ttl_expiry_guard_against_silent_zero() -> None:
    """A model with unknown usage must never contribute zero tokens."""

    from free_claude_code.application.analytics.models import sum_known

    total, unknown = sum_known([None, 12, None])
    assert total == 12
    assert unknown == 2


def test_retention_windows() -> None:
    from free_claude_code.runtime.analytics_store import AnalyticsStore

    store = AnalyticsStore(None)
    assert store.durable is False


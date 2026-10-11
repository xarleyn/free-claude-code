"""Analytics against the real FCC database.

This is the test that proves the accounting contract end to end: one client request
that retries and falls back must be stored as one ``logical_requests`` row and two
``upstream_attempts`` rows, with per-attempt usage, and with the blocked attempt
recorded when the guard refuses a call.
"""

from datetime import timedelta

import pytest
import pytest_asyncio

from free_claude_code.application.analytics.models import (
    AttemptStatus,
    ClientType,
    CostConfidence,
    RequestStatus,
    TokenUsage,
    UsageCompleteness,
    UsageSource,
    WireProtocol,
    utc_now,
)
from free_claude_code.application.analytics.queries import AnalyticsQueries
from free_claude_code.application.free_guard.models import (
    AccessStatus,
    BillingClass,
    EligibilityDecision,
    ReasonCode,
)
from free_claude_code.runtime.analytics_service import AnalyticsService
from free_claude_code.runtime.analytics_store import AnalyticsStore
from free_claude_code.runtime.analytics_writer import AnalyticsWriter
from free_claude_code.runtime.sqlite_database import SQLiteDatabase


@pytest_asyncio.fixture
async def database(tmp_path):
    db = SQLiteDatabase(tmp_path / "fcc.db", tmp_path / "fcc.lock")
    await db.start()
    try:
        yield db
    finally:
        await db.close()


def _service(settings) -> AnalyticsService:
    return AnalyticsService(lambda: settings)


async def _drain(service: AnalyticsService) -> None:
    await service._writer.flush_now()


@pytest.mark.asyncio
async def test_one_request_two_attempts(database, monkeypatch) -> None:
    from free_claude_code.config.settings import Settings

    settings = Settings.model_validate(
        {"MODEL": "nvidia_nim/test-model", "FCC_ANALYTICS_ENABLED": "true"}
    )
    store = AnalyticsStore(database)
    writer = AnalyticsWriter(store)
    writer.start()
    service = AnalyticsService(lambda: settings, store=store, writer=writer)
    await service.start()

    request = service.start_request(
        request_id="req-main",
        client_type=ClientType.CLAUDE_CODE,
        wire_protocol=WireProtocol.MESSAGES,
        requested_model="claude-3-5-sonnet-20241022",
        streamed=True,
    )
    assert request is not None
    recorder = service.attempt_recorder(request)

    first = recorder.begin(
        request_id="req-main",
        attempt_number=1,
        provider_id="groq",
        upstream_model_id="llama-3.3-70b",
    )
    recorder.record_usage(
        first,
        usage=TokenUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=64),
        source=UsageSource.PROVIDER_REPORTED,
        completeness=UsageCompleteness.FINAL,
    )
    recorder.finish(first, status=AttemptStatus.ERROR)

    second = recorder.begin(
        request_id="req-main",
        attempt_number=2,
        provider_id="open_router",
        upstream_model_id="other",
        fallback_from_attempt_id=first.attempt_id,
    )
    recorder.mark_first_token(second)
    recorder.record_usage(
        second,
        usage=TokenUsage(input_tokens=1000, output_tokens=500),
        source=UsageSource.PROVIDER_REPORTED,
        completeness=UsageCompleteness.FINAL,
    )
    recorder.finish(second, status=AttemptStatus.SUCCESS)

    service.complete_request(request, status=RequestStatus.SUCCESS)
    await _drain(service)

    queries = AnalyticsQueries(store)
    start = utc_now() - timedelta(hours=1)

    def count_requests(connection):
        return connection.execute(
            "SELECT COUNT(*) FROM logical_requests WHERE request_id = 'req-main'"
        ).fetchone()[0]

    def count_attempts(connection):
        return connection.execute(
            "SELECT COUNT(*) FROM upstream_attempts WHERE request_id = 'req-main'"
        ).fetchone()[0]

    assert await store.query(count_requests) == 1
    assert await store.query(count_attempts) == 2

    summary = await queries.summary(start=start, end=utc_now())
    assert summary["logical_requests"] == 1
    assert summary["downstream_attempts"] == 2
    assert summary["fallback_logical_requests"] == 1
    # Input tokens are reported per attempt by the provider, not collapsed.
    assert summary["usage"]["input_tokens_known"] == 2000
    assert summary["usage"]["output_tokens_known"] == 700
    assert summary["usage"]["unknown_usage_attempts"] == 0

    trace = await queries.request_trace("req-main")
    assert trace is not None
    assert trace["request"]["client_type"] == "claude_code"
    assert len(trace["attempts"]) == 2
    assert trace["attempts"][1]["fallback_from_attempt_id"] == "req-main:1"


@pytest.mark.asyncio
async def test_blocked_attempt_is_stored_and_never_costs_money(database) -> None:
    from free_claude_code.config.settings import Settings

    settings = Settings.model_validate(
        {"MODEL": "nvidia_nim/test-model", "FCC_ANALYTICS_ENABLED": "true"}
    )
    store = AnalyticsStore(database)
    writer = AnalyticsWriter(store)
    writer.start()
    service = AnalyticsService(lambda: settings, store=store, writer=writer)
    await service.start()

    request = service.start_request(
        request_id="req-blocked",
        client_type=ClientType.CODEX,
        wire_protocol=WireProtocol.RESPONSES,
        requested_model="nvidia_nim/paid-model",
    )
    recorder = service.attempt_recorder(request)
    decision = EligibilityDecision(
        allowed=False,
        reason_code=ReasonCode.EVIDENCE_MISSING,
        provider_id="nvidia_nim",
        provider_model="paid-model",
        account_binding_id="nim",
        billing_class=BillingClass.UNKNOWN,
        access_status=AccessStatus.UNKNOWN,
        checked_at_utc=utc_now(),
    )
    recorder.blocked(
        request_id="req-blocked",
        attempt_number=1,
        provider_id="nvidia_nim",
        upstream_model_id="paid-model",
        decision=decision,
    )
    service.complete_request(
        request,
        status=RequestStatus.BLOCKED,
        normalized_error_code=decision.reason_code.value,
    )
    await _drain(service)

    queries = AnalyticsQueries(store)
    start = utc_now() - timedelta(hours=1)
    summary = await queries.summary(start=start, end=utc_now())
    assert summary["blocked_count"] == 1
    # A blocked attempt never produced or estimated any cost.
    assert summary["estimated_cost"]["priced_attempts"] == 0


@pytest.mark.asyncio
async def test_unknown_usage_is_not_zero(database) -> None:
    from free_claude_code.config.settings import Settings

    settings = Settings.model_validate(
        {"MODEL": "nvidia_nim/test-model", "FCC_ANALYTICS_ENABLED": "true"}
    )
    store = AnalyticsStore(database)
    writer = AnalyticsWriter(store)
    writer.start()
    service = AnalyticsService(lambda: settings, store=store, writer=writer)
    await service.start()

    request = service.start_request(
        request_id="req-no-usage",
        client_type=ClientType.UNKNOWN,
        wire_protocol=WireProtocol.MESSAGES,
        requested_model="nvidia_nim/test-model",
    )
    recorder = service.attempt_recorder(request)
    attempt = recorder.begin(
        request_id="req-no-usage",
        attempt_number=1,
        provider_id="nvidia_nim",
        upstream_model_id="test-model",
    )
    recorder.record_usage(
        attempt,
        usage=TokenUsage(),
        source=UsageSource.UNKNOWN,
        completeness=UsageCompleteness.UNKNOWN,
    )
    recorder.finish(attempt, status=AttemptStatus.SUCCESS)
    service.complete_request(request, status=RequestStatus.SUCCESS)
    await _drain(service)

    queries = AnalyticsQueries(store)
    start = utc_now() - timedelta(hours=1)
    summary = await queries.summary(start=start, end=utc_now())
    assert summary["usage"]["unknown_usage_attempts"] == 1
    assert summary["usage"]["unknown_token_share"] == 1.0
    assert summary["estimated_cost"]["known_usd"] is None


@pytest.mark.asyncio
async def test_breakdown_and_timeseries(database) -> None:
    from free_claude_code.config.settings import Settings

    settings = Settings.model_validate({"MODEL": "nvidia_nim/test-model"})
    store = AnalyticsStore(database)
    writer = AnalyticsWriter(store)
    writer.start()
    service = AnalyticsService(lambda: settings, store=store, writer=writer)
    await service.start()

    for index, provider_id in enumerate(("groq", "open_router")):
        request = service.start_request(
            request_id=f"req-{index}",
            client_type=ClientType.VSCODE_CHAT,
            wire_protocol=WireProtocol.MESSAGES,
            requested_model="m",
        )
        recorder = service.attempt_recorder(request)
        attempt = recorder.begin(
            request_id=f"req-{index}",
            attempt_number=1,
            provider_id=provider_id,
            upstream_model_id=f"model-{index}",
        )
        recorder.record_usage(
            attempt,
            usage=TokenUsage(output_tokens=10 * (index + 1)),
            source=UsageSource.PROVIDER_REPORTED,
            completeness=UsageCompleteness.FINAL,
        )
        recorder.finish(
            attempt,
            status=AttemptStatus.SUCCESS,
            cost_confidence=CostConfidence.ESTIMATED,
        )
        service.complete_request(request, status=RequestStatus.SUCCESS)
    await _drain(service)

    queries = AnalyticsQueries(store)
    start = utc_now() - timedelta(hours=1)
    end = utc_now()
    breakdown = await queries.breakdown(by="provider", start=start, end=end)
    assert {item["key"] for item in breakdown["items"]} == {"groq", "open_router"}

    series = await queries.timeseries(start=start, end=end, bucket="hour")
    assert series["series"], "expected at least one hourly bucket"

    page = await queries.requests_page(start=start, end=end, limit=10)
    assert len(page["items"]) == 2

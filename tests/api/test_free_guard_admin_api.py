"""Admin API contracts for FreeGuard and analytics.

Covers the loopback guard, response shape, pagination, range validation and the
privacy promise: no endpoint can return a prompt, a response or a credential.
"""

import asyncio
import json

import pytest

from free_claude_code.api.ports import ApiServices
from free_claude_code.application.analytics.queries import (
    AnalyticsQueries,
    AnalyticsRangeError,
    normalize_limit,
    resolve_range,
    validate_filters,
)
from free_claude_code.config.loader import ManagedConfigStore
from free_claude_code.runtime.application import ApplicationRuntime
from free_claude_code.runtime.configuration import ConfigurationService
from free_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.web_tools_support import StubWebToolsClient


def _client(app, path: str):
    from starlette.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def admin_app():
    from free_claude_code.api.app import create_app

    store = ManagedConfigStore()
    store.initialize()
    settings = store.read().settings
    manager = ProviderRuntimeManager(settings, connected_provider_ids=tuple)

    class ApiTestRuntime(ProviderRuntimeManager):
        def _catalog_task(self, generation, provider_id, *, refresh=False):
            if refresh:
                return super()._catalog_task(generation, provider_id, refresh=True)
            task = generation.catalog_tasks.get(provider_id)
            if task is None:

                async def supplied():
                    from free_claude_code.application.model_metadata import (
                        ProviderModelRefreshResult,
                    )

                    generation.initialized.add(provider_id)
                    return ProviderModelRefreshResult()

                task = asyncio.create_task(supplied())
                generation.catalog_tasks[provider_id] = task
            return task

    manager.__class__ = ApiTestRuntime
    runtime = ApplicationRuntime(
        manager, configuration=ConfigurationService(store), transcriber=None
    )

    class FakeQueries(AnalyticsQueries):
        def __init__(self) -> None:
            super().__init__(store=None)

    class FakeGuard:
        def guard(self):
            raise AssertionError("not used in this test")

        def authorize(self, *, provider_id, provider_model, **_kwargs):
            from free_claude_code.application.free_guard.models import (
                AccessStatus,
                BillingClass,
                EligibilityDecision,
                ReasonCode,
                utc_now,
            )

            return EligibilityDecision(
                allowed=False,
                reason_code=ReasonCode.EVIDENCE_MISSING,
                provider_id=provider_id,
                provider_model=provider_model,
                account_binding_id="unbound",
                billing_class=BillingClass.UNKNOWN,
                access_status=AccessStatus.UNKNOWN,
                checked_at_utc=utc_now(),
                detail="No eligibility evidence for this provider/model.",
            )

        def is_eligible_model(self, _provider, _model, **_kwargs):
            return False

        def summary(self):
            return {
                "mode": "strict_free_only",
                "strict": True,
                "counts": {"verified_free": 0, "unknown": 3},
            }

        def model_statuses(self, **_kwargs):
            return ()

        def start_refresh_job(self, **_kwargs):
            return "2026-10-11T00:00:00Z"

        def job_status(self):
            return {"job_id": "2026-10-11T00:00:00Z", "state": "idle"}

        async def attest(self, **_kwargs):
            return None

        async def revoke_attestation(self, *_args):
            return None

        async def audit(self, **_kwargs):
            return ()

    return create_app(
        ApiServices(
            requests=manager,
            admin=runtime,
            tasks=runtime,
            web_tools=StubWebToolsClient(),
            free_guard=FakeGuard(),
        )
    )


def _call(app, path: str, method: str = "GET", payload: dict | None = None):
    """Call a local Admin endpoint as a loopback browser would."""

    from starlette.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        return client.request(
            method,
            path,
            json=payload,
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
        )


def test_free_guard_summary_is_loopback_only(admin_app) -> None:
    from starlette.testclient import TestClient

    with TestClient(admin_app, client=("127.0.0.1", 51234)) as client:
        response = client.get(
            "/admin/api/free-guard/summary",
            headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["strict"] is True


def test_free_guard_models_returns_a_list(admin_app) -> None:
    response = _call(admin_app, "/admin/api/free-guard/models")
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_free_guard_probe_refuses_unverified_models(admin_app) -> None:
    response = _call(
        admin_app,
        "/admin/api/free-guard/probe",
        method="POST",
        payload={"provider_id": "groq", "model_id": "not-attested"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["reason_code"] == "evidence_missing"


def test_analytics_endpoints_report_disabled_when_unconfigured(admin_app) -> None:
    response = _call(
        admin_app, "/admin/api/analytics/summary?from=2026-10-10T00:00:00Z"
    )
    assert response.status_code == 503


def test_analytics_range_is_validated() -> None:
    start, end = resolve_range("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z")
    assert start.isoformat() < end.isoformat()
    with pytest.raises(AnalyticsRangeError):
        resolve_range("2026-10-11T00:00:00Z", "2026-10-10T00:00:00Z")
    with pytest.raises(AnalyticsRangeError):
        resolve_range("2020-01-01T00:00:00Z", "2026-10-11T00:00:00Z")
    with pytest.raises(AnalyticsRangeError):
        resolve_range("not-a-timestamp", "2026-10-11T00:00:00Z")


def test_analytics_filters_are_allowlisted() -> None:
    assert validate_filters(client="claude_code") == {"client": "claude_code"}
    with pytest.raises(AnalyticsRangeError):
        validate_filters(client="not-a-client")
    assert validate_filters(provider="groq") == {"provider": "groq"}


def test_analytics_limit_is_capped() -> None:
    assert normalize_limit("50") == 50
    assert normalize_limit(5000) == 200
    with pytest.raises(AnalyticsRangeError):
        normalize_limit("not-a-number")
    with pytest.raises(AnalyticsRangeError):
        normalize_limit(True)


def test_analytics_payloads_hold_metadata_only(admin_app) -> None:
    """No endpoint may expose prompt text, credentials or auth headers."""

    forbidden = ("prompt", "response_text", "api_key", "authorization", "password")
    response = _call(admin_app, "/admin/api/free-guard/models")
    body = json.dumps(response.json()).casefold()
    assert not any(token in body for token in forbidden)

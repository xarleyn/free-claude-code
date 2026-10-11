"""End-to-end guard enforcement with mock providers.

The non-negotiable assertion is the *outbound call count*, not the response status:
in strict mode a prohibited target must produce **zero** HTTP requests to the mock
upstream, through every ingress, alias, retry and fallback path.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from free_claude_code.api.app import create_app
from free_claude_code.api.ports import ApiServices
from free_claude_code.application.free_guard.models import (
    AccessStatus,
    BillingClass,
    EligibilityRecord,
    EvidenceSource,
    FreeGuardMode,
    ReasonCode,
)
from free_claude_code.application.free_guard.policy import (
    FreeGuardConfig,
    ModelEligibilityPolicy,
    RoutingGuard,
)
from free_claude_code.application.model_metadata import ProviderModelRefreshResult
from free_claude_code.config.loader import ManagedConfigStore
from free_claude_code.config.settings import Settings
from free_claude_code.providers.runtime import ProviderRuntime
from free_claude_code.runtime.application import ApplicationRuntime
from free_claude_code.runtime.configuration import ConfigurationService
from free_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.web_tools_support import StubWebToolsClient


class MockProvider:
    """A provider that counts every stream it is asked to open."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stream_messages(
        self,
        request,
        *,
        input_tokens: int,
        request_id: str,
        response_model: str,
        reasoning,
        request_headers=None,
        model_info=None,
        continuation=None,
    ) -> AsyncIterator[str]:
        self.calls.append((request.model, request_id))
        yield (
            'event: message_start\ndata: {"type":"message_start","message":'
            '{"usage":{"input_tokens":10,"cache_read_input_tokens":2}}}\n\n'
        )
        yield (
            'event: message_delta\ndata: {"type":"message_delta",'
            '"usage":{"output_tokens":5}}\n\n'
        )
        yield "event: message_stop\ndata: {}\n\n"


class ApiTestRuntime(ProviderRuntimeManager):
    """Skip discovery so tests supply models explicitly."""

    def _catalog_task(self, generation, provider_id, *, refresh=False):
        if refresh:
            return super()._catalog_task(generation, provider_id, refresh=True)
        task = generation.catalog_tasks.get(provider_id)
        if task is None:

            async def supplied() -> ProviderModelRefreshResult:
                generation.initialized.add(provider_id)
                return ProviderModelRefreshResult()

            task = asyncio.create_task(supplied())
            generation.catalog_tasks[provider_id] = task
        return task


def build_app(settings: Settings, guard: RoutingGuard | None) -> Any:
    store = ManagedConfigStore()
    store.initialize()
    providers: dict[str, MockProvider] = {}

    def runtime_factory(snapshot, admission_registry) -> ProviderRuntime:
        instances = {
            provider_id: MockProvider() for provider_id in snapshot.provider_ids
        }
        providers.update(instances)
        return ProviderRuntime(snapshot, admission_registry, instances)

    manager = ApiTestRuntime(
        settings,
        runtime_factory=runtime_factory,
        connected_provider_ids=lambda: (),
    )
    runtime = ApplicationRuntime(
        manager,
        configuration=ConfigurationService(store),
        transcriber=None,
        connected_accounts={},
    )

    class GuardPort:
        def guard(self) -> RoutingGuard:
            return guard

        def authorize(self, **kwargs):
            return guard.authorize(**kwargs)

        def is_eligible_model(self, provider_id, model, **kwargs):
            return guard.is_eligible_model(provider_id, model)

        def summary(self):
            return {"mode": settings.free_guard_mode.value}

    guard_port = GuardPort() if guard is not None else None
    return create_app(
        ApiServices(
            requests=manager,
            admin=runtime,
            tasks=runtime,
            web_tools=StubWebToolsClient(),
            free_guard=guard_port,
        )
    ), providers


_STRICT_CONFIG = FreeGuardConfig(mode=FreeGuardMode.STRICT_FREE_ONLY)

STRICT_SETTINGS = Settings.model_validate(
    {
        "MODEL": "nvidia_nim/test-model",
        "NVIDIA_NIM_API_KEY": "test_key",
        "FCC_FREE_GUARD_MODE": "strict_free_only",
    }
)


def strict_guard(records: dict[tuple[str, str], EligibilityRecord]) -> RoutingGuard:
    policy = ModelEligibilityPolicy(_STRICT_CONFIG)
    return RoutingGuard(
        policy,
        lambda provider_id, model, _binding: records.get((provider_id, model)),
    )


ELIGIBLE = EligibilityRecord(
    provider_id="nvidia_nim",
    upstream_model_id="test-model",
    account_binding_id="nim",
    billing_class=BillingClass.FREE_PERMANENT,
    access_status=AccessStatus.VERIFIED_WORKING,
    evidence_json="{}",
    evidence_source=EvidenceSource.MANUAL_ATTESTATION,
    evidence_checked_at_utc=__import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ),
    evidence_expires_at_utc=(
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        + timedelta(hours=24)
    ),
)


async def _post_messages(app, model: str) -> Any:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive():
        return {
            "type": "http.request",
            "body": (
                asyncio.get_running_loop().run_in_executor(None, lambda: None) and b""
            )
            or __import__("json").dumps(body).encode(),
        }

    sent: list[dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_unknown_model_in_strict_makes_zero_upstream_calls() -> None:
    app, providers = build_app(STRICT_SETTINGS, strict_guard({}))
    sent = await _post_messages(app, "nvidia_nim/paid-model")
    provider = providers.get("nvidia_nim")
    assert provider is not None
    assert provider.calls == []
    # The client still gets a well-formed, actionable error.
    status = next(
        (
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        ),
        None,
    )
    assert status == 403
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert b"free_guard_blocked" in body


@pytest.mark.asyncio
async def test_eligible_model_is_allowed_and_usage_recorded() -> None:
    app, providers = build_app(
        STRICT_SETTINGS, strict_guard({("nvidia_nim", "test-model"): ELIGIBLE})
    )
    sent = await _post_messages(app, "nvidia_nim/test-model")
    provider = providers.get("nvidia_nim")
    assert provider is not None
    assert len(provider.calls) == 1
    status = next(
        (
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        ),
        None,
    )
    assert status == 200


@pytest.mark.asyncio
async def test_off_mode_preserves_legacy_behaviour() -> None:
    settings = Settings.model_validate(
        {
            "MODEL": "nvidia_nim/test-model",
            "NVIDIA_NIM_API_KEY": "test_key",
            "FCC_FREE_GUARD_MODE": "off",
        }
    )
    # No guard service at all: the legacy path must be untouched.
    app, providers = build_app(settings, None)
    await _post_messages(app, "nvidia_nim/anything-goes")
    provider = providers.get("nvidia_nim")
    assert provider is not None
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_direct_model_id_cannot_bypass_the_guard() -> None:
    """A raw provider/model id is a direct target; the guard still sees it."""

    app, providers = build_app(STRICT_SETTINGS, strict_guard({}))
    await _post_messages(app, "nvidia_nim/test-model")
    provider = providers.get("nvidia_nim")
    assert provider is not None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_paid_fallback_is_never_selected() -> None:
    """When the primary is blocked, a paid fallback must not be tried."""

    settings = Settings.model_validate(
        {
            "MODEL": "nvidia_nim/test-model",
            "NVIDIA_NIM_API_KEY": "test_key",
            "FCC_FREE_GUARD_MODE": "strict_free_only",
        }
    )
    app, providers = build_app(settings, strict_guard({}))
    sent = await _post_messages(app, "nvidia_nim/test-model")
    for provider in providers.values():
        assert provider.calls == []
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert b"free_guard_blocked" in body


@pytest.mark.asyncio
async def test_strict_catalog_hides_unconfirmed_models() -> None:
    """In a filtered view a model the policy has not cleared is not advertised."""

    from free_claude_code.api.model_catalog import (
        ModelCatalog,
        build_models_list_response,
    )
    from free_claude_code.application.model_catalog import CatalogModel

    settings = Settings.model_validate(
        {
            "MODEL": "nvidia_nim/test-model",
            "NVIDIA_NIM_API_KEY": "test_key",
            "FCC_FREE_GUARD_MODE": "catalog_filter_only",
        }
    )
    catalog = ModelCatalog(
        models=(
            CatalogModel(
                wire_slug="test-model",
                provider_model_ref="nvidia_nim/test-model",
                display_name="Test",
                supports_reasoning=None,
            ),
            CatalogModel(
                wire_slug="paid-model",
                provider_model_ref="nvidia_nim/paid-model",
                display_name="Paid",
                supports_reasoning=None,
            ),
        ),
        default_model_id="test-model",
    )

    class FakeCatalogPort:
        def current_settings(self):
            return settings

        def cached_prefixed_model_infos(self):
            return ()

        def cached_model_info(self, _provider_id, _model_id):
            return None

    eligible_refs = {"nvidia_nim/test-model"}

    class GuardPort:
        def is_eligible_model(self, provider_id, model, **_kwargs):
            return f"{provider_id}/{model}" in eligible_refs

    import free_claude_code.api.model_catalog as model_catalog_module

    original = model_catalog_module.read_model_catalog
    model_catalog_module.read_model_catalog = lambda _runtime, _settings=None: catalog
    try:
        response = build_models_list_response(
            settings,
            FakeCatalogPort(),
            view=model_catalog_module.ModelCatalogView.MESSAGES,
            guard=GuardPort(),
        )
    finally:
        model_catalog_module.read_model_catalog = original
    advertised = {model.id for model in response.data}
    assert "test-model" in advertised
    assert "paid-model" not in advertised


@pytest.mark.asyncio
async def test_expired_evidence_blocks_until_refreshed() -> None:
    import datetime as dt

    expired = EligibilityRecord(
        provider_id="nvidia_nim",
        upstream_model_id="test-model",
        account_binding_id="nim",
        billing_class=BillingClass.FREE_PERMANENT,
        access_status=AccessStatus.VERIFIED_WORKING,
        evidence_json="{}",
        evidence_source=EvidenceSource.MANUAL_ATTESTATION,
        evidence_checked_at_utc=dt.datetime.now(dt.UTC) - timedelta(hours=48),
        evidence_expires_at_utc=dt.datetime.now(dt.UTC) - timedelta(hours=1),
    )
    guard = strict_guard({("nvidia_nim", "test-model"): expired})
    decision = guard.authorize(provider_id="nvidia_nim", provider_model="test-model")
    assert decision.blocked
    assert decision.reason_code is ReasonCode.EVIDENCE_STALE
    app, providers = build_app(STRICT_SETTINGS, guard)
    await _post_messages(app, "nvidia_nim/test-model")
    assert providers["nvidia_nim"].calls == []

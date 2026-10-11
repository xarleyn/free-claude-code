"""Provider execution shared by inbound API adapters."""

import asyncio
import math
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from time import monotonic
from types import MappingProxyType
from typing import Literal

from loguru import logger

from free_claude_code.application.analytics.models import (
    AttemptStatus,
    ClientType,
    RequestStatus,
    UsageCompleteness,
    UsageSource,
)
from free_claude_code.application.analytics.usage import usage_extractor_for
from free_claude_code.application.free_guard.errors import GuardBlockedError
from free_claude_code.application.free_guard.policy import RoutingGuard
from free_claude_code.core.anthropic import (
    anthropic_request_snapshot,
    get_token_count,
)
from free_claude_code.core.anthropic.tokens import TokenCounter
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.core.openai_responses import (
    OpenAIResponsesRequest,
    estimate_responses_input_tokens,
)
from free_claude_code.core.reasoning import ReasoningPolicy
from free_claude_code.core.request_outcomes import record_request_route
from free_claude_code.core.stream_delivery import current_stream_delivery
from free_claude_code.core.stream_recovery import (
    ContinuationSeed,
    RecoveryAction,
    assess_recovery,
)
from free_claude_code.core.trace import (
    close_stream_input,
    trace_event,
    traced_async_stream,
)

from .ports import ModelInfoLookup, ProviderResolver
from .routing import (
    ProviderModelTarget,
    ResolvedModelRoute,
    RoutedMessagesRequest,
    RoutedNativeMessagesRequest,
    RoutedResponsesRequest,
)

ResponsesTokenCounter = Callable[[OpenAIResponsesRequest], int]
WireApi = Literal["messages", "responses"]
CandidateStreamOpener = Callable[
    [int, ProviderModelTarget, ContinuationSeed | None], Awaitable[AsyncIterator[str]]
]
AnalyticsSink = Callable[[str, str, bool, str | None], object]


def gateway_protocol(wire_api: str) -> str:
    """Map the internal wire name to the analytics wire protocol value."""

    return "messages" if wire_api == "messages" else "responses"


class _NullAttempts:
    """Analytics sink used when no recorder is configured."""

    def begin(self, **_kwargs):
        return None

    def mark_first_token(self, _attempt):
        return None

    def record_usage(self, _attempt, **_kwargs):
        return None

    def finish(self, _attempt, **_kwargs):
        return None

    def blocked(self, **_kwargs):
        return None


_NULL_ATTEMPTS = _NullAttempts()


class ProviderExecutor:
    """Resolve a provider and execute one routed Anthropic Messages stream."""

    def __init__(
        self,
        provider_resolver: ProviderResolver,
        *,
        progress_timeout_seconds: float,
        token_counter: TokenCounter = get_token_count,
        responses_token_counter: ResponsesTokenCounter = estimate_responses_input_tokens,
        generation_id: int | None = None,
        log_raw_payloads: bool = False,
        request_headers: Mapping[str, str] | None = None,
        model_info_lookup: ModelInfoLookup | None = None,
        routing_guard: RoutingGuard | None = None,
        analytics: AnalyticsSink | None = None,
        client_type: ClientType = ClientType.UNKNOWN,
        request_streamed: bool = False,
    ) -> None:
        if not math.isfinite(progress_timeout_seconds) or progress_timeout_seconds <= 0:
            raise ValueError("progress_timeout_seconds must be finite and positive")
        self._provider_resolver = provider_resolver
        self._model_info_lookup = model_info_lookup or (lambda _provider, _model: None)
        self._token_counter = token_counter
        self._responses_token_counter = responses_token_counter
        self._generation_id = generation_id
        self._log_raw_payloads = log_raw_payloads
        self._request_headers = MappingProxyType(dict(request_headers or {}))
        self._progress_timeout_seconds = float(progress_timeout_seconds)
        self._routing_guard = routing_guard
        self._analytics = analytics
        self._client_type = client_type
        self._request_streamed = request_streamed

    def _progress_timeout_failure(
        self,
        *,
        request_id: str,
        provider_id: str,
    ) -> ExecutionFailure:
        trace_event(
            stage="execution",
            event="free_claude_code.provider.progress_timeout",
            source="application",
            request_id=request_id,
            provider_id=provider_id,
            timeout_seconds=self._progress_timeout_seconds,
        )
        timeout_text = f"{self._progress_timeout_seconds:g}"
        return ExecutionFailure(
            kind=FailureKind.TIMEOUT,
            status_code=504,
            message=(
                f"Provider execution made no progress for {timeout_text} seconds.\n\n"
                f"Request ID: {request_id}"
            ),
            retryable=False,
        )

    def _trace_fallback_started(
        self,
        *,
        request_id: str,
        wire_api: WireApi,
        failed: ProviderModelTarget,
        selected: ProviderModelTarget,
        failure: ExecutionFailure,
        candidate_index: int,
        candidate_count: int,
        recovery_action: str,
        recovery_reason: str | None,
    ) -> None:
        fields: dict[str, object] = {
            "stage": "execution",
            "event": "free_claude_code.model_fallback.started",
            "source": "application",
            "request_id": request_id,
            "wire_api": wire_api,
            "from_provider_model_ref": failed.provider_model_ref,
            "to_provider_model_ref": selected.provider_model_ref,
            "candidate_index": candidate_index,
            "candidate_count": candidate_count,
            "failure_kind": failure.kind.value,
            "status_code": failure.status_code,
            "provider_retryable": failure.retryable,
            "recovery_action": recovery_action,
            "recovery_reason": recovery_reason,
        }
        if self._generation_id is not None:
            fields["generation_id"] = self._generation_id
        trace_event(**fields)
        logger.info(
            "Model fallback: request_id={} from={} to={} candidate={}/{} "
            "failure_kind={} status_code={}",
            request_id,
            failed.provider_model_ref,
            selected.provider_model_ref,
            candidate_index,
            candidate_count,
            failure.kind.value,
            failure.status_code,
        )

    def _trace_fallback_selected(
        self,
        *,
        request_id: str,
        wire_api: WireApi,
        selected: ProviderModelTarget,
        candidate_index: int,
        candidate_count: int,
    ) -> None:
        fields: dict[str, object] = {
            "stage": "execution",
            "event": "free_claude_code.model_fallback.selected",
            "source": "application",
            "request_id": request_id,
            "wire_api": wire_api,
            "selected_provider_model_ref": selected.provider_model_ref,
            "candidate_index": candidate_index,
            "candidate_count": candidate_count,
        }
        if self._generation_id is not None:
            fields["generation_id"] = self._generation_id
        trace_event(**fields)

    def set_request_streamed(self, streamed: bool) -> None:
        """Record whether this request streams, for attempt-level accounting."""

        self._request_streamed = bool(streamed)

    def _authorize(self, target: ProviderModelTarget):
        """Re-check one canonical pair immediately before its outbound call.

        Returns ``None`` when no guard is configured, which the caller treats as
        "allowed". Only a configured guard can deny.
        """

        guard = self._routing_guard
        if guard is None:
            return None
        return guard.authorize(
            provider_id=target.provider_id, provider_model=target.provider_model
        )

    def _open_analytics(
        self,
        *,
        request_id: str,
        wire_api: WireApi,
        gateway_model: str | None = None,
    ) -> object | None:
        """Start one logical request for this stream, when analytics is enabled."""

        recorder = self._analytics
        if recorder is None:
            return None
        try:
            return recorder(
                request_id,
                gateway_protocol(wire_api),
                self._request_streamed,
                gateway_model,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Analytics request start failed: exc_type={}", type(exc).__name__
            )
            return None

    @staticmethod
    def _attempt_recorder(analytics: object) -> object:
        if analytics is None:
            return _NULL_ATTEMPTS
        recorder = getattr(analytics, "attempt_recorder", None)
        if recorder is None:
            return _NULL_ATTEMPTS
        try:
            return recorder(analytics) or _NULL_ATTEMPTS
        except Exception:  # pragma: no cover - defensive
            return _NULL_ATTEMPTS

    def _begin_attempt(
        self,
        attempts: object,
        *,
        request_id: str,
        attempt_number: int,
        target: ProviderModelTarget,
        fallback_from_attempt_id: str | None,
    ) -> object | None:
        begin = getattr(attempts, "begin", None)
        if begin is None:
            return None
        try:
            return begin(
                request_id=request_id,
                attempt_number=attempt_number,
                provider_id=target.provider_id,
                upstream_model_id=target.provider_model,
                fallback_from_attempt_id=fallback_from_attempt_id,
                streamed=self._request_streamed,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Analytics attempt start failed: exc_type={}", type(exc).__name__
            )
            return None

    def _record_blocked(
        self,
        attempts: object,
        *,
        request_id: str,
        attempt_number: int,
        target: ProviderModelTarget,
        decision,
    ) -> None:
        blocked = getattr(attempts, "blocked", None)
        if blocked is None:
            return
        try:
            blocked(
                request_id=request_id,
                attempt_number=attempt_number,
                provider_id=target.provider_id,
                upstream_model_id=target.provider_model,
                decision=decision,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Analytics blocked record failed: exc_type={}", type(exc).__name__
            )

    @staticmethod
    def _finish_attempt(
        attempts: object,
        attempt: object | None,
        *,
        status: AttemptStatus,
        usage,
        completeness: UsageCompleteness,
        http_status: int | None,
        normalized_error_code: str | None = None,
    ) -> None:
        if attempt is None:
            return
        record_usage = getattr(attempts, "record_usage", None)
        finish = getattr(attempts, "finish", None)
        try:
            if record_usage is not None:
                record_usage(
                    attempt,
                    usage=usage,
                    source=(
                        UsageSource.PROVIDER_REPORTED
                        if usage.known
                        else UsageSource.UNKNOWN
                    ),
                    completeness=completeness,
                )
            if finish is not None:
                finish(
                    attempt,
                    status=status,
                    upstream_http_status=http_status,
                    normalized_error_code=normalized_error_code,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Analytics attempt finish failed: exc_type={}", type(exc).__name__
            )

    @staticmethod
    def _complete_request(
        analytics: object,
        *,
        status: RequestStatus,
        normalized_error_code: str | None = None,
        provider_id: str | None = None,
        upstream_model_id: str | None = None,
    ) -> None:
        if analytics is None:
            return
        complete = getattr(analytics, "complete_request", None)
        if complete is None:
            return
        try:
            complete(
                analytics,
                status=status,
                normalized_error_code=normalized_error_code,
                provider_id=provider_id,
                upstream_model_id=upstream_model_id,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Analytics request completion failed: exc_type={}",
                type(exc).__name__,
            )

    def stream_native_messages(
        self,
        routed: RoutedNativeMessagesRequest,
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        async def open_candidate(
            index: int,
            target: ProviderModelTarget,
            continuation: ContinuationSeed | None,
        ) -> AsyncIterator[str]:
            provider = await self._provider_resolver(target.provider_id)
            return provider.stream_native_messages(
                routed.request.with_model(target.provider_model),
                request_id=request_id,
                response_model=routed.resolved.original_model,
                request_headers=self._request_headers,
                continuation=continuation,
            )

        messages = routed.request.body["messages"]
        assert isinstance(messages, list)
        return self._stream_candidates(
            resolved=routed.resolved,
            reasoning=ReasoningPolicy.provider_default(),
            wire_api="messages",
            raw_log_label="FULL_NATIVE_MESSAGES_PAYLOAD",
            raw_log_payload=lambda: routed.request.body,
            request_snapshot=lambda: {
                "model": routed.request.model,
                "message_count": len(messages),
                "contract": "native",
            },
            ingress_count_name="message_count",
            ingress_count=len(messages),
            request_id=request_id,
            open_candidate=open_candidate,
        )

    def stream_messages(
        self,
        routed: RoutedMessagesRequest,
        *,
        raw_log_payload: Callable[[], object],
        request_id: str,
    ) -> AsyncIterator[str]:
        """Execute one Anthropic Messages request."""

        primary_request = routed.request.model_copy(deep=True)
        input_tokens = self._token_counter(
            routed.request.messages,
            routed.request.system,
            routed.request.tools,
        )

        async def open_candidate(
            index: int,
            target: ProviderModelTarget,
            continuation: ContinuationSeed | None,
        ) -> AsyncIterator[str]:
            provider = await self._provider_resolver(target.provider_id)
            request = (
                primary_request
                if index == 0
                else routed.request.model_copy(
                    update={"model": target.provider_model},
                    deep=True,
                )
            )
            return provider.stream_messages(
                request,
                input_tokens=input_tokens,
                request_id=request_id,
                response_model=routed.resolved.original_model,
                reasoning=routed.reasoning,
                model_info=self._model_info_lookup(
                    target.provider_id, target.provider_model
                ),
                request_headers=self._request_headers,
                continuation=continuation,
            )

        return self._stream_candidates(
            resolved=routed.resolved,
            reasoning=routed.reasoning,
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload=raw_log_payload,
            request_snapshot=lambda: anthropic_request_snapshot(routed.request),
            ingress_count_name="message_count",
            ingress_count=len(routed.request.messages),
            request_id=request_id,
            open_candidate=open_candidate,
        )

    def stream_responses(
        self,
        routed: RoutedResponsesRequest,
        *,
        raw_log_payload: Callable[[], object],
        request_id: str,
    ) -> AsyncIterator[str]:
        """Execute one native OpenAI Responses request."""

        primary_request = routed.request.model_copy(deep=True)
        input_tokens = self._responses_token_counter(routed.request)

        async def open_candidate(
            index: int,
            target: ProviderModelTarget,
            continuation: ContinuationSeed | None,
        ) -> AsyncIterator[str]:
            provider = await self._provider_resolver(target.provider_id)
            request = (
                primary_request
                if index == 0
                else routed.request.model_copy(
                    update={"model": target.provider_model},
                    deep=True,
                )
            )
            return provider.stream_responses(
                request,
                input_tokens=input_tokens,
                request_id=request_id,
                response_model=routed.resolved.original_model,
                reasoning=routed.reasoning,
                request_headers=self._request_headers,
                continuation=continuation,
                model_info=self._model_info_lookup(
                    target.provider_id, target.provider_model
                ),
            )

        raw_input = routed.request.input
        input_item_count = (
            len(raw_input)
            if isinstance(raw_input, list)
            else int(raw_input is not None)
        )
        return self._stream_candidates(
            resolved=routed.resolved,
            reasoning=routed.reasoning,
            wire_api="responses",
            raw_log_label="FULL_RESPONSES_PAYLOAD",
            raw_log_payload=raw_log_payload,
            request_snapshot=lambda: {
                "model": routed.request.model,
                "input_item_count": input_item_count,
                "tool_count": len(routed.request.tools or ()),
            },
            ingress_count_name="input_item_count",
            ingress_count=input_item_count,
            request_id=request_id,
            open_candidate=open_candidate,
        )

    def _stream_candidates(
        self,
        *,
        resolved: ResolvedModelRoute,
        reasoning: ReasoningPolicy,
        wire_api: WireApi,
        raw_log_label: str,
        raw_log_payload: Callable[[], object],
        request_snapshot: Callable[[], dict[str, object]],
        ingress_count_name: str,
        ingress_count: int,
        request_id: str,
        open_candidate: CandidateStreamOpener,
    ) -> AsyncIterator[str]:
        """Start and consume candidates through one protocol-blind lifecycle."""

        primary = resolved.primary
        candidates = (primary, *resolved.fallbacks)
        gateway_model = resolved.original_model

        # Line of defence 1: never let a blocked model into the candidate list, so a
        # fallback chain cannot silently substitute a paid or unproven model.
        guard = self._routing_guard
        if guard is not None:
            allowed, blocked = guard.filter_targets(candidates, account_binding_id=None)
            if blocked:
                guard.trace_blocked_route(
                    request_id=request_id, blocked=blocked, wire_api=wire_api
                )
                if not allowed:
                    decision = blocked[0][1]
                    analytics = self._open_analytics(
                        request_id=request_id, wire_api=wire_api
                    )
                    attempts = self._attempt_recorder(analytics)
                    self._record_blocked(
                        attempts,
                        request_id=request_id,
                        attempt_number=1,
                        target=blocked[0][0],
                        decision=decision,
                    )
                    self._complete_request(
                        analytics,
                        status=RequestStatus.BLOCKED,
                        normalized_error_code=decision.reason_code.value,
                    )
                    raise GuardBlockedError(decision, decision.detail)
            candidates = tuple(allowed)
            if not candidates:
                raise TypeError("resolved route has no candidates")
        route_trace: dict[str, object] = {
            "stage": "routing",
            "event": "free_claude_code.api.route.resolved",
            "source": "api",
            "request_id": request_id,
            "provider_id": primary.provider_id,
            "provider_model": primary.provider_model,
            "provider_model_ref": primary.provider_model_ref,
            "fallback_count": len(resolved.fallbacks),
            "gateway_model": gateway_model,
            "reasoning_control": reasoning.control.value,
            "reasoning_effort": (
                reasoning.effort.value if reasoning.effort is not None else None
            ),
            "reasoning_budget_tokens": reasoning.budget_tokens,
        }
        if wire_api == "responses":
            route_trace["wire_api"] = "responses"
        if self._generation_id is not None:
            route_trace["generation_id"] = self._generation_id
        trace_event(**route_trace)

        ingress_trace: dict[str, object] = {
            "stage": "ingress",
            "event": (
                "free_claude_code.api.responses.request.received"
                if wire_api == "responses"
                else "free_claude_code.api.request.received"
            ),
            "source": "api",
            "request_id": request_id,
            ingress_count_name: ingress_count,
        }
        trace_event(
            lambda: {"snapshot": {**request_snapshot(), "model": gateway_model}},
            **ingress_trace,
        )

        if self._log_raw_payloads:
            logger.opt(lazy=True).debug(
                f"{raw_log_label} [{{}}]: {{}}", lambda: request_id, raw_log_payload
            )

        analytics = self._open_analytics(
            request_id=request_id, wire_api=wire_api, gateway_model=gateway_model
        )
        attempts = self._attempt_recorder(analytics)

        async def provider_body() -> AsyncIterator[str]:
            loop = asyncio.get_running_loop()
            progress_deadline = loop.time() + self._progress_timeout_seconds
            delivery = current_stream_delivery()
            continuation: ContinuationSeed | None = None
            current_attempt: object | None = None
            last_attempt_id: str | None = None
            for index, target in enumerate(candidates):
                record_request_route(target.provider_id, target.provider_model)
                # Line of defence 2: re-check the exact canonical pair immediately
                # before the outbound call, whatever changed since the route was
                # resolved. This covers direct ids, aliases, retries and fallbacks.
                decision = self._authorize(target)
                if decision is not None and not decision.allowed:
                    self._record_blocked(
                        attempts,
                        request_id=request_id,
                        attempt_number=index + 1,
                        target=target,
                        decision=decision,
                    )
                    raise GuardBlockedError(decision, decision.detail)
                current_attempt = self._begin_attempt(
                    attempts,
                    request_id=request_id,
                    attempt_number=index + 1,
                    target=target,
                    fallback_from_attempt_id=last_attempt_id,
                )
                if current_attempt is not None:
                    last_attempt_id = getattr(current_attempt, "attempt_id", None)
                extractor = usage_extractor_for(wire_api)
                provider_stream: AsyncIterator[str] | None = None
                candidate_committed = False
                candidate_failure: ExecutionFailure | None = None
                try:
                    opening_started = monotonic()
                    try:
                        provider_stream = await open_candidate(
                            index, target, continuation
                        )
                    except ExecutionFailure as failure:
                        candidate_failure = failure
                    finally:
                        # Initialization has its own request budget. Upstream progress
                        # time is not spent waiting for a provider's startup task.
                        progress_deadline += monotonic() - opening_started

                    if provider_stream is None and candidate_failure is None:
                        raise TypeError(
                            "provider stream method must return an async iterator"
                        )
                    while provider_stream is not None:
                        if loop.time() >= progress_deadline:
                            raise self._progress_timeout_failure(
                                request_id=request_id,
                                provider_id=target.provider_id,
                            )
                        progress_timeout = asyncio.timeout_at(progress_deadline)
                        read_failure: ExecutionFailure | None = None
                        try:
                            async with progress_timeout:
                                try:
                                    chunk = await anext(provider_stream)
                                except ExecutionFailure as failure:
                                    read_failure = failure
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            if not progress_timeout.expired():
                                raise
                            raise self._progress_timeout_failure(
                                request_id=request_id,
                                provider_id=target.provider_id,
                            ) from exc
                        if progress_timeout.expired():
                            raise self._progress_timeout_failure(
                                request_id=request_id,
                                provider_id=target.provider_id,
                            )
                        if read_failure is not None:
                            candidate_failure = read_failure
                            break
                        if not chunk:
                            await asyncio.sleep(0)
                            continue
                        if not candidate_committed:
                            candidate_committed = True
                            if index > 0:
                                self._trace_fallback_selected(
                                    request_id=request_id,
                                    wire_api=wire_api,
                                    selected=target,
                                    candidate_index=index + 1,
                                    candidate_count=len(candidates),
                                )
                            if current_attempt is not None:
                                mark_first_token = getattr(
                                    attempts, "mark_first_token", None
                                )
                                if mark_first_token is not None:
                                    mark_first_token(current_attempt)
                        extractor.feed(chunk)
                        yield chunk
                        progress_deadline = loop.time() + self._progress_timeout_seconds
                finally:
                    if provider_stream is not None:
                        active_error = sys.exception()
                        preserved_error = active_error or candidate_failure
                        cleanup_timeout = asyncio.timeout_at(
                            progress_deadline if active_error is None else None
                        )
                        try:
                            async with cleanup_timeout:
                                await close_stream_input(
                                    provider_stream,
                                    owner="provider_executor",
                                    source="api",
                                    preserved_error=preserved_error,
                                )
                        except TimeoutError as exc:
                            if not cleanup_timeout.expired():
                                raise
                            raise self._progress_timeout_failure(
                                request_id=request_id,
                                provider_id=target.provider_id,
                            ) from exc

                usage, completeness = extractor.finalize()
                if candidate_failure is None:
                    self._finish_attempt(
                        attempts,
                        current_attempt,
                        status=AttemptStatus.SUCCESS,
                        usage=usage,
                        completeness=completeness,
                        http_status=None,
                    )
                    self._complete_request(
                        analytics,
                        status=RequestStatus.SUCCESS,
                        provider_id=target.provider_id,
                        upstream_model_id=target.provider_model,
                    )
                    return
                self._finish_attempt(
                    attempts,
                    current_attempt,
                    status=AttemptStatus.ERROR,
                    usage=usage,
                    completeness=completeness,
                    http_status=candidate_failure.status_code,
                    normalized_error_code=candidate_failure.kind.value,
                )
                recovery_action = "continue" if continuation is not None else "restart"
                recovery_reason = "no_candidate_output"
                context = candidate_failure.stream_context
                if delivery is None or context is None:
                    if candidate_committed:
                        raise candidate_failure
                else:
                    assessment = assess_recovery(
                        content_released=delivery.content_released,
                        prefix=delivery.prefix,
                        source=context.source,
                        retryable=candidate_failure.retryable,
                    )
                    if assessment.action is RecoveryAction.STOP:
                        raise candidate_failure
                    if assessment.action is RecoveryAction.HANDOFF:
                        delivery.begin_continuation(handoff=True)
                        self._complete_request(
                            analytics,
                            status=RequestStatus.PARTIAL_STREAM,
                            provider_id=target.provider_id,
                            upstream_model_id=target.provider_model,
                        )
                        return
                    continuation = assessment.seed
                    recovery_action = assessment.action.value
                    recovery_reason = assessment.reason
                if index + 1 >= len(candidates):
                    raise candidate_failure
                if delivery is not None and continuation is not None:
                    delivery.begin_continuation()
                next_target = candidates[index + 1]
                self._trace_fallback_started(
                    request_id=request_id,
                    wire_api=wire_api,
                    failed=target,
                    selected=next_target,
                    failure=candidate_failure,
                    candidate_index=index + 2,
                    candidate_count=len(candidates),
                    recovery_action=recovery_action,
                    recovery_reason=recovery_reason,
                )

        stream_trace: dict[str, object] = {
            "request_id": request_id,
            "initial_provider_id": primary.provider_id,
            "gateway_model": gateway_model,
        }
        if self._generation_id is not None:
            stream_trace["generation_id"] = self._generation_id

        return traced_async_stream(
            provider_body(),
            stage="egress",
            source="api",
            complete_event=(
                "free_claude_code.api.responses.stream_completed"
                if wire_api == "responses"
                else "free_claude_code.api.response.stream_completed"
            ),
            interrupted_event=(
                "free_claude_code.api.responses.stream_interrupted"
                if wire_api == "responses"
                else "free_claude_code.api.response.stream_interrupted"
            ),
            chunk_event=None,
            extra=stream_trace,
        )

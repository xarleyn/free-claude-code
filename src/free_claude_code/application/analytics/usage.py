"""Usage extraction from provider streams.

Provider-reported usage is the only authoritative token accounting. Providers publish
it in several incompatible shapes, and OpenAI-style completion payloads often repeat
it more than once, so this module normalizes them into one :class:`TokenUsage` and
guarantees that a repeated report never double counts.

When a stream ends without any usage report, the result stays ``None`` rather than
``0``: an unknown count must never look like a measured zero.
"""

import json
from dataclasses import dataclass, field
from typing import Protocol

from .models import TokenUsage, UsageCompleteness, UsageSource

_MAX_DATA_BYTES = 64_000


class UsageExtractor(Protocol):
    """One protocol-specific usage reader for a provider stream."""

    def feed(self, chunk: str) -> None: ...

    def finalize(self) -> tuple[TokenUsage, UsageCompleteness]: ...


@dataclass(slots=True)
class _UsageState:
    usage: TokenUsage = field(default_factory=TokenUsage)
    source: UsageSource = UsageSource.UNKNOWN
    reports: int = 0

    def record(self, usage: TokenUsage) -> None:
        # Later reports only fill slots that are still unknown. A provider that
        # re-sends a full usage object therefore cannot double count.
        self.usage = self.usage.merged_with(usage)
        self.source = UsageSource.PROVIDER_REPORTED
        self.reports += 1


class AnthropicSSEUsageExtractor:
    """Read Anthropic Messages usage from ``message_start``/``message_delta``."""

    def __init__(self) -> None:
        self._state = _UsageState()

    def feed(self, chunk: str) -> None:
        for payload in _iter_sse_payloads(chunk):
            if not isinstance(payload, dict):
                continue
            message = payload.get("message")
            if isinstance(message, dict):
                usage = message.get("usage")
                if isinstance(usage, dict):
                    self._state.record(_anthropic_usage(usage))
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self._state.record(_anthropic_usage(usage))

    def finalize(self) -> tuple[TokenUsage, UsageCompleteness]:
        return self._state.usage, (
            UsageCompleteness.FINAL
            if self._state.reports
            else UsageCompleteness.UNKNOWN
        )


class OpenAIResponsesUsageExtractor:
    """Read OpenAI Responses usage from the ``response.completed`` event."""

    def __init__(self) -> None:
        self._state = _UsageState()

    def feed(self, chunk: str) -> None:
        for payload in _iter_sse_payloads(chunk):
            if not isinstance(payload, dict):
                continue
            if payload.get("type") not in ("response.completed", "response.done"):
                continue
            response = payload.get("response")
            if isinstance(response, dict):
                usage = response.get("usage")
                if isinstance(usage, dict):
                    self._state.record(_responses_usage(usage))

    def finalize(self) -> tuple[TokenUsage, UsageCompleteness]:
        return self._state.usage, (
            UsageCompleteness.FINAL
            if self._state.reports
            else UsageCompleteness.UNKNOWN
        )


class OpenAIChatUsageExtractor:
    """Read OpenAI Chat Completions usage from the final streamed chunk."""

    def __init__(self) -> None:
        self._state = _UsageState()

    def feed(self, chunk: str) -> None:
        for payload in _iter_sse_payloads(chunk):
            if not isinstance(payload, dict):
                continue
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self._state.record(_chat_usage(usage))

    def finalize(self) -> tuple[TokenUsage, UsageCompleteness]:
        return self._state.usage, (
            UsageCompleteness.FINAL
            if self._state.reports
            else UsageCompleteness.UNKNOWN
        )


class NullUsageExtractor:
    """Report nothing; used where a stream carries no usage at all."""

    def feed(self, chunk: str) -> None:
        return None

    def finalize(self) -> tuple[TokenUsage, UsageCompleteness]:
        return TokenUsage(), UsageCompleteness.UNKNOWN


def usage_extractor_for(wire_api: str) -> UsageExtractor:
    """Return the extractor matching a client wire protocol."""

    if wire_api == "messages":
        return AnthropicSSEUsageExtractor()
    if wire_api == "responses":
        return OpenAIResponsesUsageExtractor()
    return NullUsageExtractor()


def _iter_sse_payloads(chunk: str) -> list[object]:
    payloads: list[object] = []
    for line in chunk.splitlines():
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        raw = body[:_MAX_DATA_BYTES]
        try:
            payloads.append(json.loads(raw))
        except ValueError, TypeError:
            continue
    return payloads


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _anthropic_usage(usage: dict[object, object]) -> TokenUsage:
    cached = usage.get("cache_read_input_tokens")
    creation = usage.get("cache_creation_input_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    return TokenUsage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        # Cache fields are separate breakdowns reported by the same contract; they
        # are recorded separately and never added on top of input_tokens.
        cache_read_tokens=_int(cached),
        cache_write_tokens=_int(creation),
        reasoning_output_tokens=_int(reasoning),
    )


def _responses_usage(usage: dict[object, object]) -> TokenUsage:
    details = usage.get("input_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
    output_details = usage.get("output_tokens_details")
    reasoning = (
        _int(output_details.get("reasoning_tokens"))
        if isinstance(output_details, dict)
        else None
    )
    return TokenUsage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=cached,
        reasoning_output_tokens=reasoning,
    )


def _chat_usage(usage: dict[object, object]) -> TokenUsage:
    details = usage.get("prompt_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else None
    completion_details = usage.get("completion_tokens_details")
    reasoning = (
        _int(completion_details.get("reasoning_tokens"))
        if isinstance(completion_details, dict)
        else None
    )
    return TokenUsage(
        input_tokens=_int(usage.get("prompt_tokens")),
        output_tokens=_int(usage.get("completion_tokens")),
        cache_read_tokens=cached,
        reasoning_output_tokens=reasoning,
    )

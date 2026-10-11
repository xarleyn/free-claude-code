"""Credential pools: order, cooldowns, and the refusal to rotate when there is nothing to."""

import asyncio

from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.providers.credentials import (
    DEFAULT_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    CredentialPool,
    credential_fingerprint,
    parse_credentials,
)


def _failure(kind: FailureKind, *, retryable: bool = False) -> ExecutionFailure:
    return ExecutionFailure(kind, 429, "spent", retryable)


def test_parse_keeps_order_and_drops_repeats_and_blanks() -> None:
    assert parse_credentials(" a1 , a2\n a1 ,, \r a3; a4 ") == ("a1", "a2", "a3", "a4")


def test_parse_of_a_single_key_is_a_single_credential() -> None:
    assert parse_credentials(" only-key ") == ("only-key",)
    assert parse_credentials(None) == ()
    assert parse_credentials("   ") == ()


def test_a_single_credential_pool_never_rotates() -> None:
    pool = CredentialPool.for_provider("open_router", ["only"])

    async def rotate() -> str | None:
        return await pool.rotate_to_next(credential_exhausted=True)

    assert asyncio.run(rotate()) is None
    assert pool.current == "only"
    assert not pool.rotates


def test_rotation_uses_the_next_key_and_cools_the_failed_one() -> None:
    pool = CredentialPool.for_provider("open_router", ["k1", "k2", "k3"])

    async def scenario() -> tuple[object, object, object]:
        first = await pool.rotate_to_next(
            credential_exhausted=True, retry_after_seconds=30, now=1000.0
        )
        second = await pool.rotate_to_next(credential_exhausted=True, now=1000.0)
        state = pool.describe(now=1000.0)
        return (first, second, state)

    first, second, state = asyncio.run(scenario())

    assert first == "k2"
    assert second == "k3"
    assert [row["fingerprint"] for row in state] == [
        credential_fingerprint(key) for key in ("k1", "k2", "k3")
    ]
    assert state[0]["cooling_down_seconds"] == 30.0
    assert state[1]["cooling_down_seconds"] == DEFAULT_COOLDOWN_SECONDS
    assert state[2]["active"] is True
    assert state[0]["consecutive_failures"] == 1
    assert all("k1" not in str(row) for row in state)


def test_a_model_or_request_failure_does_not_burn_a_credential() -> None:
    pool = CredentialPool.for_provider("open_router", ["k1", "k2"])

    async def scenario() -> tuple[object, object]:
        refused = await pool.rotate_to_next(credential_exhausted=False, now=1000.0)
        return (refused, pool.describe(now=1000.0))

    refused, state = asyncio.run(scenario())

    assert refused is None
    assert pool.current == "k1"
    assert all(row["cooling_down_seconds"] == 0.0 for row in state)


def test_rotation_stops_when_every_credential_is_cooling() -> None:
    pool = CredentialPool.for_provider("open_router", ["k1", "k2"])

    async def scenario() -> list[object]:
        return [
            await pool.rotate_to_next(
                credential_exhausted=True,
                retry_after_seconds=10,
                now=1000.0 + index,
            )
            for index in range(4)
        ]

    outcomes = asyncio.run(scenario())

    # k1 -> k2, then nothing free left, then nothing free left again.
    assert outcomes[0] == "k2"
    assert outcomes[1] is None


def test_a_long_retry_after_is_capped() -> None:
    pool = CredentialPool.for_provider("open_router", ["k1", "k2"])

    async def scenario() -> object:
        await pool.rotate_to_next(
            credential_exhausted=True,
            retry_after_seconds=10_000,
            now=1000.0,
        )
        return pool.describe(now=1000.0)[0]["cooling_down_seconds"]

    assert asyncio.run(scenario()) == MAX_COOLDOWN_SECONDS


def test_success_clears_every_cooldown() -> None:
    pool = CredentialPool.for_provider("open_router", ["k1", "k2"])

    async def scenario() -> tuple[object, object]:
        await pool.rotate_to_next(
            credential_exhausted=True, retry_after_seconds=99, now=1000.0
        )
        pool.succeeded()
        return (pool.healthy_count(now=1000.0), pool.current)

    healthy, current = asyncio.run(scenario())

    assert healthy == 2
    assert current == "k2"

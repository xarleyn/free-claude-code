"""Credential rotation inside one provider attempt ladder: seamless, and bounded."""

import pytest

from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.providers.admission import (
    ProviderAdmissionController,
    ProviderOperationKind,
)


def _controller(keys: list[str]) -> ProviderAdmissionController:
    controller = ProviderAdmissionController(
        provider_name="open_router",
        rate_limit=1000,
        rate_window=60,
        max_concurrency=5,
        base_delay=0,
        max_delay=0,
        jitter=0,
    )
    controller.attach_credentials(keys)
    return controller


async def _run(controller: ProviderAdmissionController, call):
    return await controller.start_execution().run_call(
        call, operation_kind=ProviderOperationKind.GENERATION
    )


@pytest.mark.asyncio
async def test_a_spent_credential_moves_to_the_next_account_without_changing_model() -> (
    None
):
    controller = _controller(["k1", "k2", "k3"])
    used: list[str] = []

    async def call() -> str:
        key = await controller.credentials.credential()
        used.append(key)
        if len(used) == 1:
            raise ExecutionFailure(FailureKind.RATE_LIMIT, 429, "quota spent", False)
        return "answered"

    assert await _run(controller, call) == "answered"
    assert used == ["k1", "k2"]
    assert controller.credentials.current == "k2"


@pytest.mark.asyncio
async def test_a_payment_wall_on_the_account_also_rotates() -> None:
    controller = _controller(["k1", "k2"])
    used: list[str] = []

    async def call() -> str:
        used.append(await controller.credentials.credential())
        if len(used) == 1:
            raise ExecutionFailure(FailureKind.PERMISSION, 402, "no credits", False)
        return "answered"

    assert await _run(controller, call) == "answered"
    assert used == ["k1", "k2"]


@pytest.mark.asyncio
async def test_a_model_failure_does_not_burn_the_next_account() -> None:
    controller = _controller(["k1", "k2"])
    used: list[str] = []

    async def call() -> str:
        used.append(await controller.credentials.credential())
        raise ExecutionFailure(
            FailureKind.CONTEXT_WINDOW_EXCEEDED, 400, "too big", False
        )

    with pytest.raises(ExecutionFailure):
        await _run(controller, call)

    assert used == ["k1"]
    assert controller.credentials.current == "k1"
    assert all(
        row["cooling_down_seconds"] == 0.0 for row in controller.credentials.describe()
    )


@pytest.mark.asyncio
async def test_one_account_is_not_a_pool() -> None:
    controller = _controller(["only"])
    used: list[str] = []

    async def call() -> str:
        used.append(await controller.credentials.credential())
        raise ExecutionFailure(FailureKind.RATE_LIMIT, 429, "quota spent", False)

    with pytest.raises(ExecutionFailure):
        await _run(controller, call)

    assert used == ["only"]


@pytest.mark.asyncio
async def test_rotation_stops_once_every_account_is_spent() -> None:
    controller = _controller(["k1", "k2"])
    used: list[str] = []

    async def call() -> str:
        used.append(await controller.credentials.credential())
        raise ExecutionFailure(FailureKind.RATE_LIMIT, 429, "quota spent", False)

    with pytest.raises(ExecutionFailure):
        await _run(controller, call)

    # k1 spent, then k2 spent, and the attempt budget ends the ladder.
    assert used == ["k1", "k2"]
    assert controller.credentials.current in {"k1", "k2"}


@pytest.mark.asyncio
async def test_an_accepted_response_clears_the_spent_accounts_cooldown() -> None:
    controller = _controller(["k1", "k2"])
    spent = {"k1"}

    async def call() -> str:
        key = await controller.credentials.credential()
        if key in spent:
            raise ExecutionFailure(FailureKind.RATE_LIMIT, 429, "quota spent", False)
        return "answered"

    assert await _run(controller, call) == "answered"
    assert controller.credentials.current == "k2"
    # The accepted response proves the pool is usable again, so k1 is not held back.
    assert controller.credentials.healthy_count() == 2
    assert all(
        row["cooling_down_seconds"] == 0.0 for row in controller.credentials.describe()
    )

"""Several accounts in one provider setting: parsing, and the pool behind client keys."""

import asyncio

from free_claude_code.config.provider_catalog import PROVIDER_CATALOG
from free_claude_code.config.settings import Settings
from free_claude_code.core.failures import ExecutionFailure, FailureKind
from free_claude_code.providers.admission import ProviderAdmissionController
from free_claude_code.providers.base import ProviderConfig
from free_claude_code.providers.runtime.config import (
    build_provider_config,
    has_provider_configuration,
    provider_credential,
    provider_credentials,
)
from free_claude_code.providers.runtime.factory import share_credentials


def _settings(raw: str) -> Settings:
    return Settings.model_construct(open_router_api_key=raw)


def _rate_limit() -> ExecutionFailure:
    return ExecutionFailure(FailureKind.RATE_LIMIT, 429, "quota spent", False)


def test_one_setting_carries_an_ordered_credential_pool() -> None:
    descriptor = PROVIDER_CATALOG["open_router"]
    settings = _settings("sk-second, sk-first , sk-second ,\nsk-third")

    assert provider_credentials(descriptor, settings) == (
        "sk-second",
        "sk-first",
        "sk-third",
    )
    # The key a client starts on is the first configured account, not the whole value.
    assert provider_credential(descriptor, settings) == "sk-second"
    config = build_provider_config(descriptor, settings)
    assert config.api_key == "sk-second"
    assert config.api_keys == ("sk-second", "sk-first", "sk-third")
    assert has_provider_configuration(descriptor, settings)


def test_a_single_account_stays_a_single_credential() -> None:
    descriptor = PROVIDER_CATALOG["open_router"]
    config = build_provider_config(descriptor, _settings(" sk-alone "))

    assert config.api_key == "sk-alone"
    assert config.api_keys == ("sk-alone",)
    # One account is left exactly as it was: no pool indirection in the client.
    admission = ProviderAdmissionController(provider_name="open_router")
    assert share_credentials(config, admission).credential_source is None


def test_clients_ask_the_pool_for_every_attempt() -> None:
    descriptor = PROVIDER_CATALOG["open_router"]
    config = build_provider_config(descriptor, _settings("k1,k2"))
    admission = ProviderAdmissionController(provider_name="open_router")

    shared = share_credentials(config, admission)
    source = shared.credential_source
    assert source is not None

    assert asyncio.run(source()) == "k1"
    asyncio.run(
        admission.credentials.rotate_to_next(credential_exhausted=True, now=1_000.0)
    )
    # A spent account moves the credential every later client call signs with.
    assert asyncio.run(source()) == "k2"


def test_a_provider_without_credentials_gets_no_credential_source() -> None:
    config = ProviderConfig(
        api_key=None,
        base_url="http://127.0.0.1:11434",
        http_read_timeout=1,
        http_write_timeout=1,
        http_connect_timeout=1,
        proxy=None,
        log_raw_sse_events=False,
        log_api_error_tracebacks=False,
        api_keys=(),
    )
    admission = ProviderAdmissionController(provider_name="ollama")

    assert share_credentials(config, admission).credential_source is None

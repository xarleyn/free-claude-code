"""Runtime capabilities consumed by the HTTP API adapter."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from free_claude_code.application.analytics.ports import AnalyticsRecorder
from free_claude_code.application.analytics.queries import AnalyticsQueries
from free_claude_code.application.code_sessions import CodeApplicationPort
from free_claude_code.application.connected_accounts import (
    ConnectedAccountLoginMode,
    ConnectedAccountStatus,
)
from free_claude_code.application.free_guard.models import (
    EligibilityDecision,
    EligibilityRecord,
)
from free_claude_code.application.free_guard.policy import RoutingGuard
from free_claude_code.application.model_metadata import ProviderModelRefreshResult
from free_claude_code.application.ports import RequestRuntimePort, TaskController
from free_claude_code.application.web_tools.ports import WebToolsPort
from free_claude_code.config.admin.custom_providers import CustomProviderMutation
from free_claude_code.config.admin.state import ConfigInputValue, ValueState
from free_claude_code.core.json_types import JsonObject


class AdminRuntimePort(Protocol):
    """Runtime operations exposed by the local Admin API."""

    async def apply_admin_config(
        self,
        updates: Mapping[str, ConfigInputValue],
        custom_provider: CustomProviderMutation | None = None,
    ) -> JsonObject: ...

    async def admin_config(self) -> JsonObject: ...

    async def admin_values(self) -> ValueState: ...

    async def admin_status(self) -> JsonObject: ...

    async def claude_vscode_status(self) -> JsonObject: ...

    async def vscode_chat_status(self) -> JsonObject: ...

    async def connect_vscode_chat(self) -> JsonObject: ...

    async def disconnect_vscode_chat(self) -> JsonObject: ...

    async def refresh_vscode_chat(self) -> JsonObject: ...

    async def connect_claude_vscode(self) -> JsonObject: ...

    async def disconnect_claude_vscode(self) -> JsonObject: ...

    async def refresh_claude_vscode(self) -> JsonObject: ...

    async def dsh_desktop_status(self) -> JsonObject: ...

    async def connect_dsh_desktop(self) -> JsonObject: ...

    async def disconnect_dsh_desktop(self) -> JsonObject: ...

    async def refresh_dsh_desktop(self) -> JsonObject: ...

    async def claude_desktop_status(self) -> JsonObject: ...

    async def disconnect_claude_desktop(self) -> JsonObject: ...

    async def refresh_claude_desktop(self) -> JsonObject: ...

    async def jetbrains_acp_status(self) -> JsonObject: ...

    async def connect_jetbrains_acp(self) -> JsonObject: ...

    async def disconnect_jetbrains_acp(self) -> JsonObject: ...

    async def refresh_jetbrains_acp(self) -> JsonObject: ...

    async def connect_claude_desktop(self) -> JsonObject: ...

    async def codex_integration_status(self) -> JsonObject: ...

    async def connect_codex(self) -> JsonObject: ...

    async def disconnect_codex(self) -> JsonObject: ...

    async def refresh_codex_integration(self) -> JsonObject: ...

    async def pick_folder(self, initial_path: str | None) -> str | None: ...

    async def test_provider(self, provider_id: str) -> JsonObject: ...

    async def refresh_models(self) -> ProviderModelRefreshResult: ...

    async def connected_account_status(
        self, provider_id: str
    ) -> ConnectedAccountStatus: ...

    async def start_connected_account_login(
        self, provider_id: str, mode: ConnectedAccountLoginMode
    ) -> ConnectedAccountStatus: ...

    async def cancel_connected_account_login(
        self, provider_id: str
    ) -> ConnectedAccountStatus: ...

    async def disconnect_connected_account(
        self, provider_id: str
    ) -> ConnectedAccountStatus: ...


class FreeGuardPort(Protocol):
    """Free-only routing guard shared by every ingress."""

    def guard(self) -> RoutingGuard: ...

    def authorize(
        self,
        *,
        provider_id: str,
        provider_model: str,
        account_binding_id: str | None = None,
    ) -> EligibilityDecision: ...

    def is_eligible_model(
        self,
        provider_id: str,
        provider_model: str,
        *,
        account_binding_id: str | None = None,
    ) -> bool: ...

    def summary(self) -> JsonObject: ...

    def model_statuses(
        self,
        *,
        include_blocked: bool = True,
        provider_id: str | None = None,
        query: str | None = None,
        billing_class: str | None = None,
        access_status: str | None = None,
        account_binding_id: str | None = None,
    ) -> Sequence[object]: ...

    def start_refresh_job(self, *, provider_ids: Sequence[str]) -> str: ...

    def job_status(self) -> Mapping[str, object]: ...

    async def attest(
        self,
        *,
        provider_id: str,
        upstream_model_id: str,
        source_url: str,
        note: str = "",
        by: str = "admin",
    ) -> EligibilityRecord | None: ...

    async def revoke_attestation(
        self, provider_id: str, upstream_model_id: str
    ) -> None: ...

    async def audit(self, *, limit: int = 200) -> Sequence[Mapping[str, object]]: ...


class AnalyticsAdminPort(Protocol):
    """Admin-facing surface of local usage analytics."""

    @property
    def enabled(self) -> bool: ...

    @property
    def queries(self) -> AnalyticsQueries | None: ...

    def stats(self) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class ApiServices:
    """Complete runtime boundary required to construct the API application."""

    requests: RequestRuntimePort
    admin: AdminRuntimePort
    tasks: TaskController
    web_tools: WebToolsPort
    code: CodeApplicationPort | None = None
    #: Free-only routing guard. Optional so existing compositions keep working; when
    #: absent, no free-only restriction is applied.
    free_guard: FreeGuardPort | None = None
    #: Local usage analytics. Optional; when absent, nothing is recorded.
    analytics: AnalyticsRecorder | AnalyticsAdminPort | None = None

    async def ensure_services(self) -> None:
        """Start shared features after the runtime has settled.

        Called from the request path, never from startup, so the Code service owns the
        shared database writer first. Idempotent and safe under concurrency.
        """

        ensure = getattr(self.admin, "_lazy_start_services", None)
        if ensure is not None:
            await ensure()

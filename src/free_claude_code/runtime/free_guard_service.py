"""FreeGuard runtime service: the object the request path calls.

Responsibilities:

* hold one :class:`RoutingGuard` whose evidence lookup is a synchronous in-memory
  snapshot, so a guard check costs no I/O and can never block the event loop;
* reload that snapshot when settings change, on an explicit Admin refresh, and on
  startup;
* turn provider failures into durable evidence (a purchase lock must never be
  forgotten);
* expose the read models the Admin API renders.
"""

import asyncio
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from loguru import logger

from free_claude_code.application.free_guard.account import account_binding_id
from free_claude_code.application.free_guard.adapters import (
    EligibilityAdapterRegistry,
    classify_provider_error,
    eligibility_reason_labels,
    known_provider_ids,
)
from free_claude_code.application.free_guard.errors import guard_message
from free_claude_code.application.free_guard.models import (
    AccessStatus,
    BillingClass,
    EligibilityDecision,
    EligibilityRecord,
    EvidenceSource,
    FreeGuardMode,
    PricingEvidence,
    ReasonCode,
    utc_now,
    utc_text,
)
from free_claude_code.application.free_guard.policy import (
    FreeGuardConfig,
    ModelEligibilityPolicy,
    RoutingGuard,
    free_guard_config,
    record_from_evidence,
)
from free_claude_code.application.routing import ProviderModelTarget
from free_claude_code.config.settings import Settings
from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.trace import trace_event

from .free_guard_store import EligibilityStore

_RefreshResult = "FreeGuardRefreshResult"


@dataclass(frozen=True, slots=True)
class FreeGuardRefreshResult:
    """Outcome of one explicit price/eligibility refresh."""

    provider_ids: tuple[str, ...]
    checked_models: int
    eligible_models: int
    newly_blocked: int
    failed_provider_ids: tuple[str, ...]
    started_at_utc: datetime
    finished_at_utc: datetime

    @property
    def ok(self) -> bool:
        return not self.failed_provider_ids


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """One row of the Admin Free Models table."""

    provider_id: str
    upstream_model_id: str
    account_binding_id: str
    billing_class: BillingClass
    access_status: AccessStatus
    eligible: bool
    available_now: bool
    reason_code: ReasonCode
    reason_label: str
    detail: str
    evidence_source: EvidenceSource | None
    evidence_checked_at_utc: str | None
    evidence_expires_at_utc: str | None
    last_probe_at_utc: str | None
    manually_approved: bool
    manual_attestation_url: str | None
    manual_attestation_by: str | None
    manual_attestation_at_utc: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "upstream_model_id": self.upstream_model_id,
            "account_binding_id": self.account_binding_id,
            "billing_class": self.billing_class.value,
            "access_status": self.access_status.value,
            "eligible": self.eligible,
            "available_now": self.available_now,
            "reason_code": self.reason_code.value,
            "reason_label": self.reason_label,
            "detail": self.detail,
            "evidence_source": (
                self.evidence_source.value if self.evidence_source is not None else None
            ),
            "evidence_checked_at_utc": self.evidence_checked_at_utc,
            "evidence_expires_at_utc": self.evidence_expires_at_utc,
            "last_probe_at_utc": self.last_probe_at_utc,
            "manually_approved": self.manually_approved,
            "manual_attestation_url": self.manual_attestation_url,
            "manual_attestation_by": self.manual_attestation_by,
            "manual_attestation_at_utc": self.manual_attestation_at_utc,
        }


class FreeGuardService:
    """One guard shared by every ingress; reloaded, never rebuilt per request."""

    def __init__(
        self,
        settings_provider,
        *,
        store: EligibilityStore | None = None,
        adapters: EligibilityAdapterRegistry | None = None,
        model_inventory: ModelInventoryProvider | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._store = store or EligibilityStore(None)
        self._adapters = adapters or EligibilityAdapterRegistry()
        self._model_inventory = model_inventory
        self._records: dict[tuple[str, str, str], EligibilityRecord] = {}
        self._reload_lock = asyncio.Lock()
        self._loaded = False
        self._refresh_task: asyncio.Task[FreeGuardRefreshResult] | None = None
        self._last_refresh: FreeGuardRefreshResult | None = None
        self._last_refresh_started_monotonic: float | None = None
        self._last_reload_at: float = 0.0
        self._reload_settings_id: int | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def durable(self) -> bool:
        """Whether eligibility evidence survives a restart."""

        return self._store.durable

    async def start(self) -> None:
        """Load evidence once; a failure leaves the snapshot empty (deny-all)."""

        async with self._reload_lock:
            await self._reload("startup")

    async def close(self) -> None:
        """Nothing to release: the database is owned by the application runtime."""

        self._records = {}
        self._loaded = False

    # ------------------------------------------------------------------ guard

    @property
    def config(self) -> FreeGuardConfig:
        return free_guard_config(self._settings_provider())

    @property
    def mode(self) -> FreeGuardMode:
        return self.config.mode

    def guard(self) -> RoutingGuard:
        """Return one guard bound to the current settings and evidence snapshot."""

        settings = self._settings_provider()
        policy = ModelEligibilityPolicy(free_guard_config(settings))

        def lookup(
            provider_id: str, upstream_model_id: str, binding: str
        ) -> EligibilityRecord | None:
            return self._records.get((provider_id, upstream_model_id, binding))

        def binding_for(provider_id: str) -> str:
            return account_binding_id(provider_id, settings)

        return RoutingGuard(policy, lookup, account_binding_id=binding_for)

    def authorize(
        self,
        *,
        provider_id: str,
        provider_model: str,
        account_binding_id: str | None = None,
        now: datetime | None = None,
    ) -> EligibilityDecision:
        """Return one verdict for a canonical pair."""

        return self.guard().authorize(
            provider_id=provider_id,
            provider_model=provider_model,
            account_binding_id=account_binding_id,
            now=now,
        )

    def filter_targets(
        self,
        targets: Iterable[ProviderModelTarget],
        *,
        account_binding_id: str | None = None,
    ) -> tuple[
        list[ProviderModelTarget], list[tuple[ProviderModelTarget, EligibilityDecision]]
    ]:
        """Split candidate targets into allowed and blocked ones."""

        settings = self._settings_provider()
        guard = self.guard()
        binding = account_binding_id or account_binding_id
        allowed: list[ProviderModelTarget] = []
        blocked: list[tuple[ProviderModelTarget, EligibilityDecision]] = []
        for target in targets:
            decision = guard.authorize(
                provider_id=target.provider_id,
                provider_model=target.provider_model,
                account_binding_id=(
                    binding
                    if binding is not None
                    else account_binding_id_for(target.provider_id, settings)
                ),
            )
            if decision.allowed:
                allowed.append(target)
            else:
                blocked.append((target, decision))
        return allowed, blocked

    def maybe_reload(self) -> None:
        """Reload evidence when the settings generation changed (hot reload)."""

        settings = self._settings_provider()
        marker = id(settings)
        if self._reload_settings_id == marker:
            return
        self._reload_settings_id = marker
        if not self._loaded:
            return
        task = asyncio.get_running_loop().create_task(self._reload("settings_change"))
        task.add_done_callback(_log_task_result)

    def blocked_message(self, decision: EligibilityDecision) -> str:
        """Client-safe explanation for one denial."""

        return guard_message(decision)

    def is_eligible_model(
        self,
        provider_id: str,
        provider_model: str,
        *,
        account_binding_id: str | None = None,
    ) -> bool:
        """Whether the canonical pair may be advertised in a filtered catalog."""

        return self.authorize(
            provider_id=provider_id,
            provider_model=provider_model,
            account_binding_id=account_binding_id,
        ).allowed

    # -------------------------------------------------------------- refresh

    async def refresh(
        self,
        *,
        provider_ids: Sequence[str] | None = None,
        reason: str = "admin_refresh",
    ) -> FreeGuardRefreshResult:
        """Re-check prices and eligibility for the requested providers."""

        started = utc_now()
        started_monotonic = time.monotonic()
        settings = self._settings_provider()
        targets = list(provider_ids) if provider_ids else self._target_providers()
        checked = 0
        eligible = 0
        newly_blocked = 0
        failed: list[str] = []
        for provider_id in targets:
            try:
                records = await self._collect_provider_evidence(provider_id, settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "FreeGuard eligibility refresh failed: provider={} exc_type={}",
                    provider_id,
                    type(exc).__name__,
                )
                failed.append(provider_id)
                continue
            for record in records:
                previous = self._records.get(record.key)
                checked += 1
                try:
                    self._records[record.key] = record
                    await self._store.save(record)
                except Exception as exc:
                    logger.warning(
                        "FreeGuard evidence persistence failed: provider={} exc_type={}",
                        provider_id,
                        type(exc).__name__,
                    )
                decision = self.authorize(
                    provider_id=record.provider_id,
                    provider_model=record.upstream_model_id,
                    account_binding_id=record.account_binding_id,
                    now=started,
                )
                if decision.allowed:
                    eligible += 1
                elif (
                    previous is None
                    or previous.billing_class is not record.billing_class
                ):
                    newly_blocked += 1
        result = FreeGuardRefreshResult(
            provider_ids=tuple(targets),
            checked_models=checked,
            eligible_models=eligible,
            newly_blocked=newly_blocked,
            failed_provider_ids=tuple(failed),
            started_at_utc=started,
            finished_at_utc=utc_now(),
        )
        self._last_refresh = result
        self._last_refresh_started_monotonic = started_monotonic
        trace_event(
            stage="runtime",
            event="free_claude_code.free_guard.refresh_completed",
            source="runtime",
            reason=reason,
            provider_ids=list(targets),
            checked_models=checked,
            eligible_models=eligible,
            newly_blocked=newly_blocked,
            failed_provider_ids=list(failed),
            duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        )
        return result

    def start_refresh_job(self, *, provider_ids: Sequence[str] | None = None) -> str:
        """Start a non-blocking refresh and return its job id."""

        if self._refresh_task is not None and not self._refresh_task.done():
            return self._job_id()
        task = asyncio.get_running_loop().create_task(
            self.refresh(provider_ids=provider_ids, reason="admin_job")
        )
        self._refresh_task = task
        return self._job_id()

    def job_id(self) -> str:
        return self._job_id()

    def job_status(self) -> dict[str, object]:
        task = self._refresh_task
        if task is None:
            return {"job_id": self._job_id(), "state": "idle"}
        if task.done():
            state = (
                "failed" if task.cancelled() or task.exception() is not None else "done"
            )
        else:
            state = "running"
        last = self._last_refresh
        return {
            "job_id": self._job_id(),
            "state": state,
            "provider_ids": list(last.provider_ids) if last is not None else [],
            "checked_models": last.checked_models if last is not None else 0,
            "eligible_models": last.eligible_models if last is not None else 0,
            "newly_blocked": last.newly_blocked if last is not None else 0,
            "failed_provider_ids": (
                list(last.failed_provider_ids) if last is not None else []
            ),
            "started_at_utc": utc_text(last.started_at_utc)
            if last is not None
            else None,
            "finished_at_utc": (
                utc_text(last.finished_at_utc) if last is not None else None
            ),
        }

    def _job_id(self) -> str:
        if self._last_refresh is None:
            return "pending"
        return utc_text(self._last_refresh.started_at_utc)

    # --------------------------------------------------------------- evidence

    def record_provider_error(
        self,
        *,
        provider_id: str,
        upstream_model_id: str,
        status: int,
        payload: object,
    ) -> EligibilityDecision:
        """Persist a provider failure as evidence and return the new verdict."""

        settings = self._settings_provider()
        binding = account_binding_id(provider_id, settings)
        normalized = classify_provider_error(status, payload)
        record = self._records.get((provider_id, upstream_model_id, binding))
        base = record or EligibilityRecord(
            provider_id=provider_id,
            upstream_model_id=upstream_model_id,
            account_binding_id=binding,
        )
        if normalized.access_status is AccessStatus.PAYMENT_LOCKED:
            next_record = replace(
                base,
                billing_class=BillingClass.REQUIRES_PURCHASE,
                access_status=AccessStatus.PAYMENT_LOCKED,
                requires_topup=True,
                manually_approved=False,
                blocked_reason_code=ReasonCode.PAYMENT_LOCKED,
                evidence_source=EvidenceSource.ACCOUNT_SIGNAL,
                evidence_checked_at_utc=utc_now(),
                evidence_expires_at_utc=utc_now() + self.config.ttl,
                evidence_json=base.evidence_json,
            )
        elif normalized.access_status in {
            AccessStatus.AUTH_REQUIRED,
            AccessStatus.REGION_BLOCKED,
            AccessStatus.INVALID_MODEL,
        }:
            next_record = replace(
                base,
                access_status=normalized.access_status,
                blocked_reason_code=ReasonCode.ACCESS_BLOCKED,
                evidence_source=EvidenceSource.ACCOUNT_SIGNAL,
                evidence_checked_at_utc=utc_now(),
                evidence_expires_at_utc=utc_now() + self.config.ttl,
            )
        else:
            # A genuine rate limit or a transient outage does not make a model paid.
            next_record = replace(
                base,
                access_status=normalized.access_status or base.access_status,
                evidence_checked_at_utc=utc_now(),
                evidence_expires_at_utc=utc_now() + self.config.ttl,
            )
        self._records[next_record.key] = next_record
        self._persist(next_record)
        return self.authorize(
            provider_id=provider_id,
            provider_model=upstream_model_id,
            account_binding_id=binding,
        )

    def mark_probe_success(
        self, provider_id: str, upstream_model_id: str
    ) -> EligibilityDecision:
        """Record liveness. Never converts an unknown model into a free one."""

        settings = self._settings_provider()
        binding = account_binding_id(provider_id, settings)
        record = self._records.get((provider_id, upstream_model_id, binding))
        if record is None:
            return self.authorize(
                provider_id=provider_id,
                provider_model=upstream_model_id,
                account_binding_id=binding,
            )
        next_record = replace(
            record,
            access_status=AccessStatus.VERIFIED_WORKING,
            last_probe_at_utc=utc_now(),
        )
        self._records[next_record.key] = next_record
        self._persist(next_record)
        return self.authorize(
            provider_id=provider_id,
            provider_model=upstream_model_id,
            account_binding_id=binding,
        )

    async def attest(
        self,
        *,
        provider_id: str,
        upstream_model_id: str,
        source_url: str,
        note: str = "",
        by: str = "admin",
    ) -> EligibilityRecord | None:
        """Record a human free-tier attestation for one model.

        This is the only way a model becomes ``free_permanent``: a person confirms, on
        the record, that no card, top-up or subscription is required. It is refused
        when manual approval is disabled by policy.
        """

        config = self.config
        if not config.allow_manual_approval:
            return None
        settings = self._settings_provider()
        binding = account_binding_id(provider_id, settings)
        record = self._records.get((provider_id, upstream_model_id, binding))
        base = record or EligibilityRecord(
            provider_id=provider_id,
            upstream_model_id=upstream_model_id,
            account_binding_id=binding,
        )
        now = utc_now()
        next_record = replace(
            base,
            billing_class=BillingClass.FREE_PERMANENT,
            access_status=AccessStatus.NOT_PROBED,
            requires_card=False,
            requires_topup=False,
            requires_subscription=False,
            uses_trial_or_promotional_balance=False,
            manually_approved=True,
            manual_attestation_url=source_url,
            manual_attestation_by=by,
            manual_attestation_at_utc=now,
            blocked_reason_code=None,
            evidence_source=EvidenceSource.MANUAL_ATTESTATION,
            evidence_checked_at_utc=now,
            evidence_expires_at_utc=now + config.ttl,
        )
        self._records[next_record.key] = next_record
        try:
            await self._store.save(next_record)
        except Exception:
            logger.warning(
                "FreeGuard attestation could not be persisted: provider={}",
                provider_id,
            )
        trace_event(
            stage="runtime",
            event="free_claude_code.free_guard.attested",
            source="runtime",
            provider_id=provider_id,
            upstream_model_id=upstream_model_id,
            attestation_by=by,
            has_source_url=bool(source_url),
        )
        return next_record

    async def revoke_attestation(
        self, provider_id: str, upstream_model_id: str
    ) -> None:
        """Remove evidence for one model so it returns to ``unknown``."""

        settings = self._settings_provider()
        binding = account_binding_id(provider_id, settings)
        self._records.pop((provider_id, upstream_model_id, binding), None)
        await self._store.delete(provider_id, upstream_model_id, binding)

    # ------------------------------------------------------------ read models

    def model_statuses(
        self,
        *,
        provider_id: str | None = None,
        query: str | None = None,
        billing_class: str | None = None,
        access_status: str | None = None,
        include_blocked: bool = True,
        account_binding_id: str | None = None,
    ) -> tuple[ModelStatus, ...]:
        """Every known model plus the verdict the policy currently returns.

        Models that appear in the provider catalog but have no evidence at all are
        listed too — an absent record is itself the most important status to show,
        because it is what strict mode blocks.
        """

        settings = self._settings_provider()
        guard = self.guard()
        labels = eligibility_reason_labels()
        rows: list[ModelStatus] = []
        for _key, record in self._iter_records(settings, provider_id):
            decision = guard.authorize(
                provider_id=record.provider_id,
                provider_model=record.upstream_model_id,
                account_binding_id=record.account_binding_id,
            )
            if not include_blocked and not decision.allowed:
                continue
            rows.append(
                ModelStatus(
                    provider_id=record.provider_id,
                    upstream_model_id=record.upstream_model_id,
                    account_binding_id=record.account_binding_id,
                    billing_class=record.billing_class,
                    access_status=record.access_status,
                    eligible=decision.allowed,
                    available_now=decision.available_now,
                    reason_code=decision.reason_code,
                    reason_label=labels.get(
                        decision.reason_code.value, decision.reason_code.value
                    ),
                    detail=decision.detail,
                    evidence_source=record.evidence_source,
                    evidence_checked_at_utc=self._text(record.evidence_checked_at_utc),
                    evidence_expires_at_utc=self._text(record.evidence_expires_at_utc),
                    last_probe_at_utc=self._text(record.last_probe_at_utc),
                    manually_approved=record.manually_approved,
                    manual_attestation_url=record.manual_attestation_url,
                    manual_attestation_by=record.manual_attestation_by,
                    manual_attestation_at_utc=self._text(
                        record.manual_attestation_at_utc
                    ),
                )
            )
        if query:
            needle = query.casefold()
            rows = [
                row
                for row in rows
                if needle in row.upstream_model_id.casefold()
                or needle in row.provider_id.casefold()
            ]
        if billing_class:
            rows = [row for row in rows if row.billing_class.value == billing_class]
        if access_status:
            rows = [row for row in rows if row.access_status.value == access_status]
        rows.sort(key=lambda row: (row.provider_id, row.upstream_model_id))
        return tuple(rows)

    def summary(self) -> JsonObject:
        """The Admin Free Models header: mode plus category counts."""

        config = self.config
        statuses = self.model_statuses()
        counts = {
            "verified_free": 0,
            "temporarily_unavailable": 0,
            "blocked_paid": 0,
            "unknown": 0,
        }
        for row in statuses:
            if row.eligible and row.available_now:
                counts["verified_free"] += 1
            elif row.eligible:
                counts["temporarily_unavailable"] += 1
            elif row.billing_class is BillingClass.UNKNOWN:
                counts["unknown"] += 1
            else:
                counts["blocked_paid"] += 1
        last = self._last_refresh
        summary: JsonObject = {
            "mode": config.mode.value,
            "strict": config.strict,
            "durable_storage": self.durable,
            "counts": counts,
            "known_models": len(statuses),
            "eligible_models": counts["verified_free"]
            + counts["temporarily_unavailable"],
            "last_refresh": {
                "started_at_utc": (
                    utc_text(last.started_at_utc) if last is not None else None
                ),
                "finished_at_utc": (
                    utc_text(last.finished_at_utc) if last is not None else None
                ),
                "checked_models": last.checked_models if last is not None else 0,
                "eligible_models": last.eligible_models if last is not None else 0,
                "failed_provider_ids": (
                    list(last.failed_provider_ids) if last is not None else []
                ),
                "in_progress": (
                    self._refresh_task is not None and not self._refresh_task.done()
                ),
            },
            "adapters": sorted(known_provider_ids()),
            "settings": {
                "allow_local_models": config.allow_local_models,
                "require_no_card": config.require_no_card,
                "require_no_topup": config.require_no_topup,
                "require_no_subscription": config.require_no_subscription,
                "allow_trials": config.allow_trials,
                "allow_promotional_credits": config.allow_promotional_credits,
                "allow_manual_approval": config.allow_manual_approval,
                "eligibility_ttl_hours": config.eligibility_ttl_hours,
                "block_when_evidence_stale": config.block_when_evidence_stale,
            },
        }
        return summary

    async def audit(self, *, limit: int = 200) -> tuple[dict[str, object], ...]:
        return await self._store.recent_audit(limit=limit)

    # ---------------------------------------------------------------- internal

    async def _reload(self, reason: str) -> None:
        self._reload_settings_id = id(self._settings_provider())
        try:
            records = await self._store.load_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "FreeGuard evidence reload failed ({}): exc_type={}. The snapshot is "
                "empty, so strict mode blocks every model until evidence returns.",
                reason,
                type(exc).__name__,
            )
            records = ()
        snapshot = {record.key: record for record in records}
        settings = self._settings_provider()
        binding_cache: dict[str, str] = {}
        for key in list(snapshot):
            provider_id = key[0]
            if provider_id not in binding_cache:
                binding_cache[provider_id] = account_binding_id(provider_id, settings)
            if snapshot[key].account_binding_id != binding_cache[provider_id]:
                # A key or account change invalidates the old account's proof.
                snapshot.pop(key)
        self._records = snapshot
        self._loaded = True
        self._last_reload_at = time.monotonic()

    def _iter_records(
        self, settings: Settings, provider_id: str | None
    ) -> list[tuple[tuple[str, str, str], EligibilityRecord]]:
        records: dict[tuple[str, str, str], EligibilityRecord] = dict(self._records)
        binding_cache: dict[str, str] = {}
        for provider, model, binding in list(records):
            if provider not in binding_cache:
                binding_cache[provider] = account_binding_id(provider, settings)
            if binding != binding_cache[provider]:
                # The stored record belongs to a different account.
                records.pop((provider, model, binding))
        if provider_id is not None:
            records = {
                key: record for key, record in records.items() if key[0] == provider_id
            }
        return list(records.items())

    def _target_providers(self) -> list[str]:
        settings = self._settings_provider()
        providers = {record.provider_id for record in self._records.values()}
        providers.update(known_provider_ids())
        if self._model_inventory is not None:
            try:
                providers.update(self._model_inventory().keys())
            except Exception:
                logger.debug("FreeGuard model inventory unavailable")
        return sorted(
            provider
            for provider in providers
            if provider in settings.provider_ids or provider in known_provider_ids()
        )

    async def _collect_provider_evidence(
        self, provider_id: str, settings: Settings
    ) -> tuple[EligibilityRecord, ...]:
        adapter = self._adapters.adapter(provider_id)
        pricing = adapter.discover_pricing(provider_id)
        constraints = adapter.discover_account_constraints(provider_id)
        binding = account_binding_id(provider_id, settings)
        config = self.config
        now = utc_now()
        records: dict[tuple[str, str, str], EligibilityRecord] = {}
        models = self._provider_models(provider_id)
        for evidence in pricing:
            for model_id in self._expand_models(evidence, models):
                record = record_from_evidence(
                    provider_id=provider_id,
                    upstream_model_id=model_id,
                    account_binding_id=binding,
                    pricing=evidence,
                    account=constraints,
                    ttl=config.ttl,
                    now=now,
                )
                if (
                    models
                    and evidence.upstream_model_id == "*"
                    and record.billing_class is BillingClass.LOCAL
                ):
                    record = replace(record, evidence_expires_at_utc=now + config.ttl)
                records[record.key] = record
        for model_id in models:
            key = (provider_id, model_id, binding)
            if key in records:
                continue
            evidence = PricingEvidence(
                provider_id=provider_id,
                upstream_model_id=model_id,
                billing_class=BillingClass.UNKNOWN,
                source=EvidenceSource.DOCUMENTED_PUBLIC,
                source_url=None,
                note="No published pricing or free-tier declaration for this model.",
            )
            records[key] = record_from_evidence(
                provider_id=provider_id,
                upstream_model_id=model_id,
                account_binding_id=binding,
                pricing=evidence,
                account=constraints,
                ttl=config.ttl,
                now=now,
            )
        return tuple(records.values())

    @staticmethod
    def _expand_models(
        evidence: PricingEvidence, models: Sequence[str]
    ) -> tuple[str, ...]:
        if evidence.upstream_model_id != "*":
            return (evidence.upstream_model_id,)
        return tuple(models)

    def _provider_models(self, provider_id: str) -> tuple[str, ...]:
        if self._model_inventory is None:
            return ()
        return _provider_models_from(self._model_inventory, provider_id)

    def _persist(self, record: EligibilityRecord) -> None:
        if self._store.durable:
            try:
                self._schedule_save(record)
            except RuntimeError:
                return

    def _schedule_save(self, record: EligibilityRecord) -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._store.save(record))
        task.add_done_callback(_log_task_result)

    @staticmethod
    def _text(moment) -> str | None:
        return utc_text(moment) if moment is not None else None


def _log_task_result(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "FreeGuard background write ended with exc_type={}", type(exc).__name__
        )


class ModelInventoryProvider(Protocol):
    """Returns the provider/model pairs currently known to the catalog."""

    def __call__(self) -> Mapping[str, Sequence[str]]: ...


class _NoModelInventory:
    """Default inventory: eligibility refresh uses only stored records."""

    def __call__(self) -> Mapping[str, Sequence[str]]:
        return {}


def account_binding_id_for(provider_id: str, settings) -> str:
    """Module-level alias so a parameter name never shadows the helper."""

    return account_binding_id(provider_id, settings)


def _provider_models_from(
    inventory: ModelInventoryProvider, provider_id: str
) -> tuple[str, ...]:
    try:
        models = inventory().get(provider_id, ())
    except Exception:
        return ()
    return tuple(models)

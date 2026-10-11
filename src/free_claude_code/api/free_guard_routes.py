"""Admin API for FreeGuard: summary, eligibility, refresh, probe and attestation.

Every route is loopback-admin only, returns ``Cache-Control: no-store``, and returns
JSON built from the application-layer read models. No business logic lives here.

Nothing in this module can start a paid request. ``probe`` is refused for a model the
policy has not already cleared, and it never sends user content.
"""

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from free_claude_code.application.free_guard.models import ReasonCode
from free_claude_code.core.json_types import JsonObject

from .admin_security import require_loopback_admin
from .dependencies import get_services
from .ports import ApiServices

router = APIRouter(prefix="/admin/api/free-guard", tags=["free-guard"])


class AttestationPayload(BaseModel):
    """A human free-tier attestation for one model."""

    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    source_url: str = Field(min_length=1, max_length=2048)
    note: str = Field(default="", max_length=2000)
    attested_by: str = Field(default="admin", max_length=128)


class ProbePayload(BaseModel):
    """A liveness probe request for an already-cleared model."""

    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)


class RefreshPayload(BaseModel):
    provider_ids: list[str] = Field(default_factory=list, max_length=64)


@router.get("/summary")
async def free_guard_summary(
    request: Request, services: ApiServices = Depends(get_services)
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"mode": "off", "available": False})
    return _no_store(dict(guard.summary()))


@router.get("/models")
async def free_guard_models(
    request: Request,
    provider: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=256),
    cursor: str | None = Query(default=None, max_length=256),
    include_blocked: bool = Query(default=True),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"items": [], "next_cursor": None, "available": False})
    rows = guard.model_statuses(
        include_blocked=include_blocked,
        provider_id=provider,
        query=q,
        billing_class=status,
        account_binding_id=cursor,
    )
    items = [_as_dict(row) for row in rows]
    return _no_store(
        {
            "items": items,
            "count": len(items),
            "next_cursor": None,
        }
    )


@router.post("/refresh")
async def free_guard_refresh(
    request: Request,
    payload: RefreshPayload | None = Body(default=None),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"available": False, "state": "idle"}, status_code=503)
    provider_ids = tuple(payload.provider_ids) if payload else ()
    job_id = guard.start_refresh_job(provider_ids=tuple(provider_ids))
    return _no_store({"job_id": job_id, "state": "running"})


@router.get("/jobs/{job_id}")
async def free_guard_job(
    job_id: str, request: Request, services: ApiServices = Depends(get_services)
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"available": False}, status_code=503)
    status = guard.job_status()
    if status.get("job_id") != job_id and job_id != "latest":
        # Unknown job ids get an idle status rather than another server's progress.
        return _no_store({"job_id": job_id, "state": "unknown"})
    return _no_store(status)


@router.post("/probe")
async def free_guard_probe(
    payload: ProbePayload,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    """Probe only a model the policy has already cleared as free.

    The guard is re-evaluated first. A paid, unproven or trial model is refused with
    ``guard_blocked`` and no request leaves the process.
    """

    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"available": False}, status_code=503)
    decision = guard.authorize(
        provider_id=payload.provider_id, provider_model=payload.model_id
    )
    if not decision.allowed:
        return _no_store(
            {
                "probed": False,
                "reason_code": decision.reason_code.value,
                "detail": decision.detail,
            },
            status_code=403,
        )
    return _no_store(
        {
            "probed": True,
            "access_status": "not_probed",
            "detail": (
                "Liveness probing is not enabled on this installation; the model is "
                "already cleared for routing."
            ),
        }
    )


@router.post("/attest")
async def free_guard_attest(
    payload: AttestationPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"available": False}, status_code=503)
    record = await guard.attest(
        provider_id=payload.provider_id,
        upstream_model_id=payload.model_id,
        source_url=payload.source_url,
        note=payload.note,
        by=payload.attested_by,
    )
    if record is None:
        return _no_store(
            {
                "attested": False,
                "reason_code": ReasonCode.MANUAL_APPROVAL_REQUIRED.value,
                "detail": "Manual approval is disabled by policy.",
            },
            status_code=403,
        )
    return _no_store(
        {
            "attested": True,
            "provider_id": record.provider_id,
            "model_id": record.upstream_model_id,
            "billing_class": record.billing_class.value,
            "attested_at_utc": (
                record.manual_attestation_at_utc.isoformat()
                if record.manual_attestation_at_utc
                else None
            ),
        }
    )


@router.post("/revoke")
async def free_guard_revoke(
    payload: ProbePayload,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"available": False}, status_code=503)
    await guard.revoke_attestation(payload.provider_id, payload.model_id)
    return _no_store({"revoked": True})


@router.get("/audit")
async def free_guard_audit(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"items": [], "available": False})
    return _no_store({"items": list(await guard.audit(limit=limit))})


@router.get("/export")
async def free_guard_export(
    request: Request,
    scope: str = Query(default="allowlist", max_length=32),
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    require_loopback_admin(request)
    guard = services.free_guard
    if guard is None:
        return _no_store({"items": [], "available": False})
    if scope not in ("allowlist", "excluded", "all"):
        raise HTTPException(status_code=400, detail="Unknown export scope")
    rows: list[JsonObject] = []
    for row in guard.model_statuses(include_blocked=True):
        entry = _as_dict(row)
        eligible = bool(entry.get("eligible"))
        if scope == "allowlist" and not eligible:
            continue
        if scope == "excluded" and eligible:
            continue
        rows.append(entry)
    return _no_store({"scope": scope, "count": len(rows), "items": rows})


def _as_dict(row: object) -> JsonObject:
    as_dict = getattr(row, "as_dict", None)
    if as_dict is None:
        raise HTTPException(status_code=500, detail="Unsupported status row")
    return dict(as_dict())


def _no_store(payload: Mapping[str, object], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {**payload, "cache": "no-store"},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]

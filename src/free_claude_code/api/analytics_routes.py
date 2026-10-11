"""Admin API for analytics: KPIs, timeseries, breakdowns, history, trace, export.

Every route is loopback-admin only and returns ``Cache-Control: no-store``. Ranges are
UTC RFC3339, filters pass an allowlist, ``limit`` is capped at 200, and SQL is
parameterized in :mod:`free_claude_code.runtime.analytics_queries`.

The payloads carry identifiers and counters only. There is no endpoint here that could
return prompt text, a response body or a credential.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from free_claude_code.application.analytics.queries import (
    AnalyticsQueries,
    AnalyticsRangeError,
    normalize_limit,
    resolve_range,
    validate_filters,
)
from free_claude_code.config.settings import Settings

from .admin_security import require_loopback_admin
from .dependencies import get_services, get_settings
from .ports import AnalyticsAdminPort, ApiServices

router = APIRouter(prefix="/admin/api/analytics", tags=["analytics"])

_CSV_HEADER = (
    "request_id,started_at_utc,client_type,requested_model,request_status,"
    "normalized_error_code,duration_ms,fallback_count,attempt_number,provider_id,"
    "upstream_model_id,attempt_status,upstream_http_status,input_tokens,output_tokens,"
    "cache_read_tokens,cache_write_tokens,usage_source,estimated_cost_usd,"
    "cost_confidence\n"
)


def _analytics(services: ApiServices) -> AnalyticsAdminPort:
    analytics = services.analytics
    if analytics is None or not hasattr(analytics, "queries"):
        raise HTTPException(status_code=503, detail="Analytics is not configured")
    return cast(AnalyticsAdminPort, analytics)


def _queries(services: ApiServices) -> AnalyticsQueries:
    analytics = _analytics(services)
    if not analytics.enabled:
        raise HTTPException(status_code=503, detail="Analytics is disabled")
    queries = analytics.queries
    if queries is None:
        raise HTTPException(status_code=503, detail="Analytics storage is unavailable")
    return queries


def _range(
    settings: Settings, from_utc: str | None, to_utc: str | None
) -> tuple[datetime, datetime]:
    try:
        return resolve_range(from_utc, to_utc, settings=settings)
    except AnalyticsRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _filters(
    provider: str | None,
    model: str | None,
    client: str | None,
    status: str | None,
) -> dict[str, str]:
    try:
        return validate_filters(
            provider=provider, model=model, client=client, status=status
        )
    except AnalyticsRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary")
async def analytics_summary(
    request: Request,
    from_utc: str | None = Query(default=None, max_length=40),
    to_utc: str | None = Query(default=None, max_length=40),
    provider: str | None = Query(default=None, max_length=128),
    model: str | None = Query(default=None, max_length=256),
    client: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    include_probes: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    require_loopback_admin(request)
    queries = _queries(services)
    start, end = _range(settings, from_utc, to_utc)
    filters = _filters(provider, model, client, status)
    payload = await queries.summary(
        start=start,
        end=end,
        filters=filters,
        include_probes=include_probes,
    )
    return _no_store(payload)


@router.get("/timeseries")
async def analytics_timeseries(
    request: Request,
    from_utc: str | None = Query(default=None, max_length=40),
    to_utc: str | None = Query(default=None, max_length=40),
    bucket: str = Query(default="hour", max_length=16),
    provider: str | None = Query(default=None, max_length=128),
    model: str | None = Query(default=None, max_length=256),
    client: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    include_probes: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    require_loopback_admin(request)
    queries = _queries(services)
    start, end = _range(settings, from_utc, to_utc)
    filters = _filters(provider, model, client, status)
    try:
        payload = await queries.timeseries(
            start=start,
            end=end,
            bucket=bucket,
            filters=filters,
            include_probes=include_probes,
        )
    except AnalyticsRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _no_store(payload)


@router.get("/breakdown")
async def analytics_breakdown(
    request: Request,
    by: str = Query(default="provider", max_length=16),
    from_utc: str | None = Query(default=None, max_length=40),
    to_utc: str | None = Query(default=None, max_length=40),
    provider: str | None = Query(default=None, max_length=128),
    model: str | None = Query(default=None, max_length=256),
    client: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_probes: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    require_loopback_admin(request)
    queries = _queries(services)
    start, end = _range(settings, from_utc, to_utc)
    filters = _filters(provider, model, client, status)
    try:
        payload = await queries.breakdown(
            by=by,
            start=start,
            end=end,
            filters=filters,
            include_probes=include_probes,
            limit=limit,
        )
    except AnalyticsRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _no_store(payload)


@router.get("/requests")
async def analytics_requests(
    request: Request,
    from_utc: str | None = Query(default=None, max_length=40),
    to_utc: str | None = Query(default=None, max_length=40),
    provider: str | None = Query(default=None, max_length=128),
    model: str | None = Query(default=None, max_length=256),
    client: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = Query(default=None, max_length=64),
    include_probes: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    require_loopback_admin(request)
    queries = _queries(services)
    start, end = _range(settings, from_utc, to_utc)
    filters = _filters(provider, model, client, status)
    cap = normalize_limit(limit)
    payload = await queries.requests_page(
        start=start,
        end=end,
        filters=filters,
        include_probes=include_probes,
        limit=cap,
        cursor=cursor,
    )
    return _no_store(payload)


@router.get("/requests/{request_id}")
async def analytics_request_trace(
    request_id: str,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> JSONResponse:
    require_loopback_admin(request)
    queries = _queries(services)
    payload = await queries.request_trace(request_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return _no_store(payload)


@router.get("/export")
async def analytics_export(
    request: Request,
    response: Response,
    format: str = Query(default="json", max_length=8),
    from_utc: str | None = Query(default=None, max_length=40),
    to_utc: str | None = Query(default=None, max_length=40),
    provider: str | None = Query(default=None, max_length=128),
    model: str | None = Query(default=None, max_length=256),
    client: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    include_probes: bool = Query(default=False),
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> Response:
    require_loopback_admin(request)
    analytics = services.analytics
    if analytics is None or not getattr(settings, "analytics_export_enabled", True):
        raise HTTPException(status_code=403, detail="Analytics export is disabled")
    queries = _queries(services)
    start, end = _range(settings, from_utc, to_utc)
    filters = _filters(provider, model, client, status)
    rows = await queries.export_rows(
        start=start,
        end=end,
        filters=filters,
        include_probes=include_probes,
        limit=200,
    )
    if format == "csv":
        lines = [_CSV_HEADER.rstrip("\n")]
        lines.extend(
            ",".join(_csv_cell(row.get(key)) for key in _CSV_KEYS) for row in rows
        )
        return Response(
            content="\n".join(lines) + "\n",
            media_type="text/csv; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="fcc-analytics.csv"',
            },
        )
    if format != "json":
        raise HTTPException(status_code=400, detail="Unknown export format")
    return _no_store({"count": len(rows), "items": rows})


@router.get("/diagnostics")
async def analytics_diagnostics(
    request: Request, services: ApiServices = Depends(get_services)
) -> JSONResponse:
    require_loopback_admin(request)
    analytics = _analytics(services)
    return _no_store(
        {
            "available": True,
            "enabled": analytics.enabled,
            "stats": analytics.stats(),
        }
    )


_CSV_KEYS = (
    "request_id",
    "started_at_utc",
    "client_type",
    "requested_model",
    "request_status",
    "normalized_error_code",
    "duration_ms",
    "fallback_count",
    "attempt_number",
    "provider_id",
    "upstream_model_id",
    "attempt_status",
    "upstream_http_status",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "usage_source",
    "estimated_cost_usd",
    "cost_confidence",
)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if any(char in text for char in (",", '"', "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def _no_store(payload: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(dict(payload), headers={"Cache-Control": "no-store"})


__all__ = ["router"]

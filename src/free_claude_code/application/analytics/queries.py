"""Analytics read models: KPI summary, timeseries, breakdowns, history, traces.

Every statement is parameterized, and every filter field is checked against an
allowlist before it reaches SQL. The Admin API is the only caller; it never passes
user text into a query — the request trace holds identifiers and counters, never a
prompt or a response.

Two numbers must never be confused, and the read models keep them apart:

* ``logical_requests`` — what clients asked FCC for.
* ``upstream_attempts`` — what FCC actually sent to providers, including retries and
  fallbacks.

Because ``origin``, ``client_type`` and the requested model live on the request row,
every attempt query joins to it. That keeps "exclude health probes" and the client
filter meaningful at attempt level, which is the level tokens are counted at.

Token totals are reported as ``*_known`` plus an ``unknown_usage_attempts`` count, so
a provider that reported nothing is visibly missing rather than silently zero.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from free_claude_code.application.analytics.models import utc_now
from free_claude_code.application.free_guard.models import utc_text
from free_claude_code.config.settings import Settings

_ALLOWED_BREAKDOWNS = frozenset({"provider", "model", "client", "error"})
_ALLOWED_STATUS = frozenset(
    {"success", "error", "blocked", "canceled", "partial_stream"}
)
_ALLOWED_CLIENTS = frozenset(
    {
        "claude_code",
        "codex",
        "vscode_chat",
        "dsh",
        "opencode",
        "browser",
        "other",
        "unknown",
    }
)
_MAX_RANGE_DAYS = 365
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


class AnalyticsRangeError(ValueError):
    """The requested range or filter is not acceptable."""


def resolve_range(
    from_utc: str | None,
    to_utc: str | None,
    *,
    settings: Settings | None = None,
) -> tuple[datetime, datetime]:
    """Parse and bound a UTC range, rejecting anything absurd."""

    limit_days = _MAX_RANGE_DAYS
    if settings is not None:
        limit_days = min(
            _MAX_RANGE_DAYS,
            max(1, int(getattr(settings, "analytics_retention_days", 90)) * 4),
        )
    start = _parse(from_utc) if from_utc else utc_now() - timedelta(hours=24)
    end = _parse(to_utc) if to_utc else utc_now()
    if end <= start:
        raise AnalyticsRangeError("`to` must be after `from`")
    if end - start > timedelta(days=limit_days):
        raise AnalyticsRangeError(
            f"Requested range exceeds the {limit_days}-day maximum"
        )
    return start, end


def _parse(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise AnalyticsRangeError("Empty timestamp")
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise AnalyticsRangeError("Timestamps must be RFC3339 UTC") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int | str):
        raise AnalyticsRangeError("`limit` must be an integer")
    try:
        value = int(limit)
    except ValueError as exc:
        raise AnalyticsRangeError("`limit` must be an integer") from exc
    if value < 1:
        raise AnalyticsRangeError("`limit` must be at least 1")
    return min(value, _MAX_LIMIT)


def validate_filters(
    *,
    provider: object = None,
    model: object = None,
    client: object = None,
    status: object = None,
    origin: object = None,
) -> dict[str, str]:
    """Return only the filters that pass their allowlist."""

    filters: dict[str, str] = {}
    if isinstance(provider, str) and provider.strip():
        filters["provider"] = provider.strip()[:128]
    if isinstance(model, str) and model.strip():
        filters["model"] = model.strip()[:256]
    if isinstance(client, str) and client.strip():
        value = client.strip().casefold()
        if value not in _ALLOWED_CLIENTS:
            raise AnalyticsRangeError("Unknown client filter")
        filters["client"] = value
    if isinstance(status, str) and status.strip():
        value = status.strip().casefold()
        if value not in _ALLOWED_STATUS:
            raise AnalyticsRangeError("Unknown status filter")
        filters["status"] = value
    if isinstance(origin, str) and origin.strip():
        filters["origin"] = origin.strip().casefold()[:32]
    return filters


@runtime_checkable
class AnalyticsReadStore(Protocol):
    """The storage capability the read models need: one parameterized query."""

    async def query(self, operation): ...


class _Filter:
    """One shared filter set, materialized against either table shape.

    ``origin``, ``client_type`` and the requested model only exist on
    ``logical_requests``, so attempt queries always join to it. Building both SQL
    shapes from one parameter list keeps the two in exact agreement.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None,
        include_probes: bool,
    ) -> None:
        self.start = utc_text(start)
        self.end = utc_text(end)
        self.origin = None if include_probes else "user"
        self.provider = (filters or {}).get("provider")
        self.model = (filters or {}).get("model")
        self.client = (filters or {}).get("client")
        self.status = (filters or {}).get("status")

    def request_sql(self) -> tuple[str, list[object]]:
        clauses = ["started_at_utc >= ?", "started_at_utc <= ?"]
        params: list[object] = [self.start, self.end]
        if self.origin is not None:
            clauses.append("origin = ?")
            params.append(self.origin)
        for column, value in (
            ("provider_id", self.provider),
            ("upstream_model_id", self.model),
            ("client_type", self.client),
            ("status", self.status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        return " WHERE " + " AND ".join(clauses), params

    def attempt_sql(self) -> tuple[str, list[object]]:
        clauses = [
            "r.started_at_utc >= ?",
            "r.started_at_utc <= ?",
            "r.request_id = a.request_id",
        ]
        params: list[object] = [self.start, self.end]
        if self.origin is not None:
            clauses.append("r.origin = ?")
            params.append(self.origin)
        for column, value in (
            ("a.provider_id", self.provider),
            ("a.upstream_model_id", self.model),
            ("r.client_type", self.client),
            ("r.status", self.status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        return " WHERE " + " AND ".join(clauses), params


class AnalyticsQueries:
    """Read-only analytics projections over one store."""

    def __init__(self, store: AnalyticsReadStore) -> None:
        self._store = store

    async def _one[T](self, operation) -> T:
        return await self._store.query(operation)

    # ------------------------------------------------------------------ summary

    async def summary(
        self,
        *,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
        include_probes: bool = False,
    ) -> dict[str, object]:
        active = _Filter(start, end, filters, include_probes)
        request_where, request_params = active.request_sql()
        attempt_where, attempt_params = active.attempt_sql()

        def request_row(connection):
            return connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success, "
                "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error, "
                "SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked, "
                "SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END) AS canceled, "
                "SUM(fallback_count) AS fallbacks FROM logical_requests"
                + request_where,
                tuple(request_params),
            ).fetchone()

        def attempt_row(connection):
            return connection.execute(
                "SELECT COUNT(*) AS attempts, "
                "SUM(CASE WHEN a.status = 'error' THEN 1 ELSE 0 END) AS errored, "
                "SUM(CASE WHEN a.status = 'blocked' THEN 1 ELSE 0 END) AS blocked "
                "FROM upstream_attempts a JOIN logical_requests r "
                "ON r.request_id = a.request_id" + attempt_where,
                tuple(attempt_params),
            ).fetchone()

        def usage_row(connection):
            return connection.execute(
                "SELECT "
                # COUNT() ignores NULLs, so a bucket with no reported usage sums to
                # NULL rather than 0: an unknown total must never look measured.
                "CASE WHEN COUNT(a.input_tokens) > 0 THEN SUM(a.input_tokens) END"
                " AS input_tokens, "
                "CASE WHEN COUNT(a.output_tokens) > 0 THEN SUM(a.output_tokens) END"
                " AS output_tokens, "
                "CASE WHEN COUNT(a.cache_read_tokens) > 0 THEN SUM(a.cache_read_tokens) END"
                " AS cache_read_tokens, "
                "CASE WHEN COUNT(a.cache_write_tokens) > 0 THEN SUM(a.cache_write_tokens) END"
                " AS cache_write_tokens, "
                "SUM(CASE WHEN a.usage_source = 'provider_reported' THEN 1 ELSE 0 END) "
                "AS reported, "
                "SUM(CASE WHEN a.usage_source = 'unknown' THEN 1 ELSE 0 END) "
                "AS unknown_usage "
                "FROM upstream_attempts a JOIN logical_requests r "
                "ON r.request_id = a.request_id" + attempt_where,
                tuple(attempt_params),
            ).fetchone()

        def cost_row(connection):
            return connection.execute(
                "SELECT SUM(CASE WHEN a.estimated_cost_usd IS NOT NULL THEN "
                "CAST(a.estimated_cost_usd AS REAL) END) AS known, "
                "SUM(CASE WHEN a.estimated_cost_usd IS NOT NULL THEN 1 ELSE 0 END) "
                "AS priced, "
                "SUM(CASE WHEN a.estimated_cost_usd IS NULL THEN 1 ELSE 0 END) "
                "AS unpriced, "
                "SUM(CASE WHEN a.cost_confidence = 'verified' THEN 1 ELSE 0 END) "
                "AS verified_free "
                "FROM upstream_attempts a JOIN logical_requests r "
                "ON r.request_id = a.request_id" + attempt_where,
                tuple(attempt_params),
            ).fetchone()

        def latency_row(connection):
            return connection.execute(
                "SELECT duration_ms FROM logical_requests"
                + request_where
                + " AND duration_ms IS NOT NULL",
                tuple(request_params),
            ).fetchall()

        def ttft_row(connection):
            return connection.execute(
                "SELECT a.ttft_ms FROM upstream_attempts a JOIN logical_requests r "
                "ON r.request_id = a.request_id"
                + attempt_where
                + " AND a.ttft_ms IS NOT NULL",
                tuple(attempt_params),
            ).fetchall()

        def error_row(connection):
            return connection.execute(
                "SELECT r.normalized_error_code, COUNT(*) AS total "
                "FROM logical_requests r"
                + request_where
                + " AND r.normalized_error_code IS NOT NULL "
                "GROUP BY r.normalized_error_code ORDER BY total DESC LIMIT 12",
                tuple(request_params),
            ).fetchall()

        requests = await self._one(request_row)
        attempts = await self._one(attempt_row)
        usage = await self._one(usage_row)
        cost = await self._one(cost_row)
        latency = await self._one(latency_row)
        ttft = await self._one(ttft_row)
        errors = await self._one(error_row)

        total = _int(requests["total"] if requests else None) or 0
        success = _int(requests["success"] if requests else None) or 0
        attempt_total = _int(attempts["attempts"] if attempts else None) or 0
        fallbacks = _int(requests["fallbacks"] if requests else None) or 0
        reported = _int(usage["reported"] if usage else None) or 0
        unknown_usage = _int(usage["unknown_usage"] if usage else None) or 0

        return {
            "from_utc": utc_text(start),
            "to_utc": utc_text(end),
            "filters": filters or {},
            "include_probes": include_probes,
            "logical_requests": total,
            "success_count": success,
            "error_count": _int(requests["error"] if requests else None) or 0,
            "blocked_count": _int(requests["blocked"] if requests else None) or 0,
            "canceled_count": _int(requests["canceled"] if requests else None) or 0,
            "downstream_attempts": attempt_total,
            "error_attempts": _int(attempts["errored"] if attempts else None) or 0,
            "blocked_attempts": _int(attempts["blocked"] if attempts else None) or 0,
            "fallback_logical_requests": fallbacks,
            "retry_fallback_rate": (round(attempt_total / total, 4) if total else 0.0),
            "success_rate": round(success / total, 4) if total else None,
            "usage": {
                "input_tokens_known": _int(usage["input_tokens"] if usage else None)
                or 0,
                "output_tokens_known": _int(usage["output_tokens"] if usage else None)
                or 0,
                "cache_read_tokens_known": _int(
                    usage["cache_read_tokens"] if usage else None
                )
                or 0,
                "cache_write_tokens_known": _int(
                    usage["cache_write_tokens"] if usage else None
                )
                or 0,
                "usage_source_counts": {
                    "provider_reported": reported,
                    "unknown": unknown_usage,
                },
                "unknown_usage_attempts": unknown_usage,
                "unknown_token_share": (
                    round(unknown_usage / attempt_total, 4) if attempt_total else None
                ),
            },
            "estimated_cost": {
                "known_usd": _decimal(cost["known"] if cost else None),
                "priced_attempts": _int(cost["priced"] if cost else None) or 0,
                "unpriced_attempts": _int(cost["unpriced"] if cost else None) or 0,
                "verified_free_attempts": _int(cost["verified_free"] if cost else None)
                or 0,
                "confidence": _cost_confidence(
                    _int(cost["priced"] if cost else None) or 0,
                    _int(cost["unpriced"] if cost else None) or 0,
                ),
            },
            "latency": _percentiles([row[0] for row in latency] if latency else []),
            "ttft": _percentiles([row[0] for row in ttft] if ttft else []),
            "error_breakdown": [
                {"code": row[0], "count": _int(row[1]) or 0} for row in (errors or [])
            ],
        }

    # --------------------------------------------------------------- timeseries

    async def timeseries(
        self,
        *,
        start: datetime,
        end: datetime,
        bucket: str = "hour",
        filters: dict[str, str] | None = None,
        include_probes: bool = False,
    ) -> dict[str, object]:
        size = bucket.strip().casefold() if isinstance(bucket, str) else "hour"
        if size not in ("minute", "hour", "day"):
            raise AnalyticsRangeError("`bucket` must be minute, hour or day")
        step = {
            "minute": "%Y-%m-%dT%H:%M:00Z",
            "hour": "%Y-%m-%dT%H:00:00Z",
            "day": "%Y-%m-%d",
        }[size]
        active = _Filter(start, end, filters, include_probes)
        request_where, request_params = active.request_sql()
        attempt_where, attempt_params = active.attempt_sql()

        def rows(connection):
            return connection.execute(
                "SELECT strftime(?, r.started_at_utc) AS bucket, r.status, "
                "COUNT(*) AS total FROM logical_requests r"
                + request_where
                + " GROUP BY bucket, r.status ORDER BY bucket",
                (step, *request_params),
            ).fetchall()

        def usage_rows(connection):
            return connection.execute(
                "SELECT strftime(?, a.started_at_utc) AS bucket, "
                "CASE WHEN COUNT(a.input_tokens) > 0 THEN SUM(a.input_tokens) END"
                " AS input_tokens, "
                "CASE WHEN COUNT(a.output_tokens) > 0 THEN SUM(a.output_tokens) END"
                " AS output_tokens, "
                "CASE WHEN COUNT(a.cache_read_tokens) > 0 THEN SUM(a.cache_read_tokens) END"
                " AS cache_read_tokens, "
                "CASE WHEN COUNT(a.cache_write_tokens) > 0 THEN SUM(a.cache_write_tokens) END"
                " AS cache_write_tokens "
                "FROM upstream_attempts a JOIN logical_requests r "
                "ON r.request_id = a.request_id"
                + attempt_where
                + " GROUP BY bucket ORDER BY bucket",
                (step, *attempt_params),
            ).fetchall()

        data = await self._one(rows)
        usage = await self._one(usage_rows)
        buckets: dict[str, dict[str, object]] = {}
        for row in data or []:
            entry = buckets.setdefault(
                row[0], {"bucket": row[0], "success": 0, "error": 0, "blocked": 0}
            )
            entry[str(row[1])] = _int(row[2]) or 0
        for row in usage or []:
            entry = buckets.setdefault(
                row[0],
                {
                    "bucket": row[0],
                    "success": 0,
                    "error": 0,
                    "blocked": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )
            entry["input_tokens"] = _int(row[1]) or 0
            entry["output_tokens"] = _int(row[2]) or 0
            entry["cache_read_tokens"] = _int(row[3]) or 0
            entry["cache_write_tokens"] = _int(row[4]) or 0
        return {
            "from_utc": utc_text(start),
            "to_utc": utc_text(end),
            "bucket": size,
            "series": [buckets[key] for key in sorted(buckets)],
        }

    # --------------------------------------------------------------- breakdown

    async def breakdown(
        self,
        *,
        by: str,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
        include_probes: bool = False,
        limit: int = 50,
    ) -> dict[str, object]:
        dimension = by.strip().casefold() if isinstance(by, str) else ""
        if dimension not in _ALLOWED_BREAKDOWNS:
            raise AnalyticsRangeError("`by` must be provider, model, client or error")
        active = _Filter(start, end, filters, include_probes)
        attempt_where, attempt_params = active.attempt_sql()
        column = {
            "provider": "a.provider_id",
            "model": "a.provider_id || '/' || a.upstream_model_id",
            "client": "r.client_type",
            "error": "r.normalized_error_code",
        }[dimension]
        cap = normalize_limit(limit)

        def rows(connection):
            return connection.execute(
                f"SELECT {column} AS key, "
                "COUNT(*) AS attempts, "
                "SUM(CASE WHEN a.status = 'success' THEN 1 ELSE 0 END) AS succeeded, "
                "SUM(CASE WHEN a.status = 'error' THEN 1 ELSE 0 END) AS errored, "
                "SUM(CASE WHEN a.input_tokens IS NOT NULL THEN a.input_tokens ELSE 0 END)"
                " AS input_tokens, "
                "SUM(CASE WHEN a.output_tokens IS NOT NULL THEN a.output_tokens "
                "ELSE 0 END) AS output_tokens, "
                "SUM(CASE WHEN a.cache_read_tokens IS NOT NULL "
                "THEN a.cache_read_tokens ELSE 0 END) AS cache_read_tokens, "
                "SUM(CASE WHEN a.usage_source = 'unknown' THEN 1 ELSE 0 END) AS unknown "
                "FROM upstream_attempts a JOIN logical_requests r "
                "ON r.request_id = a.request_id"
                + attempt_where
                + " GROUP BY key ORDER BY attempts DESC LIMIT ?",
                (*attempt_params, cap),
            ).fetchall()

        data = await self._one(rows)
        return {
            "by": dimension,
            "from_utc": utc_text(start),
            "to_utc": utc_text(end),
            "items": [
                {
                    "key": row[0] or "(unattributed)",
                    "upstream_attempts": _int(row[1]) or 0,
                    "succeeded": _int(row[2]) or 0,
                    "errored": _int(row[3]) or 0,
                    "input_tokens": _int(row[4]) or 0,
                    "output_tokens": _int(row[5]) or 0,
                    "cache_read_tokens": _int(row[6]) or 0,
                    "unknown_usage_attempts": _int(row[7]) or 0,
                }
                for row in (data or [])
            ],
        }

    # ----------------------------------------------------------------- history

    async def requests_page(
        self,
        *,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
        include_probes: bool = False,
        limit: int = _DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, object]:
        cap = normalize_limit(limit)
        active = _Filter(start, end, filters, include_probes)
        request_where, request_params = active.request_sql()
        if cursor:
            request_where += " AND r.request_id < ?"
            request_params.append(cursor[:64])

        def rows(connection):
            return connection.execute(
                "SELECT r.request_id, r.started_at_utc, r.ended_at_utc, r.client_type, "
                "r.requested_model, r.resolved_provider_id, r.resolved_upstream_model_id,"
                " r.status, r.normalized_error_code, r.duration_ms, r.fallback_count, "
                "r.request_streamed, r.origin, "
                "(SELECT COUNT(*) FROM upstream_attempts a WHERE a.request_id = r.request_id)"
                " AS attempts, "
                "(SELECT CASE WHEN COUNT(a.input_tokens) > 0 THEN SUM(a.input_tokens) END"
                " FROM upstream_attempts a "
                "WHERE a.request_id = r.request_id) AS input_tokens, "
                "(SELECT CASE WHEN COUNT(a.output_tokens) > 0 THEN SUM(a.output_tokens) END"
                " FROM upstream_attempts a "
                "WHERE a.request_id = r.request_id) AS output_tokens, "
                "(SELECT CASE WHEN COUNT(a.cache_read_tokens) > 0 "
                "THEN SUM(a.cache_read_tokens) END FROM upstream_attempts a "
                "WHERE a.request_id = r.request_id) AS cache_read_tokens, "
                "(SELECT SUM(CASE WHEN a.usage_source = 'unknown' THEN 1 ELSE 0 END) "
                "FROM upstream_attempts a WHERE a.request_id = r.request_id) "
                "AS unknown_usage "
                "FROM logical_requests r"
                + request_where
                + " ORDER BY r.started_at_utc DESC, r.request_id DESC LIMIT ?",
                (*request_params, cap + 1),
            ).fetchall()

        data = await self._one(rows)
        page = list(data or [])[:cap]
        next_cursor = (
            page[-1]["request_id"] if data and len(data) > cap and page else None
        )
        return {
            "from_utc": utc_text(start),
            "to_utc": utc_text(end),
            "limit": cap,
            "next_cursor": next_cursor,
            "items": [
                {
                    "request_id": row["request_id"],
                    "started_at_utc": row["started_at_utc"],
                    "ended_at_utc": row["ended_at_utc"],
                    "client_type": row["client_type"],
                    "requested_model": row["requested_model"],
                    "final_provider_id": row["resolved_provider_id"],
                    "final_upstream_model_id": row["resolved_upstream_model_id"],
                    "status": row["status"],
                    "normalized_error_code": row["normalized_error_code"],
                    "duration_ms": _int(row["duration_ms"]),
                    "upstream_attempts": _int(row["attempts"]) or 0,
                    "fallback_count": _int(row["fallback_count"]) or 0,
                    "request_streamed": bool(row["request_streamed"]),
                    "origin": row["origin"],
                    "input_tokens": _int(row["input_tokens"]),
                    "output_tokens": _int(row["output_tokens"]),
                    "cache_read_tokens": _int(row["cache_read_tokens"]),
                    "unknown_usage_attempts": _int(row["unknown_usage"]) or 0,
                }
                for row in page
            ],
        }

    async def request_trace(self, request_id: str) -> dict[str, object] | None:
        identifier = request_id.strip()[:64]
        if not identifier:
            return None

        def request_row(connection):
            return connection.execute(
                "SELECT request_id, started_at_utc, ended_at_utc, client_type, "
                "requested_model, resolved_provider_id, resolved_upstream_model_id, "
                "status, normalized_error_code, duration_ms, fallback_count, "
                "retry_count, request_streamed, origin, client_label, final_attempt_id "
                "FROM logical_requests WHERE request_id = ?",
                (identifier,),
            ).fetchone()

        def attempt_rows(connection):
            return connection.execute(
                "SELECT attempt_id, attempt_number, provider_id, upstream_model_id, "
                "fallback_from_attempt_id, started_at_utc, ended_at_utc, status, "
                "upstream_http_status, normalized_error_code, retry_after_seconds, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "reasoning_output_tokens, usage_source, usage_completeness, duration_ms,"
                " ttft_ms, price_snapshot_id, estimated_cost_usd, cost_confidence "
                "FROM upstream_attempts WHERE request_id = ? ORDER BY attempt_number",
                (identifier,),
            ).fetchall()

        request = await self._one(request_row)
        if request is None:
            return None
        attempts = await self._one(attempt_rows)
        return {
            "request": {
                "request_id": request["request_id"],
                "started_at_utc": request["started_at_utc"],
                "ended_at_utc": request["ended_at_utc"],
                "client_type": request["client_type"],
                "requested_model": request["requested_model"],
                "final_provider_id": request["resolved_provider_id"],
                "final_upstream_model_id": request["resolved_upstream_model_id"],
                "status": request["status"],
                "normalized_error_code": request["normalized_error_code"],
                "duration_ms": _int(request["duration_ms"]),
                "fallback_count": _int(request["fallback_count"]) or 0,
                "retry_count": _int(request["retry_count"]) or 0,
                "request_streamed": bool(request["request_streamed"]),
                "origin": request["origin"],
                "final_attempt_id": request["final_attempt_id"],
            },
            "attempts": [
                {
                    "attempt_id": row["attempt_id"],
                    "attempt_number": _int(row["attempt_number"]),
                    "provider_id": row["provider_id"],
                    "upstream_model_id": row["upstream_model_id"],
                    "fallback_from_attempt_id": row["fallback_from_attempt_id"],
                    "started_at_utc": row["started_at_utc"],
                    "ended_at_utc": row["ended_at_utc"],
                    "status": row["status"],
                    "upstream_http_status": _int(row["upstream_http_status"]),
                    "normalized_error_code": row["normalized_error_code"],
                    "retry_after_seconds": _int(row["retry_after_seconds"]),
                    "usage": {
                        "input_tokens": _int(row["input_tokens"]),
                        "output_tokens": _int(row["output_tokens"]),
                        "cache_read_tokens": _int(row["cache_read_tokens"]),
                        "cache_write_tokens": _int(row["cache_write_tokens"]),
                        "reasoning_output_tokens": _int(row["reasoning_output_tokens"]),
                        "source": row["usage_source"],
                        "completeness": row["usage_completeness"],
                    },
                    "duration_ms": _int(row["duration_ms"]),
                    "ttft_ms": _int(row["ttft_ms"]),
                    "estimated_cost_usd": row["estimated_cost_usd"],
                    "cost_confidence": row["cost_confidence"],
                }
                for row in (attempts or [])
            ],
            "note": (
                "This trace holds metadata only. Request and response text are never "
                "stored."
            ),
        }

    async def export_rows(
        self,
        *,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
        include_probes: bool = False,
        limit: int = _MAX_LIMIT,
    ) -> list[dict[str, object]]:
        cap = normalize_limit(limit)
        active = _Filter(start, end, filters, include_probes)
        request_where, request_params = active.request_sql()

        def rows(connection):
            return connection.execute(
                "SELECT r.request_id, r.started_at_utc, r.client_type, r.requested_model,"
                " r.status, r.normalized_error_code, r.duration_ms, r.fallback_count, "
                "a.attempt_number, a.provider_id, a.upstream_model_id, a.status, "
                "a.upstream_http_status, a.input_tokens, a.output_tokens, "
                "a.cache_read_tokens, a.cache_write_tokens, a.usage_source, "
                "a.estimated_cost_usd, a.cost_confidence "
                "FROM logical_requests r "
                "LEFT JOIN upstream_attempts a ON a.request_id = r.request_id"
                + request_where
                + " ORDER BY r.started_at_utc DESC LIMIT ?",
                (*request_params, cap),
            ).fetchall()

        data = await self._one(rows)
        return [
            {
                "request_id": row["request_id"],
                "started_at_utc": row["started_at_utc"],
                "client_type": row["client_type"],
                "requested_model": row["requested_model"],
                "request_status": row["status"],
                "normalized_error_code": row["normalized_error_code"],
                "duration_ms": _int(row["duration_ms"]),
                "fallback_count": _int(row["fallback_count"]) or 0,
                "attempt_number": _int(row["attempt_number"]),
                "provider_id": row["provider_id"],
                "upstream_model_id": row["upstream_model_id"],
                "attempt_status": (
                    row["status"] if row["attempt_number"] is not None else None
                ),
                "upstream_http_status": _int(row["upstream_http_status"]),
                "input_tokens": _int(row["input_tokens"]),
                "output_tokens": _int(row["output_tokens"]),
                "cache_read_tokens": _int(row["cache_read_tokens"]),
                "cache_write_tokens": _int(row["cache_write_tokens"]),
                "usage_source": row["usage_source"],
                "estimated_cost_usd": row["estimated_cost_usd"],
                "cost_confidence": row["cost_confidence"],
            }
            for row in (data or [])
        ]


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _decimal(value) -> str | None:
    if value is None:
        return None
    try:
        return f"{float(value):.6f}"
    except TypeError, ValueError:
        return None


def _percentiles(values: Sequence[object]) -> dict[str, object]:
    numbers = sorted(
        int(value) for value in values if isinstance(value, int | float | str)
    )
    if not numbers:
        return {"p50_ms": None, "p95_ms": None, "samples": 0}
    return {
        "p50_ms": numbers[int(0.50 * (len(numbers) - 1))],
        "p95_ms": numbers[int(0.95 * (len(numbers) - 1))],
        "samples": len(numbers),
    }


def _cost_confidence(priced: int, unpriced: int) -> str:
    if not priced and not unpriced:
        return "unknown"
    if unpriced == 0:
        return "verified"
    if priced == 0:
        return "unknown"
    return "partial"


__all__ = [
    "AnalyticsQueries",
    "AnalyticsRangeError",
    "normalize_limit",
    "resolve_range",
    "validate_filters",
]

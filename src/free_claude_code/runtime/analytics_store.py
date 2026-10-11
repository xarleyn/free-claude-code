"""SQLite accounting for logical requests and upstream attempts.

Two levels are stored, never collapsed: one *logical request* per client call, and one
*upstream attempt* per actual provider send. A retry or fallback therefore adds an
attempt row and leaves the request row at one.

Everything written here is metadata. There is deliberately no column for a prompt, a
response, a tool argument, a header, an API key or a credential, and no mechanism to
add one: the writer only accepts the typed records defined in
``free_claude_code.application.analytics``.
"""

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from loguru import logger

from free_claude_code.application.analytics.models import (
    AttemptStatus,
    ClientType,
    LogicalRequestRecord,
    Origin,
    RequestStatus,
    UpstreamAttemptRecord,
    UsageSource,
    WireProtocol,
    utc_now,
)
from free_claude_code.application.free_guard.models import utc_text

from .sqlite_database import SQLiteDatabase

_REQUEST_COLUMNS = (
    "request_id",
    "started_at_utc",
    "ended_at_utc",
    "origin",
    "client_type",
    "wire_protocol",
    "requested_model",
    "resolved_provider_id",
    "resolved_upstream_model_id",
    "status",
    "normalized_error_code",
    "duration_ms",
    "ttft_ms",
    "request_streamed",
    "fallback_count",
    "retry_count",
    "final_attempt_id",
    "client_label",
)

_ATTEMPT_COLUMNS = (
    "attempt_id",
    "request_id",
    "attempt_number",
    "provider_id",
    "upstream_model_id",
    "fallback_from_attempt_id",
    "started_at_utc",
    "ended_at_utc",
    "status",
    "upstream_http_status",
    "normalized_error_code",
    "retry_after_seconds",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_output_tokens",
    "usage_source",
    "usage_completeness",
    "duration_ms",
    "ttft_ms",
    "price_snapshot_id",
    "estimated_cost_usd",
    "cost_confidence",
)


def _text(moment) -> str | None:
    return utc_text(moment) if moment is not None else None


def _request_values(record: LogicalRequestRecord) -> tuple[object, ...]:
    return (
        record.request_id,
        _text(record.started_at_utc) or utc_text(utc_now()),
        _text(record.ended_at_utc),
        _enum(record.origin, Origin, Origin.USER),
        _enum(record.client_type, ClientType, ClientType.UNKNOWN),
        _enum(record.wire_protocol, WireProtocol, WireProtocol.OTHER),
        record.requested_model,
        record.resolved_provider_id,
        record.resolved_upstream_model_id,
        _enum(record.status, RequestStatus, RequestStatus.SUCCESS),
        record.normalized_error_code,
        record.duration_ms,
        record.ttft_ms,
        int(bool(record.request_streamed)),
        int(record.fallback_count),
        int(record.retry_count),
        record.final_attempt_id,
        record.client_label,
    )


def _attempt_values(record: UpstreamAttemptRecord) -> tuple[object, ...]:
    return (
        record.attempt_id,
        record.request_id,
        int(record.attempt_number),
        record.provider_id,
        record.upstream_model_id,
        record.fallback_from_attempt_id,
        _text(record.started_at_utc) or utc_text(utc_now()),
        _text(record.ended_at_utc),
        _enum(record.status, AttemptStatus, AttemptStatus.SUCCESS),
        record.upstream_http_status,
        record.normalized_error_code,
        record.retry_after_seconds,
        record.input_tokens,
        record.output_tokens,
        record.cache_read_tokens,
        record.cache_write_tokens,
        record.reasoning_output_tokens,
        _enum(record.usage_source, UsageSource, UsageSource.UNKNOWN),
        record.usage_completeness.value,
        record.duration_ms,
        record.ttft_ms,
        record.price_snapshot_id,
        record.estimated_cost_usd,
        record.cost_confidence.value,
    )


def _enum(value, enum_type, default):
    try:
        return enum_type(value).value
    except ValueError, KeyError:
        return default.value


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


class AnalyticsStore:
    """All analytics SQL lives behind this class."""

    def __init__(self, database: SQLiteDatabase | None = None) -> None:
        self._database = database

    @property
    def durable(self) -> bool:
        return self._database is not None

    # --------------------------------------------------------------- writers

    async def upsert_request(self, record: LogicalRequestRecord) -> None:
        if self._database is None:
            return
        await self._write(
            lambda connection: connection.execute(
                "INSERT INTO logical_requests ("
                + ", ".join(_REQUEST_COLUMNS)
                + ") VALUES ("
                + ", ".join("?" for _ in _REQUEST_COLUMNS)
                + ") ON CONFLICT(request_id) DO UPDATE SET "
                + ", ".join(
                    f"{column} = excluded.{column}" for column in _REQUEST_COLUMNS[1:]
                ),
                _request_values(record),
            )
        )

    async def upsert_attempts(
        self,
        request: LogicalRequestRecord,
        attempts: Sequence[UpstreamAttemptRecord],
    ) -> None:
        """Write attempts and, in the same transaction, their parent request row.

        Ordering inside one transaction keeps the ``upstream_attempts`` reference
        valid even when the parent row was never written by an earlier flush.
        """

        if self._database is None or not attempts:
            return

        def transaction(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO logical_requests (request_id, started_at_utc, origin, "
                "client_type, wire_protocol, status) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(request_id) DO NOTHING",
                (
                    request.request_id,
                    _text(request.started_at_utc) or utc_text(utc_now()),
                    request.origin.value,
                    request.client_type.value,
                    request.wire_protocol.value,
                    request.status.value,
                ),
            )
            for attempt in attempts:
                connection.execute(
                    "INSERT INTO upstream_attempts ("
                    + ", ".join(_ATTEMPT_COLUMNS)
                    + ") VALUES ("
                    + ", ".join("?" for _ in _ATTEMPT_COLUMNS)
                    + ") ON CONFLICT(attempt_id) DO UPDATE SET "
                    + ", ".join(
                        f"{column} = excluded.{column}"
                        for column in _ATTEMPT_COLUMNS[1:]
                    ),
                    _attempt_values(attempt),
                )

        await self._write(transaction)

    async def mark_abandoned(self, cutoff: datetime) -> int:
        """Mark requests that never finalized as abandoned, inventing nothing."""

        if self._database is None:
            return 0

        def transaction(connection: sqlite3.Connection) -> int:
            rows = connection.execute(
                "SELECT request_id FROM logical_requests "
                "WHERE ended_at_utc IS NULL AND started_at_utc < ?",
                (utc_text(cutoff),),
            ).fetchall()
            ids = [row[0] for row in rows]
            for request_id in ids:
                connection.execute(
                    "UPDATE logical_requests SET status = ?, ended_at_utc = ? "
                    "WHERE request_id = ?",
                    (
                        RequestStatus.ERROR.value,
                        utc_text(cutoff),
                        request_id,
                    ),
                )
                connection.execute(
                    "UPDATE upstream_attempts SET status = ? "
                    "WHERE request_id = ? AND ended_at_utc IS NULL",
                    (AttemptStatus.ABANDONED.value, request_id),
                )
            return len(ids)

        return await self._write(transaction)

    async def prune(self, *, raw_cutoff: datetime, rollup_cutoff: datetime) -> int:
        if self._database is None:
            return 0

        def transaction(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM logical_requests WHERE started_at_utc < ?",
                (utc_text(raw_cutoff),),
            )
            connection.execute(
                "DELETE FROM analytics_daily_rollups WHERE day_utc < ?",
                (rollup_cutoff.strftime("%Y-%m-%d"),),
            )
            return int(cursor.rowcount or 0)

        return await self._write(transaction)

    async def _write[T](self, operation) -> T:
        database = self._database
        if database is None:
            raise sqlite3.OperationalError("Analytics storage is unavailable")
        try:
            return await database.run(operation)
        except sqlite3.Error as exc:
            logger.warning(
                "Analytics write failed (event dropped): exc_type={}",
                type(exc).__name__,
            )
            raise

    async def _read[T](self, operation) -> T:
        database = self._database
        if database is None:
            raise sqlite3.OperationalError("Analytics storage is unavailable")
        return await database.run(operation, write=False)

    # ---------------------------------------------------------------- readers

    async def query[T](self, operation) -> T:
        """Run one parameterized read operation against the reader pool."""

        if self._database is None:
            return operation(_NullConnection())
        return await self._read(operation)

    async def write_operation[T](self, operation) -> T:
        """Run one parameterized write operation (retention, rollups)."""

        if self._database is None:
            return operation(_NullConnection())
        return await self._write(operation)

    async def table_present(self, name: str) -> bool:
        if self._database is None:
            return False
        try:
            row = await self._read(
                lambda connection: connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
                    (name,),
                ).fetchone()
            )
        except sqlite3.Error:
            return False
        return row is not None


class _NullConnection:
    """A no-op connection used when storage is unavailable."""

    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("Analytics storage is unavailable")

    def executemany(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("Analytics storage is unavailable")


def day_bucket(moment: datetime) -> str:
    return utc_text(moment)[:10]


def minute_bucket(moment: datetime) -> str:
    return utc_text(moment)[:16]

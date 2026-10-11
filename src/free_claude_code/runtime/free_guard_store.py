"""Durable FreeGuard eligibility evidence storage.

Evidence lives in FCC's shared SQLite database next to Code sessions and messaging
state, so there is exactly one storage file. Reads are bulk-loaded into an in-memory
snapshot by :class:`free_claude_code.runtime.free_guard_service.FreeGuardService`
before a request is served, which keeps the hot path free of SQLite latency and means
a storage failure can never stall or slow a request.

When no database is available, evidence simply never loads: the snapshot stays empty,
every model is ``unknown`` and strict mode blocks it. Losing storage must never look
like losing the guard, so that is the intended fail-closed direction.
"""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from loguru import logger

from free_claude_code.application.free_guard.models import (
    AccessStatus,
    BillingClass,
    EligibilityRecord,
    EvidenceSource,
    PricingSnapshot,
    ReasonCode,
    parse_utc_text,
    utc_now,
    utc_text,
)

from .sqlite_database import SQLiteDatabase

_RECORD_COLUMNS = (
    "provider_id",
    "upstream_model_id",
    "account_binding_id",
    "billing_class",
    "access_status",
    "card_required",
    "topup_required",
    "subscription_required",
    "trial_or_promo",
    "evidence_json",
    "evidence_source",
    "evidence_checked_at_utc",
    "evidence_expires_at_utc",
    "last_probe_at_utc",
    "blocked_reason_code",
    "manually_approved",
    "manual_attestation_url",
    "manual_attestation_by",
    "manual_attestation_at_utc",
)

_TABLE = "model_eligibility"


def _row_to_record(row: sqlite3.Row) -> EligibilityRecord:
    return EligibilityRecord(
        provider_id=row["provider_id"],
        upstream_model_id=row["upstream_model_id"],
        account_binding_id=row["account_binding_id"],
        billing_class=_enum(row["billing_class"], BillingClass, BillingClass.UNKNOWN),
        access_status=_enum(row["access_status"], AccessStatus, AccessStatus.UNKNOWN),
        requires_card=bool(row["card_required"]),
        requires_topup=bool(row["topup_required"]),
        requires_subscription=bool(row["subscription_required"]),
        uses_trial_or_promotional_balance=bool(row["trial_or_promo"]),
        evidence_json=row["evidence_json"] or "{}",
        evidence_source=_enum(
            row["evidence_source"],
            EvidenceSource,
            EvidenceSource.UNVERIFIED_LIST,
        ),
        evidence_checked_at_utc=_parse(row["evidence_checked_at_utc"]),
        evidence_expires_at_utc=_parse(row["evidence_expires_at_utc"]),
        last_probe_at_utc=_parse(row["last_probe_at_utc"]),
        blocked_reason_code=_optional_enum(row["blocked_reason_code"], ReasonCode),
        manually_approved=bool(row["manually_approved"]),
        manual_attestation_url=row["manual_attestation_url"],
        manual_attestation_by=row["manual_attestation_by"],
        manual_attestation_at_utc=_parse(row["manual_attestation_at_utc"]),
    )


def _enum(value, enum_type, default):
    try:
        return enum_type(value)
    except ValueError, KeyError:
        return default


def _optional_enum(value, enum_type):
    if value is None:
        return None
    return _enum(value, enum_type, None)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_text(value)
    except ValueError:
        return utc_now()


def _text(moment) -> str | None:
    return utc_text(moment) if moment is not None else None


class EligibilityStore:
    """Async SQLite storage for eligibility evidence, prices and audit history."""

    def __init__(self, database: SQLiteDatabase | None = None) -> None:
        self._database = database

    @property
    def durable(self) -> bool:
        """Whether this store can persist anything at all."""

        return self._database is not None

    async def load_all(self) -> tuple[EligibilityRecord, ...]:
        """Return every stored record, or nothing when storage is unavailable."""

        if self._database is None:
            return ()
        try:
            return await self._database.run(_select_all, write=False)
        except sqlite3.Error as exc:
            logger.warning(
                "FreeGuard evidence read failed; models stay unverified: exc_type={}",
                type(exc).__name__,
            )
            return ()

    async def save(self, record: EligibilityRecord) -> EligibilityRecord:
        """Insert or update one record and append an audit entry."""

        if self._database is None:
            return record
        previous = await self._database.run(
            lambda connection: _select_one(
                connection,
                record.provider_id,
                record.upstream_model_id,
                record.account_binding_id,
            ),
            write=False,
        )

        def transaction(connection: sqlite3.Connection) -> EligibilityRecord:
            _upsert(connection, record)
            _insert_audit(connection, record, previous)
            return record

        try:
            return await self._database.run(transaction)
        except sqlite3.Error as exc:
            logger.warning(
                "FreeGuard evidence write failed: provider={} exc_type={}",
                record.provider_id,
                type(exc).__name__,
            )
            raise

    async def delete(
        self, provider_id: str, upstream_model_id: str, account_binding_id: str
    ) -> None:
        """Drop one record so the model returns to ``unknown``."""

        if self._database is None:
            return

        def transaction(connection: sqlite3.Connection) -> None:
            connection.execute(
                f"DELETE FROM {_TABLE} WHERE provider_id = ? AND upstream_model_id = ? "
                "AND account_binding_id = ?",
                (provider_id, upstream_model_id, account_binding_id),
            )
            _insert_audit_event(
                connection,
                provider_id=provider_id,
                upstream_model_id=upstream_model_id,
                event="revoke",
                prior_status=None,
                next_status=None,
                reason_code=None,
                evidence_source=None,
            )

        try:
            await self._database.run(transaction)
        except sqlite3.Error as exc:
            logger.warning(
                "FreeGuard evidence delete failed: provider={} exc_type={}",
                provider_id,
                type(exc).__name__,
            )

    async def recent_audit(self, *, limit: int = 200) -> tuple[dict[str, object], ...]:
        if self._database is None:
            return ()
        try:
            return await self._database.run(
                lambda connection: _select_audit(connection, limit), write=False
            )
        except sqlite3.Error:
            return ()

    async def save_price_snapshot(
        self,
        *,
        provider_id: str,
        upstream_model_id: str,
        price_info_json: str,
        currency: str = "USD",
        source_url: str | None = None,
    ) -> PricingSnapshot | None:
        if self._database is None:
            return None
        fetched = utc_now()
        try:
            return await self._database.run(
                lambda connection: _insert_price(
                    connection,
                    provider_id=provider_id,
                    upstream_model_id=upstream_model_id,
                    price_info_json=price_info_json,
                    currency=currency,
                    source_url=source_url,
                    fetched=fetched,
                )
            )
        except sqlite3.Error:
            return None

    async def latest_price_snapshot(
        self, provider_id: str, upstream_model_id: str
    ) -> PricingSnapshot | None:
        if self._database is None:
            return None
        try:
            return await self._database.run(
                lambda connection: _select_price(
                    connection, provider_id, upstream_model_id
                ),
                write=False,
            )
        except sqlite3.Error:
            return None


def expired_records(
    records: Sequence[EligibilityRecord], *, now: datetime | None = None
) -> tuple[EligibilityRecord, ...]:
    """Records whose evidence TTL has elapsed (safe to re-attest)."""

    from free_claude_code.application.free_guard.models import utc_now as _now

    moment = now or _now()
    return tuple(
        record
        for record in records
        if record.evidence_expires_at_utc is None
        or record.evidence_expires_at_utc <= moment
    )


def reattested(record: EligibilityRecord, *, expires_at) -> EligibilityRecord:
    """Return one record with a refreshed expiry, used after a successful check."""

    return replace(record, evidence_expires_at_utc=expires_at)


def _select_all(connection: sqlite3.Connection) -> tuple[EligibilityRecord, ...]:
    rows = connection.execute(
        f"SELECT {', '.join(_RECORD_COLUMNS)} FROM {_TABLE}"
    ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def _select_one(
    connection: sqlite3.Connection,
    provider_id: str,
    upstream_model_id: str,
    account_binding_id: str,
) -> EligibilityRecord | None:
    row = connection.execute(
        f"SELECT {', '.join(_RECORD_COLUMNS)} FROM {_TABLE} "
        "WHERE provider_id = ? AND upstream_model_id = ? AND account_binding_id = ?",
        (provider_id, upstream_model_id, account_binding_id),
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def _select_audit(
    connection: sqlite3.Connection, limit: int
) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        "SELECT at_utc, provider_id, upstream_model_id, event, prior_status, "
        "next_status, reason_code, evidence_source FROM eligibility_audit "
        "ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return tuple(
        {
            "at_utc": row["at_utc"],
            "provider_id": row["provider_id"],
            "upstream_model_id": row["upstream_model_id"],
            "event": row["event"],
            "prior_status": row["prior_status"],
            "next_status": row["next_status"],
            "reason_code": row["reason_code"],
            "evidence_source": row["evidence_source"],
        }
        for row in rows
    )


def _upsert(connection: sqlite3.Connection, record: EligibilityRecord) -> None:
    columns = _RECORD_COLUMNS
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns[3:])
    connection.execute(
        f"INSERT INTO {_TABLE} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(provider_id, upstream_model_id, account_binding_id) "
        f"DO UPDATE SET {updates}",
        (
            record.provider_id,
            record.upstream_model_id,
            record.account_binding_id,
            record.billing_class.value,
            record.access_status.value,
            int(record.requires_card),
            int(record.requires_topup),
            int(record.requires_subscription),
            int(record.uses_trial_or_promotional_balance),
            record.evidence_json,
            record.evidence_source.value,
            _text(record.evidence_checked_at_utc) or utc_text(utc_now()),
            _text(record.evidence_expires_at_utc),
            _text(record.last_probe_at_utc),
            record.blocked_reason_code.value if record.blocked_reason_code else None,
            int(record.manually_approved),
            record.manual_attestation_url,
            record.manual_attestation_by,
            _text(record.manual_attestation_at_utc),
        ),
    )


def _insert_audit(
    connection: sqlite3.Connection,
    record: EligibilityRecord,
    previous: EligibilityRecord | None,
    event: str = "upsert",
) -> None:
    _insert_audit_event(
        connection,
        provider_id=record.provider_id,
        upstream_model_id=record.upstream_model_id,
        event=event,
        prior_status=(previous.billing_class.value if previous is not None else None),
        next_status=record.billing_class.value,
        reason_code=(
            record.blocked_reason_code.value if record.blocked_reason_code else None
        ),
        evidence_source=record.evidence_source.value,
    )


def _insert_audit_event(
    connection: sqlite3.Connection,
    *,
    provider_id: str,
    upstream_model_id: str,
    event: str,
    prior_status: str | None,
    next_status: str | None,
    reason_code: str | None,
    evidence_source: str | None,
) -> None:
    connection.execute(
        "INSERT INTO eligibility_audit "
        "(at_utc, provider_id, upstream_model_id, event, prior_status, next_status, "
        "reason_code, evidence_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            utc_text(utc_now()),
            provider_id,
            upstream_model_id,
            event,
            prior_status,
            next_status,
            reason_code,
            evidence_source,
        ),
    )


def _insert_price(
    connection: sqlite3.Connection,
    *,
    provider_id: str,
    upstream_model_id: str,
    price_info_json: str,
    currency: str,
    source_url: str | None,
    fetched: datetime,
) -> PricingSnapshot:
    cursor = connection.execute(
        "INSERT INTO pricing_snapshots "
        "(provider_id, upstream_model_id, price_info_json, currency, source_url, "
        "fetched_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
        (
            provider_id,
            upstream_model_id,
            price_info_json,
            currency,
            source_url,
            utc_text(fetched),
        ),
    )
    return PricingSnapshot(
        id=int(cursor.lastrowid or 0),
        provider_id=provider_id,
        upstream_model_id=upstream_model_id,
        price_info_json=price_info_json,
        currency=currency,
        source_url=source_url,
        fetched_at_utc=fetched,
    )


def _select_price(
    connection: sqlite3.Connection, provider_id: str, upstream_model_id: str
) -> PricingSnapshot | None:
    row = connection.execute(
        "SELECT id, provider_id, upstream_model_id, price_info_json, currency, "
        "source_url, fetched_at_utc FROM pricing_snapshots "
        "WHERE provider_id = ? AND upstream_model_id = ? ORDER BY id DESC LIMIT 1",
        (provider_id, upstream_model_id),
    ).fetchone()
    if row is None:
        return None
    return PricingSnapshot(
        id=row["id"],
        provider_id=row["provider_id"],
        upstream_model_id=row["upstream_model_id"],
        price_info_json=row["price_info_json"],
        currency=row["currency"],
        source_url=row["source_url"],
        fetched_at_utc=_parse(row["fetched_at_utc"]),
    )


def parse_price_info(price_info_json: str) -> dict[str, float]:
    """Parse a stored price payload into floats, ignoring anything malformed."""

    try:
        payload = json.loads(price_info_json)
    except ValueError, TypeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in payload.items():
        try:
            result[str(key)] = float(value)
        except TypeError, ValueError:
            continue
    return result


__all__ = [
    "EligibilityStore",
    "expired_records",
    "parse_price_info",
    "reattested",
]

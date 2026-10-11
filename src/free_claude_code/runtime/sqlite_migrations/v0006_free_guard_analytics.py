"""FreeGuard eligibility state and analytics accounting tables.

Published migrations are immutable; this one creates the FreeGuard evidence tables
(:mod:`free_claude_code.runtime.free_guard_store`) and the analytics accounting tables
(:mod:`free_claude_code.runtime.analytics_store`) inside FCC's existing shared
database, so no second competing storage file exists.

No prompt text, response text, credential, or authorization header has a column here.
``evidence_json`` carries provider/model/billing metadata only.
"""

import sqlite3

_TABLES: tuple[tuple[str, str], ...] = (
    (
        "model_eligibility",
        """
        CREATE TABLE IF NOT EXISTS model_eligibility (
          id INTEGER PRIMARY KEY,
          provider_id TEXT NOT NULL,
          upstream_model_id TEXT NOT NULL,
          account_binding_id TEXT NOT NULL,
          billing_class TEXT NOT NULL,
          access_status TEXT NOT NULL,
          card_required INTEGER NOT NULL DEFAULT 0,
          topup_required INTEGER NOT NULL DEFAULT 0,
          subscription_required INTEGER NOT NULL DEFAULT 0,
          trial_or_promo INTEGER NOT NULL DEFAULT 0,
          evidence_json TEXT NOT NULL,
          evidence_source TEXT NOT NULL DEFAULT 'unknown',
          evidence_checked_at_utc TEXT NOT NULL,
          evidence_expires_at_utc TEXT,
          last_probe_at_utc TEXT,
          blocked_reason_code TEXT,
          manually_approved INTEGER NOT NULL DEFAULT 0,
          manual_attestation_url TEXT,
          manual_attestation_by TEXT,
          manual_attestation_at_utc TEXT
        )
        """,
    ),
    (
        "model_eligibility_key",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS model_eligibility_key
          ON model_eligibility(provider_id, upstream_model_id, account_binding_id)
        """,
    ),
    (
        "pricing_snapshots",
        """
        CREATE TABLE IF NOT EXISTS pricing_snapshots (
          id INTEGER PRIMARY KEY,
          provider_id TEXT NOT NULL,
          upstream_model_id TEXT NOT NULL,
          price_info_json TEXT NOT NULL,
          currency TEXT NOT NULL DEFAULT 'USD',
          source_url TEXT,
          fetched_at_utc TEXT NOT NULL
        )
        """,
    ),
    (
        "eligibility_audit",
        """
        CREATE TABLE IF NOT EXISTS eligibility_audit (
          id INTEGER PRIMARY KEY,
          at_utc TEXT NOT NULL,
          provider_id TEXT NOT NULL,
          upstream_model_id TEXT NOT NULL,
          event TEXT NOT NULL,
          prior_status TEXT,
          next_status TEXT,
          reason_code TEXT,
          evidence_source TEXT
        )
        """,
    ),
    (
        "logical_requests",
        """
        CREATE TABLE IF NOT EXISTS logical_requests (
          request_id TEXT PRIMARY KEY,
          started_at_utc TEXT NOT NULL,
          ended_at_utc TEXT,
          origin TEXT NOT NULL,
          client_type TEXT NOT NULL,
          wire_protocol TEXT NOT NULL,
          requested_model TEXT,
          resolved_provider_id TEXT,
          resolved_upstream_model_id TEXT,
          status TEXT NOT NULL,
          normalized_error_code TEXT,
          duration_ms INTEGER,
          ttft_ms INTEGER,
          request_streamed INTEGER NOT NULL DEFAULT 0,
          fallback_count INTEGER NOT NULL DEFAULT 0,
          retry_count INTEGER NOT NULL DEFAULT 0,
          final_attempt_id TEXT,
          client_label TEXT
        )
        """,
    ),
    (
        "upstream_attempts",
        """
        CREATE TABLE IF NOT EXISTS upstream_attempts (
          attempt_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL REFERENCES logical_requests(request_id),
          attempt_number INTEGER NOT NULL,
          provider_id TEXT NOT NULL,
          upstream_model_id TEXT NOT NULL,
          fallback_from_attempt_id TEXT,
          started_at_utc TEXT NOT NULL,
          ended_at_utc TEXT,
          status TEXT NOT NULL,
          upstream_http_status INTEGER,
          normalized_error_code TEXT,
          retry_after_seconds INTEGER,
          input_tokens INTEGER,
          output_tokens INTEGER,
          cache_read_tokens INTEGER,
          cache_write_tokens INTEGER,
          reasoning_output_tokens INTEGER,
          usage_source TEXT NOT NULL DEFAULT 'unknown',
          usage_completeness TEXT NOT NULL DEFAULT 'unknown',
          duration_ms INTEGER,
          ttft_ms INTEGER,
          price_snapshot_id INTEGER,
          estimated_cost_usd TEXT,
          cost_confidence TEXT NOT NULL DEFAULT 'unknown'
        )
        """,
    ),
    (
        "logical_requests_time",
        """
        CREATE INDEX IF NOT EXISTS logical_requests_time
          ON logical_requests(started_at_utc)
        """,
    ),
    (
        "logical_requests_filters",
        """
        CREATE INDEX IF NOT EXISTS logical_requests_filters
          ON logical_requests(started_at_utc, client_type, status)
        """,
    ),
    (
        "upstream_attempts_provider",
        """
        CREATE INDEX IF NOT EXISTS upstream_attempts_provider
          ON upstream_attempts(provider_id, upstream_model_id, started_at_utc)
        """,
    ),
    (
        "upstream_attempts_order",
        """
        CREATE INDEX IF NOT EXISTS upstream_attempts_order
          ON upstream_attempts(request_id, attempt_number)
        """,
    ),
    (
        "analytics_daily_rollups",
        """
        CREATE TABLE IF NOT EXISTS analytics_daily_rollups (
          day_utc TEXT NOT NULL,
          provider_id TEXT NOT NULL,
          upstream_model_id TEXT NOT NULL,
          client_type TEXT NOT NULL,
          status TEXT NOT NULL,
          origin TEXT NOT NULL DEFAULT 'user',
          logical_requests INTEGER NOT NULL DEFAULT 0,
          upstream_attempts INTEGER NOT NULL DEFAULT 0,
          fallback_requests INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          blocked_count INTEGER NOT NULL DEFAULT 0,
          input_tokens_known INTEGER NOT NULL DEFAULT 0,
          output_tokens_known INTEGER NOT NULL DEFAULT 0,
          cache_read_tokens_known INTEGER NOT NULL DEFAULT 0,
          cache_write_tokens_known INTEGER NOT NULL DEFAULT 0,
          unknown_usage_attempts INTEGER NOT NULL DEFAULT 0,
          estimated_cost_usd TEXT,
          PRIMARY KEY (
            day_utc, provider_id, upstream_model_id, client_type, status, origin
          )
        )
        """,
    ),
)


def upgrade(connection: sqlite3.Connection) -> None:
    for _name, statement in _TABLES:
        connection.execute(statement)

# Changelog

All notable changes to this fork are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project uses date-based
versions, so the newest release is at the top.

## [Unreleased]

FreeGuard and local usage analytics, per `FCC_FREEGUARD_ANALYTICS_SPEC.md` v1.0.

### Added

- **FreeGuard**: deny-by-default free-only routing. A new `ModelEligibilityPolicy` and
  `RoutingGuard` in `application/free_guard/policy.py` re-check the exact
  `provider_id + upstream_model_id + account_binding_id` pair immediately before every
  upstream call, on direct model ids, aliases/shortcuts, retries, fallbacks and stream
  hand-offs. A blocked model fails with `free_guard_blocked` (HTTP 403,
  `permission_error`) and **no HTTP request is sent**. `FCC_FREE_GUARD_MODE` selects
  `off` (default, legacy behaviour) / `catalog_filter_only` / `strict_free_only`.
- **Free-only semantics**: billing class and access status are separate from price. A
  `$0` catalog price never proves "no card". `requires_card`, `requires_topup`,
  `requires_subscription` and trial/promotional balance are all honoured, and
  `unknown` is always blocked in strict mode.
- **Purchase-lock classification**: a provider failure carrying
  `model_requires_purchase` — even inside HTTP `429 insufficient_quota` — is stored as
  `requires_purchase` + `payment_locked` and never retried. A genuine rate limit stays
  a cooldown (`temporarily_rate_limited`) and does not reclassify the model as paid.
- **Eligibility storage and evidence**: `model_eligibility`, `pricing_snapshots` and
  `eligibility_audit` tables in FCC's existing SQLite database, with adapters for
  Groq, OpenRouter, OpenCode Zen and Experiential Labs plus a generic `unknown`
  adapter. Account identity is a non-secret stable fingerprint, so changing an API key
  invalidates that account's proof. Manual attestation (with URL, who and when) in
  Admin is the only route to an eligible free model.
- **Filtered catalogs**: `/v1/models`, `/muse-code/models` and the Codex catalog
  publisher hide models the policy has not cleared in `catalog_filter_only` and
  `strict_free_only`, so no client is invited into a request that would be refused.
- **Analytics**: one `logical_requests` row per client request and one
  `upstream_attempts` row per actual provider send, so retries and fallbacks no longer
  inflate the request count. Records UTC timestamps, client type (from reliable signals
  only), requested/resolved model, status, normalized error code, retry-after, UTC
  duration and TTFT, input/output/cache/reasoning tokens with source and completeness,
  estimated cost with confidence, and fallback/retry counts. No prompts, responses,
  credentials or auth headers are stored anywhere.
- **Analytics read models and API**: `/admin/api/analytics/summary`, `/timeseries`,
  `/breakdown`, `/requests`, `/requests/{id}`, `/export` and `/diagnostics`. All
  loopback-admin only, `Cache-Control: no-store`, allowlisted filters, parameterized
  SQL, `limit <= 200`, and UTC RFC3339 ranges with a configurable maximum.
- **Admin tabs**: new **Analytics** tab (KPIs, timeline chart, request history with a
  trace drawer, filters, auto-refresh, export) and **Free Models** tab (mode, category
  counts, eligibility table with reasons, attest/revoke, refresh). Rendered with
  server-rendered HTML/JS and inline SVG charts; no external CDN, no SPA rewrite.
- **Health/Diagnostics support**: `runtime/diagnostics.py` classifies and reports the
  new settings; a writer failure increments a dropped-event counter instead of failing
  a request; unfinished requests are marked `abandoned` on startup without inventing
  token counts; daily rollups and raw retention (90 days) / rollup retention (365 days)
  are configurable.
- Documentation: `docs/architecture-decisions/2026-10-11-freeguard-analytics-audit.md`
  (spec-to-code map, integration points, verification) and README sections for both
  features.

### Changed

- `core/failures.py` is unchanged. `GuardBlockedError` subclasses `ExecutionFailure`
  instead, so no new `FailureKind` value and no wire-protocol change for existing
  Claude Code, Codex, VS Code Chat, DSH or OpenCode clients.
- `application/execution.py` accepts an optional `routing_guard` and `analytics` sink
  and enforces both around the existing candidate loop, leaving all existing behaviour
  intact when neither is configured.
- `api/model_catalog.py` gained `filter_catalog`; the default (guard off) path is
  byte-for-byte the previous behaviour.
- `runtime/sqlite_database.py` recognises the FreeGuard/analytics tables from schema
  version 6.

### Fixed

- A provider reporting `model_requires_purchase` inside an HTTP `429` was previously
  rendered as a plain rate limit, so an automatic retry could hit a paid model again.
- A single client request that retried or fell back was counted as multiple user
  requests; upstream attempts were not separated from logical requests.
- Provider-reported usage that arrived more than once in one stream could be counted
  twice; token fields are now merged once and unknown usage is never rendered as `0`.

[Unreleased]: https://github.com/Alishahryar1/free-claude-code/compare/main...main

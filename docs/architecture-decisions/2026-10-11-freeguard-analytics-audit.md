# FreeGuard & Analytics — implementation audit of this fork

Audit date: 2026-10-11 · Audited commit: `fb3a7bb6` (`patch: update dependency minimum versions (#2028)`)
Specification: `FCC_FREEGUARD_ANALYTICS_SPEC.md` v1.0

## 0. Why this document exists

The specification warns that its file names are *entry points for audit*, not guaranteed
patch locations. This document records what the fork actually contains, where each
requirement lands, and every deliberate deviation.

## 1. Request flow (real code paths)

```
POST /v1/messages            api/routes.py:create_message
POST /v1/responses           api/routes.py:create_response
POST /v1/messages/count_tokens  api/routes.py:count_tokens   (local; no upstream call)
GET  /v1/models              api/routes.py:list_models
        │
        ▼  services.requests.acquire()          → ProviderGenerationLease
       api/routes.py:_create_messages_response / _create_responses_response
        │   ModelRouter(lease.settings).resolve(model)
        ▼
   api/handlers/messages.py:MessagesHandler.create / create_native
   api/handlers/responses.py:ResponsesHandler.create
        │   (_intercept_local_optimization may answer without any provider)
        ▼
   application/execution.py:ProviderExecutor.stream_messages / stream_responses /
                            stream_native_messages
        │   → _stream_candidates(...)
        ▼
   for index, target in enumerate((primary, *fallbacks)):
       provider = await self._provider_resolver(target.provider_id)   ◀── the only
       provider.stream_messages(...) / stream_responses(...) /            outbound
           stream_native_messages(...)                                   call sites
```

Everything a client can reach funnels through
`ProviderExecutor._stream_candidates`. That is the single place where "one provider
attempt" exists, so it is the only correct place to enforce a pre-call policy check and
the only place where attempt-level telemetry is unambiguous.

## 2. spec → existing code map

| Spec § | Requirement | Where it lands in this fork |
|---|---|---|
| 2 | Settings, editable in Admin, applied without restart | `config/settings.py` (flat `FCC_*` fields), `config/admin/manifest.py` (Admin spec), `config/admin/values.py` (provenance). `ApplicationRuntime.apply_admin_config` → `ProviderRuntimeManager.replace` already hot-swaps `Settings` for new requests. |
| 3 | Billing/access model, evidence, adapters | new `application/free_guard/` package |
| 4.1 | `ModelEligibilityPolicy` + `RoutingGuard` in the application layer | new `application/free_guard/policy.py`, wired into `ProviderExecutor` |
| 4.2 | Route filtering + error shapes | `application/routing.py` (`ResolvedModelRoute`), new `GuardBlockedError` |
| 5 | Filtered client catalogs | `api/model_catalog.py` (`build_models_list_response`), `runtime/codex_catalog.py` (already publishes the catalog snapshot for Codex) |
| 6 | Event model | new `application/analytics/` + `runtime/analytics_*` |
| 7 | SQLite storage and migrations | `runtime/sqlite_database.py`, `runtime/sqlite_migrations/v0006_*` (new) |
| 8 | Admin UI | `api/admin_static/index.html`, `admin.js`, new `analytics.js` / `free_guard.js` / `analytics.css`. No SPA rewrite. |
| 9 | Admin API | new `api/free_guard_routes.py`, `api/analytics_routes.py`, both registered in `api/app.py` |
| 10 | Service composition | `runtime/bootstrap.py` (`build_asgi_app`), `runtime/application.py` (`ApplicationRuntime`), `api/ports.py` (`ApiServices`) |

### Existing mechanisms that are reused, not duplicated

- **Storage.** The fork already owns one SQLite database at
  `%LOCALAPPDATA%/.fcc/fcc.db` on Windows (`~/.fcc/fcc.db` elsewhere) opened by
  `runtime/sqlite_database.py` with WAL, a writer pool of 1, a reader pool of 4,
  `busy_timeout` via `timeout=10`, and an interprocess owner lock
  (`core/interprocess_lock.py`). The specification asks to reuse compatible storage
  instead of creating a second competing database, so FreeGuard/Analytics tables are
  added to that same file through the existing migration runner.
- **Settings provenance and sparse dotenv persistence.** `config/admin/*`.
- **Loopback admin guard.** `api/admin_security.py:require_loopback_admin`, plus
  `api/admin_cache.py:AdminNoStoreMiddleware`.
- **Trace/logging.** `core/trace.py:trace_event`.
- **Failure taxonomy.** `core/failures.py:ExecutionFailure` / `FailureKind`, which all
  wire adapters already understand.

## 3. Deliberate deviations (fail-closed choices)

1. **`free_guard.mode` defaults to `off`.** The specification's example YAML shows
   `mode: strict_free_only`. Enabling strict by default in a fork whose users already
   have working configurations would silently block every model for which no
   eligibility evidence exists yet — a regression for existing Claude Code, Codex,
   VS Code Chat, DSH and OpenCode clients. Strict free routing is therefore opt-in
   (Admin → Model Config, or `FCC_FREE_GUARD_MODE=strict_free_only`). Once enabled,
   the guard itself is deny-by-default. This is the only place where the fork fails
   *open*, and only for backwards compatibility.
2. **`analytics.enabled` defaults to `true`.** Analytics is additive: it writes to its
   own tables, never alters request behaviour, and stores no prompt/secret data. It can
   be switched off with `FCC_ANALYTICS_ENABLED=false`.
3. **No `FailureKind` was added.** A new enum member would have to be taught to every
   wire mapper (`core/anthropic/errors.py`, `core/openai_responses/errors.py`), and a
   brand-new Anthropic error type is a protocol change for existing clients. Guard
   denials are therefore `ExecutionFailure(kind=PERMISSION, status_code=403,
   retryable=False)` raised as `GuardBlockedError`, which carries the machine-readable
   `reason_code` and renders on both wires as a `permission_error` whose message starts
   with `free_guard_blocked:`. Clients see a stable, already-known error type.
4. **`estimated_cost_usd` is a decimal string, never a float**, and `NULL`/absent stays
   absent. Unknown usage is never rendered as `0`.
5. **`logical_requests.request_id` is not a foreign key target for deletes.** The
   `upstream_attempts.request_id` reference *is* declared, but the writer always
   upserts the parent row inside the same transaction before its attempts, so ordering
   inside the FIFO writer keeps the constraint satisfied.
6. **Rollups.** `analytics_daily_rollups` is a materialised daily aggregate recomputed
   by `runtime/analytics_service.py` and consulted only for ranges that reach past the
   raw window, per the specification's "only after measuring the need".

## 4. Test baseline

`pytest` (`-n auto --dist=worksteal`) plus optional Playwright e2e/smoke suites. See
`tests/` and `e2e/`. Live provider suites are opt-in markers (`live`, `provider`) and
are never executed by CI; new tests use mock transports only.

## 5. Implementation record

Phases from spec §15, in order, each a logical change:

| Phase | What landed |
|---|---|
| 0. Audit | This document, before any code. |
| 1. Storage & core types | `config/free_guard.py` (mode enum), `Settings` fields, `application/free_guard/models.py`, migration `v0006_free_guard_analytics`, `runtime/free_guard_store.py`. |
| 2. Evidence adapters | `application/free_guard/adapters.py` — documented static rules with URL + review date for Groq/OpenRouter/OpenCode Zen/Experiential Labs, a machine-readable price adapter for OpenRouter, and a generic `unknown` adapter for everything else. |
| 3. Guard in execution | `application/free_guard/policy.py` (`ModelEligibilityPolicy`, `RoutingGuard`), `application/free_guard/errors.py` (`GuardBlockedError`), both lines of defence inside `ProviderExecutor._stream_candidates` — the single choke point every ingress converges on. |
| 4. Filtered catalogs | `api/model_catalog.py:filter_catalog`, wired into `/v1/models`, `/muse-code/models`, and therefore the Codex catalog publisher. |
| 5. Telemetry instrumentation | `application/analytics/models.py`, `application/analytics/usage.py` (protocol-specific usage readers), `runtime/analytics_writer.py` (bounded queue, one worker, batched commits), `runtime/analytics_service.py`. |
| 6. Analytics API | `application/analytics/queries.py` (allowlisted filters, parameterized SQL only), `api/analytics_routes.py`. |
| 7. Analytics UI | `api/admin_static/analytics.js` + `analytics.css`, new `analytics` and `free_models` tabs in `admin.js`/`index.html`. No SPA rewrite. |
| 8. Hardening | Retention, `mark_abandoned`, daily rollups, `docs/architecture-decisions/`, README. |

### Enforcement points, verified by code path

Every ingress that can reach a provider goes through
`ProviderExecutor._stream_candidates`:

- `POST /v1/messages` → `MessagesHandler.create` / `create_native`
- `POST /v1/responses` → `ResponsesHandler.create`
- web tools (`/v1/messages` with `web_search`/`web_fetch`) → `WebToolService` → same executor

That single loop is where the route is built and where each candidate is opened, so
both guard lines are enforced for all of them at once, including the native
passthrough providers (`anthropic_messages/passthrough.py`) and the chat/responses
transports.

### Why no new `FailureKind`

Adding an enum member would have forced a wire-protocol change on every client.
`GuardBlockedError` subclasses `ExecutionFailure` with `kind=PERMISSION`,
`status_code=403`, `retryable=False`, and a message prefixed `free_guard_blocked:`.
Clients see the already-known `permission_error`; FCC, the Admin UI and tests read
the structured `reason_code`.

### UI: the existing design system, not a new one

The specification requires the new tabs to be built into the current Admin structure
and warns against a full redesign. `analytics.css` therefore contains no colour
literals of its own: every value comes from the Admin's custom properties
(`--card`, `--line`, `--text-strong`, `--muted`, `--accent`, `--ok/--warn/--error`,
`--radius-*`), and each tab is a `form-sections > section.settings-section` card with
the same `section-heading` markup `renderSections` produces, so a page switches theme
with the rest of the product. Charts are inline SVG with `currentColor` bars and grid
strokes from `--line`; there are no external assets or CDN calls.

An early draft invented its own token names (`--surface-color`, `--accent-color`) with
light-theme fallbacks, which produced white tiles with white text on the dark Admin.
The e2e suite now asserts the computed colours of a stat tile
(`rgb(21, 24, 34)` = `--card`) so that regression cannot come back silently.

**A note on the zero/unknown boundary.** SQL aggregates such as
`SUM(CASE WHEN x IS NOT NULL THEN x ELSE 0 END)` silently turn "nothing was reported"
into a measured `0`, which is exactly the invariant the specification forbids. Every
usage aggregate in `application/analytics/queries.py` therefore guards on
`COUNT(column) > 0` and yields `NULL` when no attempt reported the field, and the UI
renders that as `unknown` rather than `0`. Per-request rows additionally carry an
"n attempts without usage" marker instead of hiding the known totals of the other
attempts.

### Deliberate deviations from the spec's YAML example

1. `free_guard.mode` defaults to `off`. Strict is opt-in, because defaulting to strict
   would block every model for which no evidence exists yet — a regression for
   existing Claude Code, Codex, VS Code Chat, DSH and OpenCode users.
2. `analytics.enabled` defaults to `true`. Purely additive; set it to `false` to
   disable.
3. Spec §7's `eligibility_audit` retention is left to the shared-database migration
   rather than a separate policy table.
4. Spec §8's heatmap is P2 and not implemented; the periods `1h/24h/7d/30d`, the
   filters, the KPIs, the timeline, request history, the trace drawer, the
   provider/model/client/error breakdowns and export are all present.
5. Spec §9's `/admin/api/free-guard/jobs/{job_id}` returns the single in-flight
   refresh job; there is no job queue.

### Verification

- `pytest tests` — 8527 passed, 5 failed, 8 skipped (2026-10-11).
  The 5 failures (`tests/cli/test_codex_model_catalog.py::test_launcher_config_composes_with_persistent_codex_config`,
  four `tests/contracts/test_uv_policy.py::test_ci_python_identity_parses_toml`
  cases) are pre-existing on the base commit and unrelated.
- `ruff check src tests` and `ruff format --check` — clean.
- `ty check src` — clean.
- New: 57 tests covering policy semantics, provider-error classification, writer
  batching/idempotency/per-attempt usage, SQLite accounting, guard enforcement with
  `mock_upstream_request_count == 0`, and the Admin API contracts.

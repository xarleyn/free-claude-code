/* Analytics and Free Models tabs.

Rendering follows the Admin's own structure: pages are `form-sections` cards with a
`section-heading`, stats are `dl`/`dt`/`dd` pairs styled by analytics.css, and status
values reuse --ok/--warn/--error. Charts are inline SVG with no external assets.
*/

const analyticsState = {
  range: "24h",
  provider: "",
  model: "",
  client: "",
  status: "",
  includeProbes: false,
  traceRequest: null,
};

const ANALYTICS_RANGES = {
  "1h": { hours: 1, bucket: "minute", label: "Last hour" },
  "24h": { hours: 24, bucket: "hour", label: "Last 24 hours" },
  "7d": { hours: 24 * 7, bucket: "hour", label: "Last 7 days" },
  "30d": { hours: 24 * 30, bucket: "day", label: "Last 30 days" },
};

const CLIENT_LABELS = {
  claude_code: "Claude Code",
  codex: "Codex",
  vscode_chat: "VS Code Chat",
  dsh: "DeepSeek Harness",
  opencode: "OpenCode",
  browser: "Browser",
  other: "Other",
  unknown: "Unknown",
};

function analyticsEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function analyticsWindow() {
  const config = ANALYTICS_RANGES[analyticsState.range] || ANALYTICS_RANGES["24h"];
  const to = new Date();
  const from = new Date(to.getTime() - config.hours * 3600 * 1000);
  return { from, to, bucket: config.bucket };
}

function analyticsFilters() {
  const params = new URLSearchParams();
  const { from, to } = analyticsWindow();
  params.set("from", from.toISOString());
  params.set("to", to.toISOString());
  params.set("include_probes", String(analyticsState.includeProbes));
  if (analyticsState.provider) params.set("provider", analyticsState.provider);
  if (analyticsState.model) params.set("model", analyticsState.model);
  if (analyticsState.client) params.set("client", analyticsState.client);
  if (analyticsState.status) params.set("status", analyticsState.status);
  return params;
}

function formatTokens(value) {
  if (value === null || value === undefined) return "unknown";
  const number = Number(value);
  if (!Number.isFinite(number)) return "unknown";
  if (number >= 1e9) return `${(number / 1e9).toFixed(1)}B`;
  if (number >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
  if (number >= 1e3) return `${(number / 1e3).toFixed(1)}K`;
  return String(number);
}

function formatCost(value) {
  if (value === null || value === undefined || value === "") return "unknown";
  return `$${Number(value).toFixed(6)}`;
}

function formatMs(value) {
  if (value === null || value === undefined || value === "") return "\u2014";
  return `${Math.round(Number(value))} ms`;
}

const UTC_STAMP = /^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function formatLocal(value) {
  if (!value) return "—";
  const text = String(value);
  const match = UTC_STAMP.exec(text);
  if (!match) {
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return text;
    return parsed.toISOString().slice(0, 16).replace("T", " ");
  }
  const month = MONTHS[Number(match[1]) - 1] || match[1];
  return `${month} ${Number(match[2])}, ${match[3]}:${match[4]}`;
}

function formatShortDate(value) {
  if (!value) return "—";
  const text = String(value);
  const match = UTC_STAMP.exec(text);
  if (!match) return formatLocal(value).slice(0, 14);
  const month = MONTHS[Number(match[1]) - 1] || match[1];
  return `${month} ${Number(match[2])} ${match[3]}:${match[4]}`;
}

function clientLabel(value) {
  return CLIENT_LABELS[value] || value || "unknown";
}

function statusLabel(value) {
  return String(value || "unknown").replace(/_/g, " ");
}

function tokenCell(item, field) {
  const value = item[field];
  if (value === null || value === undefined) {
    return item.unknown_usage_attempts ? "unknown" : "—";
  }
  return formatTokens(value);
}

function unknownUsageNote(item) {
  const missing = Number(item.unknown_usage_attempts || 0);
  if (!missing) return null;
  return analyticsEl(
    "div",
    "analytics-trace-meta",
    `${missing} attempt${missing === 1 ? "" : "s"} without usage`
  );
}

function appendStat(host, label, value, state) {
  const term = analyticsEl("div", "analytics-stat");
  const dt = analyticsEl("dt", null, label);
  dt.title = label;
  const dd = analyticsEl("dd", null, value);
  if (state) dd.dataset.state = state;
  term.appendChild(dt);
  term.appendChild(dd);
  host.appendChild(term);
}

function analyticsEmpty(target, message) {
  const node = byId(target);
  if (!node) return;
  node.replaceChildren();
  node.appendChild(analyticsEl("p", "analytics-empty", message));
}

function appendNote(host, message, tight) {
  host.appendChild(
    analyticsEl("p", `analytics-note${tight ? " analytics-tight" : ""}`, message)
  );
}

/* ------------------------------------------------------------------ KPIs */

async function refreshAnalytics() {
  const view = byId("view-analytics");
  if (!view || view.hidden) return;
  const params = analyticsFilters();

  try {
    const summary = await api(`/admin/api/analytics/summary?${params}`);
    renderAnalyticsKpis(summary);
  } catch (error) {
    analyticsEmpty("analyticsKpis", `Could not load analytics: ${error.message}`);
  }
  try {
    const series = await api(
      `/admin/api/analytics/timeseries?${params}&bucket=${analyticsWindow().bucket}`
    );
    renderAnalyticsChart(series);
  } catch (error) {
    analyticsEmpty("analyticsChart", "Chart unavailable for this range.");
  }
  try {
    const rows = await api(`/admin/api/analytics/requests?${params}&limit=50`);
    renderAnalyticsRequests(rows);
  } catch (error) {
    analyticsEmpty("analyticsRequests", "Request history unavailable.");
  }
}

function renderAnalyticsKpis(summary) {
  const host = byId("analyticsKpis");
  if (!host) return;
  host.replaceChildren();

  const usage = summary.usage || {};
  const cost = summary.estimated_cost || {};
  const latency = summary.latency || {};
  const ttft = summary.ttft || {};
  const unknownTokens =
    usage.unknown_token_share === null || usage.unknown_token_share === undefined
      ? "unknown"
      : `${Math.round(usage.unknown_token_share * 100)}%`;
  const costState = cost.known_usd === null || cost.known_usd === undefined ? "unknown" : null;

  appendStat(host, "Logical requests", summary.logical_requests);
  appendStat(host, "Success", summary.success_count);
  appendStat(host, "Errors", summary.error_count);
  appendStat(host, "Guard-blocked", summary.blocked_count);
  appendStat(host, "Downstream attempts", summary.downstream_attempts);
  appendStat(host, "Fallbacks", summary.fallback_logical_requests);
  appendStat(host, "Input tokens (known)", formatTokens(usage.input_tokens_known));
  appendStat(host, "Output tokens (known)", formatTokens(usage.output_tokens_known));
  appendStat(host, "Cached read tokens", formatTokens(usage.cache_read_tokens_known));
  appendStat(
    host,
    "Estimated cost (known)",
    formatCost(cost.known_usd),
    costState
  );
  appendStat(
    host,
    "Cost confidence",
    cost.confidence,
    costState === "unknown" ? "muted" : null
  );
  appendStat(host, "Unknown token share", unknownTokens, unknownTokens === "unknown" ? "muted" : null);
  appendStat(host, "P50 latency", formatMs(latency.p50_ms));
  appendStat(host, "P95 latency", formatMs(latency.p95_ms));
  appendStat(host, "P50 TTFT", formatMs(ttft.p50_ms));
  appendStat(host, "P95 TTFT", formatMs(ttft.p95_ms));
}

/* ------------------------------------------------------------------ chart */

function renderAnalyticsChart(series) {
  const host = byId("analyticsChart");
  if (!host) return;
  host.replaceChildren();
  const buckets = (series.series || []).slice(-48);
  if (!buckets.length) {
    host.appendChild(analyticsEl("p", "analytics-empty", "No requests in range."));
    return;
  }
  const width = 760;
  const height = 196;
  const max = Math.max(
    1,
    ...buckets.map((bucket) =>
      Number(
        (bucket.success || 0) + (bucket.error || 0) + (bucket.blocked || 0)
      )
    )
  );
  const step = width / Math.max(1, buckets.length);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "analytics-chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Requests over time, stacked by outcome");

  for (let line = 0; line <= 4; line += 1) {
    const y = height - 22 - (line / 4) * (height - 40);
    const grid = document.createElementNS("http://www.w3.org/2000/svg", "line");
    grid.setAttribute("x1", "0");
    grid.setAttribute("x2", String(width));
    grid.setAttribute("y1", String(y));
    grid.setAttribute("y2", String(y));
    grid.setAttribute("class", "analytics-grid");
    svg.appendChild(grid);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", "6");
    label.setAttribute("y", String(Math.max(12, y - 5)));
    label.setAttribute("class", "analytics-axis");
    label.textContent = String(Math.round((line / 4) * max));
    svg.appendChild(label);
  }

  buckets.forEach((bucket, index) => {
    const total =
      Number(bucket.success || 0) + Number(bucket.error || 0) + Number(bucket.blocked || 0);
    const barHeight = (total / max) * (height - 30);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", String(index * step + 1.5));
    rect.setAttribute("y", String(height - 20 - barHeight));
    rect.setAttribute("width", String(Math.max(2, step - 3)));
    rect.setAttribute("height", String(barHeight));
    rect.setAttribute("class", "analytics-bar");
    if (Number(bucket.blocked || 0) > Number(bucket.success || 0)) {
      rect.dataset.status = "blocked";
    } else if (Number(bucket.error || 0) > 0 && total === Number(bucket.error || 0)) {
      rect.dataset.status = "error";
    }
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent =
      `${formatShortDate(bucket.bucket)}: ${total} requests ` +
      `(${bucket.success || 0} ok, ${bucket.error || 0} error, ${bucket.blocked || 0} blocked)`;
    rect.appendChild(title);
    svg.appendChild(rect);
  });
  host.appendChild(svg);

  const wrap = analyticsEl("div", "analytics-table-wrap");
  const table = analyticsEl("table", "analytics-table");
  const head = analyticsEl("thead");
  const headRow = analyticsEl("tr");
  [
    ["Time", false],
    ["Requests", true],
    ["Success", true],
    ["Errors", true],
    ["Blocked", true],
    ["Input tokens", true],
    ["Output tokens", true],
  ].forEach(([label, numeric]) => {
    const th = analyticsEl("th", numeric ? "analytics-num" : null, label);
    th.scope = "col";
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = analyticsEl("tbody");
  buckets
    .slice()
    .reverse()
    .forEach((bucket) => {
      const row = analyticsEl("tr");
      const time = analyticsEl("td", null, formatShortDate(bucket.bucket));
      row.appendChild(time);
      [
        (bucket.success || 0) + (bucket.error || 0) + (bucket.blocked || 0),
        bucket.success || 0,
        bucket.error || 0,
        bucket.blocked || 0,
        formatTokens(bucket.input_tokens),
        formatTokens(bucket.output_tokens),
      ].forEach((value) => {
        row.appendChild(analyticsEl("td", "analytics-num", value));
      });
      body.appendChild(row);
    });
  table.appendChild(body);
  wrap.appendChild(table);
  host.appendChild(wrap);
}

/* --------------------------------------------------------- request history */

function renderAnalyticsRequests(payload) {
  const host = byId("analyticsRequests");
  if (!host) return;
  host.replaceChildren();
  const items = payload.items || [];
  if (!items.length) {
    host.appendChild(
      analyticsEl("p", "analytics-empty", "No recorded requests in this range.")
    );
    return;
  }
  const wrap = analyticsEl("div", "analytics-table-wrap");
  const table = analyticsEl("table", "analytics-table");
  const columns = [
    ["Time", false],
    ["Client", false],
    ["Requested model", false],
    ["Final provider / model", false],
    ["Status", false],
    ["In", true],
    ["Out", true],
    ["Cache", true],
    ["Attempts", true],
    ["Latency", true],
    ["Cost", false],
    [null, false],
  ];
  const head = analyticsEl("thead");
  const headRow = analyticsEl("tr");
  columns.forEach(([label, numeric]) => {
    if (label === null) {
      headRow.appendChild(analyticsEl("th", null, "Trace"));
    } else {
      const th = analyticsEl("th", numeric ? "analytics-num" : null, label);
      th.scope = "col";
      headRow.appendChild(th);
    }
  });
  head.appendChild(headRow);
  table.appendChild(head);

  const body = analyticsEl("tbody");
  items.forEach((item) => {
    const row = analyticsEl("tr");
    row.appendChild(analyticsEl("td", null, formatLocal(item.started_at_utc)));
    row.appendChild(analyticsEl("td", null, clientLabel(item.client_type)));
    row.appendChild(analyticsEl("td", null, item.requested_model || "\u2014"));
    row.appendChild(
      analyticsEl(
        "td",
        null,
        `${item.final_provider_id || "\u2014"}/${item.final_upstream_model_id || ""}`
      )
    );
    const status = analyticsEl("td");
    const badge = analyticsEl(
      "span",
      "analytics-status",
      statusLabel(item.status)
    );
    badge.dataset.status = item.status || "unknown";
    status.appendChild(badge);
    if (item.normalized_error_code) {
      status.appendChild(
        analyticsEl("div", "analytics-trace-meta", item.normalized_error_code)
      );
    }
    const missing = unknownUsageNote(item);
    if (missing) status.appendChild(missing);
    row.appendChild(status);
    // Known totals stay visible; an attempt without usage is flagged separately so a
    // gap is never mistaken for a measured zero.
    row.appendChild(
      analyticsEl("td", "analytics-num", tokenCell(item, "input_tokens"))
    );
    row.appendChild(
      analyticsEl("td", "analytics-num", tokenCell(item, "output_tokens"))
    );
    row.appendChild(
      analyticsEl("td", "analytics-num", tokenCell(item, "cache_read_tokens"))
    );
    row.appendChild(analyticsEl("td", "analytics-num", item.upstream_attempts));
    row.appendChild(analyticsEl("td", "analytics-num", formatMs(item.duration_ms)));
    row.appendChild(
      analyticsEl("td", "analytics-word", formatCost(item.estimated_cost_usd))
    );
    const action = analyticsEl("td");
    const button = analyticsEl("button", "analytics-link", "View");
    button.type = "button";
    button.addEventListener("click", () => void showRequestTrace(item.request_id));
    action.appendChild(button);
    row.appendChild(action);
    body.appendChild(row);
  });
  table.appendChild(body);
  wrap.appendChild(table);
  host.appendChild(wrap);
  appendNote(host, "Metadata only. Prompts and responses are never stored.", true);
}

async function showRequestTrace(requestId) {
  const host = byId("analyticsTrace");
  if (!host) return;
  host.replaceChildren();
  analyticsState.traceRequest = requestId;
  try {
    const trace = await api(`/admin/api/analytics/requests/${requestId}`);
    const request = trace.request || {};
    host.appendChild(
      analyticsEl("h4", null, `Trace for ${request.requested_model || requestId}`)
    );
    host.appendChild(
      analyticsEl(
        "p",
        "analytics-note analytics-tight",
        `${clientLabel(request.client_type)} · started ${formatLocal(
          request.started_at_utc
        )} · ${formatMs(request.duration_ms)} · final ${
          request.final_provider_id || "\u2014"
        }/${request.final_upstream_model_id || ""}`
      )
    );
    const list = analyticsEl("ol", "analytics-trace");
    (trace.attempts || []).forEach((attempt) => {
      const item = analyticsEl("li", "analytics-trace-step");
      item.dataset.status = attempt.status || "unknown";
      item.appendChild(
        analyticsEl(
          "div",
          "analytics-trace-target",
          `${attempt.attempt_number}. ${attempt.provider_id}/${attempt.upstream_model_id}`
        )
      );
      const meta = analyticsEl("div", "analytics-trace-meta");
      meta.textContent =
        `${statusLabel(attempt.status)}` +
        `${attempt.upstream_http_status ? ` · HTTP ${attempt.upstream_http_status}` : ""}` +
        `${attempt.normalized_error_code ? ` · ${attempt.normalized_error_code}` : ""}` +
        `${attempt.fallback_from_attempt_id ? ` · fallback from attempt ${attempt.fallback_from_attempt_id}` : ""}`;
      item.appendChild(meta);
      const usage = attempt.usage || {};
      const source =
        usage.source === "provider_reported"
          ? "provider reported"
          : usage.source === "local_estimate"
            ? "local estimate"
            : "source unknown";
      item.appendChild(
        analyticsEl(
          "div",
          "analytics-trace-meta",
          `in ${formatTokens(usage.input_tokens)} · out ${formatTokens(
            usage.output_tokens
          )} · cache read ${formatTokens(usage.cache_read_tokens)} · ${source}` +
            ` · ttft ${formatMs(attempt.ttft_ms)} · cost ${
              attempt.estimated_cost_usd
                ? formatCost(attempt.estimated_cost_usd)
                : "unknown"
            }`
        )
      );
      list.appendChild(item);
    });
    host.appendChild(list);
    if (!trace.attempts || !trace.attempts.length) {
      host.appendChild(
        analyticsEl("p", "analytics-empty", "This request has no recorded attempts.")
      );
    }
  } catch (error) {
    analyticsEmpty("analyticsTrace", `Trace unavailable: ${error.message}`);
  }
}

/* --------------------------------------------------------------- controls */

function bindAnalyticsControls() {
  const bind = (id, event, handler) => {
    const node = byId(id);
    if (!node || node.dataset.bound) return node;
    node.dataset.bound = "1";
    node.addEventListener(event, handler);
    return node;
  };

  bind("analyticsRange", "change", (event) => {
    analyticsState.range = event.target.value;
    void refreshAnalytics();
  });
  bind("analyticsIncludeProbes", "change", (event) => {
    analyticsState.includeProbes = event.target.checked;
    void refreshAnalytics();
  });
  bind("analyticsRefresh", "click", () => void refreshAnalytics());
  bind("analyticsProvider", "change", (event) => {
    analyticsState.provider = event.target.value.trim();
    void refreshAnalytics();
  });
  bind("analyticsModel", "change", (event) => {
    analyticsState.model = event.target.value.trim();
    void refreshAnalytics();
  });
  bind("analyticsClient", "change", (event) => {
    analyticsState.client = event.target.value;
    void refreshAnalytics();
  });
  bind("analyticsStatus", "change", (event) => {
    analyticsState.status = event.target.value;
    void refreshAnalytics();
  });
  bind("analyticsExport", "click", () => {
    const params = analyticsFilters();
    const format = byId("analyticsFormat")?.value || "json";
    window.open(
      `/admin/api/analytics/export?format=${format}&${params}`,
      "_blank",
      "noopener"
    );
  });

  const auto = bind("analyticsAuto", "change", null);
  if (auto) auto.remove();
  startAnalyticsAutoRefresh();
  if (analyticsState.traceRequest) {
    void showRequestTrace(analyticsState.traceRequest);
  }
}

let analyticsAutoTimer = null;

function startAnalyticsAutoRefresh() {
  if (analyticsAutoTimer) return;
  analyticsAutoTimer = window.setInterval(() => {
    const view = byId("view-analytics");
    if (view && !view.hidden && !document.hidden) void refreshAnalytics();
  }, 15000);
}

/* ------------------------------------------------------------- FreeGuard */

async function refreshFreeGuard() {
  const view = byId("view-free-models");
  if (!view || view.hidden) return;
  try {
    const summary = await api("/admin/api/free-guard/summary");
    renderFreeGuardSummary(summary);
  } catch (error) {
    analyticsEmpty("freeGuardSummary", `FreeGuard unavailable: ${error.message}`);
    return;
  }
  const params = new URLSearchParams({ include_blocked: "true" });
  const provider = byId("freeGuardProvider")?.value.trim();
  const query = byId("freeGuardSearch")?.value.trim();
  if (provider) params.set("provider", provider);
  if (query) params.set("q", query);
  try {
    const payload = await api(`/admin/api/free-guard/models?${params}`);
    renderFreeGuardModels(payload);
  } catch (error) {
    analyticsEmpty("freeGuardModels", "Eligibility list unavailable.");
  }
}

function renderFreeGuardSummary(summary) {
  const host = byId("freeGuardSummary");
  if (!host) return;
  host.replaceChildren();
  const counts = summary.counts || {};
  const last = summary.last_refresh || {};
  appendStat(
    host,
    "Mode",
    summary.strict ? `${summary.mode} (strict)` : summary.mode,
    summary.strict ? null : "muted"
  );
  appendStat(host, "Verified free", counts.verified_free);
  appendStat(host, "Temporarily unavailable", counts.temporarily_unavailable);
  appendStat(host, "Blocked (paid/trial)", counts.blocked_paid);
  appendStat(host, "Unknown", counts.unknown, counts.unknown ? "muted" : null);
  appendStat(
    host,
    "Last refresh",
    last.finished_at_utc ? formatLocal(last.finished_at_utc) : "never",
    last.finished_at_utc ? null : "muted"
  );
}

function renderFreeGuardModels(payload) {
  const host = byId("freeGuardModels");
  if (!host) return;
  host.replaceChildren();
  const items = payload.items || [];
  if (!items.length) {
    host.appendChild(
      analyticsEl("p", "analytics-empty", "No eligibility evidence recorded yet.")
    );
    return;
  }
  const wrap = analyticsEl("div", "analytics-table-wrap");
  const table = analyticsEl("table", "analytics-table");
  const columns = [
    "Provider",
    "Model",
    "Billing class",
    "Access",
    "Status",
    "Reason",
    "Last verified",
    "Actions",
  ];
  const head = analyticsEl("thead");
  const headRow = analyticsEl("tr");
  columns.forEach((label) => {
    const th = analyticsEl("th", null, label);
    th.scope = "col";
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  table.appendChild(head);

  const body = analyticsEl("tbody");
  items.forEach((item) => {
    const row = analyticsEl("tr");
    row.appendChild(analyticsEl("td", null, item.provider_id));
    row.appendChild(analyticsEl("td", null, item.upstream_model_id));
    row.appendChild(analyticsEl("td", null, item.billing_class));
    row.appendChild(analyticsEl("td", null, item.access_status));
    const status = analyticsEl("td");
    const badge = analyticsEl(
      "span",
      "analytics-status",
      item.eligible ? "eligible" : "blocked"
    );
    badge.dataset.status = item.eligible ? "success" : "error";
    status.appendChild(badge);
    row.appendChild(status);
    row.appendChild(
      analyticsEl(
        "td",
        null,
        `${item.reason_label || item.reason_code}${
          item.manually_approved ? " · attested" : ""
        }`
      )
    );
    row.appendChild(
      analyticsEl(
        "td",
        null,
        item.evidence_checked_at_utc ? formatLocal(item.evidence_checked_at_utc) : "never"
      )
    );
    const action = analyticsEl("td");
    const button = analyticsEl(
      "button",
      "analytics-link",
      item.eligible ? "Revoke" : "Attest free"
    );
    button.type = "button";
    button.addEventListener("click", () =>
      item.eligible ? void revokeFree(item) : void attestFree(item)
    );
    action.appendChild(button);
    row.appendChild(action);
    body.appendChild(row);
  });
  table.appendChild(body);
  wrap.appendChild(table);
  host.appendChild(wrap);
  appendNote(
    host,
    "Billing class comes from provider documentation, machine-readable prices and this " +
      "account's own failures. A price of zero is treated as unknown, not as free.",
    true
  );
}

async function attestFree(item) {
  const url = window.prompt(
    `Public URL proving ${item.provider_id}/${item.upstream_model_id} needs no card, ` +
      "top-up or subscription:"
  );
  if (!url) return;
  const note = window.prompt("Optional note:", "") || "";
  try {
    await api("/admin/api/free-guard/attest", {
      method: "POST",
      body: JSON.stringify({
        provider_id: item.provider_id,
        model_id: item.upstream_model_id,
        source_url: url,
        note,
        attested_by: "admin",
      }),
    });
    showMessage(`Attested ${item.upstream_model_id} as free.`);
    void refreshFreeGuard();
  } catch (error) {
    showMessage(`Attestation failed: ${error.message}`, "error");
  }
}

async function revokeFree(item) {
  try {
    await api("/admin/api/free-guard/revoke", {
      method: "POST",
      body: JSON.stringify({
        provider_id: item.provider_id,
        model_id: item.upstream_model_id,
      }),
    });
    showMessage(`Revoked attestation for ${item.upstream_model_id}.`);
    void refreshFreeGuard();
  } catch (error) {
    showMessage(`Revoke failed: ${error.message}`, "error");
  }
}

async function refreshAllFreeGuard() {
  try {
    await api("/admin/api/free-guard/refresh", {
      method: "POST",
      body: JSON.stringify({ provider_ids: [] }),
    });
    showMessage("Eligibility refresh started.");
  } catch (error) {
    showMessage(`Refresh failed: ${error.message}`, "error");
  }
  void refreshFreeGuard();
}

function bindFreeGuardControls() {
  const bind = (id, event, handler) => {
    const node = byId(id);
    if (!node || node.dataset.bound) return;
    node.dataset.bound = "1";
    node.addEventListener(event, handler);
  };
  bind("freeGuardSearch", "change", () => void refreshFreeGuard());
  bind("freeGuardProvider", "change", () => void refreshFreeGuard());
  bind("freeGuardRefresh", "click", () => void refreshAllFreeGuard());
}

window.Analytics = {
  activate() {
    bindAnalyticsControls();
    void refreshAnalytics();
  },
  activateFreeGuard() {
    bindFreeGuardControls();
    void refreshFreeGuard();
  },
};

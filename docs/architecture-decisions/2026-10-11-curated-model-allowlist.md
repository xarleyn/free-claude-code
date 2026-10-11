# Curated model allowlist — catalog audit and verified shortlist

Snapshot date: 2026-10-11. Server under test: `http://127.0.0.1:8082` (`uv run fcc-server` from this repo).
Everything below was measured against that running server; nothing is carried over from a previous report.

## 1. What was measured

| Step | Input | Method |
| --- | --- | --- |
| Catalog dump | `GET /v1/models` → 2985 ids | parsed into `provider/model` references |
| Quality signals | OpenRouter `GET /api/v1/models` (458 rows, incl. `benchmarks`), OpenRouter `GET /api/v1/models?category=programming` (19 rows, weekly usage order), `models.dev/api.json` (226 providers) | joined on the normalized model name |
| Liveness | `POST /v1/messages` with `Reply with exactly: PONG-7391`, `max_tokens=512` | 200 + exact echo = live; anything else = failed, with the upstream status recorded |

Probe totals: 42 provider-health probes, 22 confirmation probes on suspected-dead providers,
141 canonical model probes (134 live, median 8.8 s), 26 `:free` variant probes (9 live), and
115 OpenCode probes re-run with a session header (23 live on `opencode_go`, 0 on `opencode_zen`).

Scoring used: Artificial Analysis `intelligence_index`, Design Arena coding ELO, the OpenRouter
programming-category rank, tool/structured-output support, context size, release date, free-tier flag.
The absolute score is only a sort key; the underlying numbers are printed per model in §5.

## 2. Why the list is 2985 entries long

The catalog holds **1539 distinct `provider/model` references**, not 2985:

- 1438 references are advertised twice — once as `anthropic/<provider>/<model>` and once as
  `claude-3-freecc-no-thinking/<provider>/<model>`. The second form forces reasoning off.
- 101 references are advertised only under the no-thinking prefix.
- 8 ids are the built-in Claude aliases (`claude-fable-5`, `claude-opus-4-20250514`, …).

After collapsing provider prefixes, the 1539 references describe **584 distinct models**. Of those,
173 report no tool support, 158 have a context below 64k, and 90 are unknown to both OpenRouter and
models.dev. This is most of the "trash" volume: superseded versions, tiny open-weight models,
`:batch` entries, and models no provider will actually serve.

`:batch` is broken rather than merely useless: `open_router/anthropic/claude-opus-5.5:batch` answers
HTTP 400/404 with "`…:batch cannot be used with the chat/completions endpoint`". Every `:batch` id in
the catalog is advertised and unroutable through `/v1/messages`.

## 3. Provider reachability

Four of the fourteen configured providers answer, and one of them only for a caller that
carries a session (§9).

| Provider | Result | Detail |
| --- | --- | --- |
| `open_router` | live 3/3 + 128/141 model probes | the general-purpose lane; paid models bill against the key |
| `opencode_go` | live 23/31, **session required** | subscription tier: without `x-opencode-session` (or a caller session) every call is HTTP 400 `MissingSessionID`; with one, 23 models answer in 0.8–5.7 s. The 8 refusals are two account settings, not billing: 4 × `This Go model requires Global regions. Select Global in your workspace's Privacy settings`, 2 × `ModelProtocolUnsupported` (`claude-haiku-5-5`, `minimax-m2.7`), 2 × the same Global-regions refusal on `muse-spark-*-contributor` |
| `zai` | live 3/3 | GLM family, 6 of the shortlisted models |
| `groq` | live 1/1 | small model set |
| `xkiro` | partial, flaky | HTTP 403 on paid models; 6/15 `qwen/*:free` probes answered, 8 returned HTTP 503 after ~35 s |
| `opencode_zen` | dead | HTTP 403 `Model access is disabled` on 84/84 probes even with a session — an account-side switch, not a per-model limit |
| `cheaperinference` | dead | HTTP 402 `Insufficient wallet balance` |
| `huggingface` | dead | HTTP 402 `permission` |
| `ollama_cloud` | dead | HTTP 402 billing |
| `orcarouter` | dead | HTTP 402 `insufficient_quota` |
| `tokenrouter` | dead | HTTP 403 credential permissions |
| `experiential` | dead | HTTP 429 `org_under_review` — the account is flagged, not rate-limited |
| `zai_api` | dead | HTTP 429 code `1113` |
| `custom_52b1aa…` | dead | HTTP 402 `insufficient_balance` |

The first `opencode_go` verdict in this document's earlier draft was "dead". That was an
artifact of the probe: the request carried no session, so the refusal was about the request,
not the account. A provider whose gate is a caller header has to be probed as its real clients
call it.

**The default model is dead.** `~/.fcc/.env` sets `MODEL=opencode_zen/exo-free`; `opencode_zen`
returns 403 `Model access is disabled`, and the model itself is reported as `ModelDeprecated`
(HTTP 410) when it is reached through the fallback path. Any request that does not name a
catalog model explicitly ends up in that dead lane.

## 4. How to switch the catalog off the trash

The patch adds one setting; it filters the client catalog only and never blocks a direct call
(that is FreeGuard's job).

- Setting: `FCC_MODEL_ALLOWLIST` — comma-separated globs over `provider/model` references.
  Admin → Models → *Model Allowlist* (textarea), or the env var in `~/.fcc/.env`.
- It is applied in `application/model_catalog.apply_model_allowlist`, so both surfaces that publish
  the inventory are trimmed: the client model lists (`api/model_catalog.filter_catalog`, which backs
  `/v1/models` for every view) and the Codex snapshot written to `~/.fcc/codex-model-catalog.json`
  (`runtime/codex_catalog.CodexModelCatalogPublisher`). CLI launcher configs pick the trimmed list up
  over HTTP.
- A pattern containing `/` is matched against the whole reference; a bare pattern is matched against
  the model part and its last path segment, so `glm-5*` covers `zai/glm-5.3` and
  `open_router/z-ai/glm-5.3-flash` at once.
- References named in `MODEL`, `MODEL_FABLE`, `MODEL_OPUS`, `MODEL_SONNET`, `MODEL_HAIKU` and
  `MODEL_FALLBACKS` always stay visible, so a mistyped pattern cannot hide the model in use.
- Empty value means no filtering — the current behaviour is unchanged by default.

Apply the value below, then restart the server (the running process has the old code):

```
FCC_MODEL_ALLOWLIST=groq/qwen/qwen3.8-27b,open_router/anthropic/claude-fable-5,open_router/anthropic/claude-fable-5.1,open_router/anthropic/claude-haiku-5.5,open_router/anthropic/claude-opus-4.5,open_router/anthropic/claude-opus-4.6,open_router/anthropic/claude-opus-4.7,open_router/anthropic/claude-opus-4.8,open_router/anthropic/claude-opus-5,open_router/anthropic/claude-opus-5.5,open_router/anthropic/claude-sonnet-4.6,open_router/anthropic/claude-sonnet-5,open_router/anthropic/claude-sonnet-5.5,open_router/deepseek/deepseek-v4-flash,open_router/deepseek/deepseek-v4-flash-0731,open_router/deepseek/deepseek-v4-pro,open_router/deepseek/deepseek-v4-pro-0813,open_router/deepseek/deepseek-v4.1-flash,open_router/google/gemini-3.1-pro-preview,open_router/google/gemini-3.5-flash,open_router/google/gemini-3.6-flash,open_router/google/gemini-3.7-flash,open_router/google/gemini-3.8-flash,open_router/inclusionai/ling-3.1-flash,open_router/minimax/minimax-m3,open_router/moonshotai/kimi-k2.5,open_router/moonshotai/kimi-k2.6,open_router/moonshotai/kimi-k2.7-code,open_router/moonshotai/kimi-k3,open_router/nvidia/nemotron-3-ultra-550b-a55b,open_router/nvidia/nemotron-3-ultra-550b-a55b:free,open_router/openai/gpt-5.2,open_router/openai/gpt-5.2-codex,open_router/openai/gpt-5.3-codex,open_router/openai/gpt-5.4,open_router/openai/gpt-5.5,open_router/openai/gpt-5.6-luna,open_router/openai/gpt-5.6-sol,open_router/openai/gpt-5.6-terra,open_router/openai/gpt-6-astra,open_router/openai/gpt-6-luna,open_router/openai/gpt-6-sol,open_router/openai/gpt-6.1-sol,open_router/qwen/qwen3.6-plus,open_router/qwen/qwen3.7-max,open_router/qwen/qwen3.7-plus,open_router/qwen/qwen3.8-2.4t-a95b,open_router/qwen/qwen3.8-27b,open_router/qwen/qwen3.8-max-0902,open_router/thinkingmachines/inkling,open_router/upstage/solar-mini4,open_router/upstage/solar-pro4,open_router/x-ai/grok-4.20,open_router/x-ai/grok-4.3,open_router/x-ai/grok-4.5,open_router/x-ai/grok-4.6,open_router/x-ai/grok-4.7,open_router/xiaomi/mimo-v2.5,open_router/xiaomi/mimo-v2.5-pro,open_router/xiaomi/mimo-v2.6-flash,open_router/xiaomi/mimo-v2.6-pro,open_router/z-ai/glm-5,open_router/z-ai/glm-5-turbo,open_router/z-ai/glm-5.1,open_router/z-ai/glm-5.2,open_router/z-ai/glm-5.3,open_router/z-ai/glm-5.3-flash,open_router/z-ai/glm-5v-turbo,opencode_go/glm-5.2,opencode_go/glm-5.3,opencode_go/glm-5.3-flash,opencode_go/gpt-5.6-luna,opencode_go/gpt-6-luna,opencode_go/grok-4.6,opencode_go/kimi-k2.7-code,opencode_go/kimi-k3,opencode_go/mimo-v2.5,opencode_go/mimo-v2.5-pro,opencode_go/mimo-v2.6-flash,opencode_go/mimo-v2.6-pro,opencode_go/minimax-m3,opencode_go/qwen3.7-plus,xkiro/qwen/qwen3.6-plus:free,xkiro/qwen/qwen3.7-max:free,xkiro/qwen/qwen3.7-plus:free,zai/glm-5,zai/glm-5-turbo,zai/glm-5.1,zai/glm-5.2,zai/glm-5.3,zai/glm-5.3-flash
```

91 patterns keep exactly 91 references — 5.9% of the catalog, and 66 distinct models. The
`opencode_go/*` entries are the subscription lane (§5.1) and only answer for a caller that
carries a session (§9). Glob-compressed variants were generated and rejected: any prefix wide
enough to shorten the list (`open_router/anthropic/claude-*`) also pulls in `:batch` and
superseded entries, so the exact list is the shortest correct one.

Also worth setting while the config is open, because they currently point at the dead default:

```
MODEL=open_router/z-ai/glm-5.3-flash
MODEL_OPUS=open_router/anthropic/claude-opus-5.5
MODEL_SONNET=open_router/z-ai/glm-5.3
MODEL_HAIKU=open_router/anthropic/claude-haiku-5.5
MODEL_FALLBACKS=open_router/z-ai/glm-5.3-flash,open_router/deepseek/deepseek-v4.1-flash,open_router/xiaomi/mimo-v2.6-flash
```

These are suggestions, not measurements: the tier names map to Claude Code's own aliases, so the
cheap/fast lanes above were picked for the price column in §5, and the top lanes for the benchmark
column. `MODEL_FALLBACKS` matters more than usual here, because every non-dead model sits behind a
single OpenRouter key and one rate limit.

## 5. Verified shortlist

Tier S = score ≥ 90, tier A = 70–89. Every row answered the liveness probe with at least one
reference. `AA` is the Artificial Analysis intelligence index, `Code ELO` the best Design Arena coding
score, `Prog` the OpenRouter weekly programming-category rank (blank = outside the top 19). Prices are
OpenRouter per-million-tokens, in USD.

### Tier S

| Model | Reference (OpenRouter unless noted) | AA | Code ELO | Prog | Context | Price in/out | Providers |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Opus 5.5 | `anthropic/claude-opus-5.5` | 57.6 | 1466 | 10 | 1000k | 4.0/20.0 | open_router |
| Claude Sonnet 5.5 | `anthropic/claude-sonnet-5.5` | 56.0 | 1440 | — | 1000k | 2.0/10.0 | open_router |
| GPT-6 Astra | `openai/gpt-6-astra` | 52.7 | — | — | 1050k | 10.0/50.0 | open_router |
| GPT-6.1 Sol | `openai/gpt-6.1-sol` | 51.8 | 1366 | 14 | 1050k | 2.0/10.0 | open_router |
| Claude Opus 5 | `anthropic/claude-opus-5` | 50.8 | 1344 | — | 1000k | 5.0/25.0 | open_router |
| Claude Fable 5.1 | `anthropic/claude-fable-5.1` | 53.4 | 1387 | — | 1000k | 10.0/50.0 | open_router |
| Claude Fable 5 | `anthropic/claude-fable-5` | 49.6 | 1343 | — | 1000k | 10.0/50.0 | open_router |
| MiMo-V2.6-Pro | `xiaomi/mimo-v2.6-pro` | 46.3 | 1321 | 17 | 1050k | 0.43/0.87 | open_router |
| Grok 4.7 | `x-ai/grok-4.7` | 46.4 | 1311 | — | 500k | 2.0/6.0 | open_router |
| Grok 4.6 | `x-ai/grok-4.6` | 44.3 | 1300 | — | 500k | 2.0/6.0 | open_router |
| GLM 5.3 | `z-ai/glm-5.3` | 44.8 | 1320 | 7 | 1049k | 0.04/4.8 | open_router, zai |
| Kimi K3 | `moonshotai/kimi-k3` | 43.6 | 1369 | 13 | 1049k | 0.8/10.0 | open_router |
| Claude Haiku 5.5 | `anthropic/claude-haiku-5.5` | 43.4 | 1312 | — | 1000k | 0.1/0.5 | open_router |
| Claude Opus 4.8 | `anthropic/claude-opus-4.8` | 41.8 | 1310 | — | 1000k | 5.0/25.0 | open_router |
| GLM 5.3 Flash | `z-ai/glm-5.3-flash` | 41.8 | 1288 | 3 | 1049k | 0.15/0.5 | open_router, zai |
| Gemini 3.8 Flash | `google/gemini-3.8-flash` | 40.9 | 1313 | 18 | 1049k | 0.75/3.75 | open_router |
| GPT-5.4 | `openai/gpt-5.4` | 39.0 | 1246 | — | 1050k | 2.5/15.0 | open_router |
| Gemini 3.7 Flash | `google/gemini-3.7-flash` | 39.6 | 1327 | — | 1049k | 0.75/3.75 | open_router |
| GPT-5.5 | `openai/gpt-5.5` | 38.4 | 1300 | — | 1050k | 5.0/30.0 | open_router |
| GPT-6 Luna | `openai/gpt-6-luna` | 38.1 | — | 5 | 1050k | 0.1/0.5 | open_router |
| Claude Sonnet 5 | `anthropic/claude-sonnet-5` | 38.2 | 1288 | — | 1000k | 2.0/10.0 | open_router |
| Grok 4.5 | `x-ai/grok-4.5` | 38.8 | 1286 | — | 500k | 2.0/6.0 | open_router |
| MiMo-V2.6-Flash | `xiaomi/mimo-v2.6-flash` | 37.9 | — | 2 | 1050k | 0.14/0.28 | open_router |
| DeepSeek V4.1 Flash | `deepseek/deepseek-v4.1-flash` | 39.5 | — | 1 | 1049k | 0.3/1.2 | open_router |
| DeepSeek V4 Flash 0731 | `deepseek/deepseek-v4-flash-0731` | 34.3 | 1242 | 12 | 1049k | 0.01/0.64 | open_router |
| GLM 5.2 | `z-ai/glm-5.2` | 33.7 | 1293 | 8 | 1049k | 0.06/7.0 | open_router, zai |

### Tier A

| Model | Reference (OpenRouter unless noted) | AA | Code ELO | Prog | Context | Price in/out | Providers |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GPT-6 Sol | `openai/gpt-6-sol` | 47.6 | — | — | 1050k | 2.0/10.0 | open_router |
| GPT-5.6 Sol | `openai/gpt-5.6-sol` | 47.0 | — | — | 1050k | 2.0/10.0 | open_router |
| Qwen3.8 Max (0902) | `qwen/qwen3.8-max-0902` | 45.4 | — | — | 1000k | 2.0/6.0 | open_router |
| GPT-5.6 Terra | `openai/gpt-5.6-terra` | 42.1 | — | — | 1050k | 2.0/12.0 | open_router |
| Ling 3.1 Flash | `inclusionai/ling-3.1-flash` | 41.1 | — | — | 262k | free | open_router |
| Claude Opus 4.7 | `anthropic/claude-opus-4.7` | 40.7 | — | — | 1000k | 5.0/25.0 | open_router |
| Qwen3.8 2.4T A95B | `qwen/qwen3.8-2.4t-a95b` | 39.9 | — | — | 1049k | 2.0/6.0 | open_router |
| GPT-5.6 Luna | `openai/gpt-5.6-luna` | 37.3 | — | — | 1050k | 0.2/1.2 | open_router |
| DeepSeek V4 Pro 0813 | `deepseek/deepseek-v4-pro-0813` | 36.0 | — | — | 1049k | 0.66/1.98 | open_router |
| Gemini 3.6 Flash | `google/gemini-3.6-flash` | 34.0 | 1304 | — | 1049k | 0.75/3.75 | open_router |
| Qwen3.8 27B | `qwen/qwen3.8-27b` | 33.7 | — | — | 1000k | 0.04/2.3 | groq, open_router |
| GPT-5.3-Codex | `openai/gpt-5.3-codex` | 32.5 | 1167 | — | 400k | 1.75/14.0 | open_router |
| Claude Opus 4.6 | `anthropic/claude-opus-4.6` | 31.9 | 1295 | — | 1000k | 5.0/25.0 | open_router |
| GPT-5.2 | `openai/gpt-5.2` | 30.4 | 1202 | — | 400k | 1.75/14.0 | open_router |
| DeepSeek V4 Pro 0423 | `deepseek/deepseek-v4-pro` | 30.4 | 1244 | — | 1049k | 0.21/0.42 | open_router |
| Claude Sonnet 4.6 | `anthropic/claude-sonnet-4.6` | 30.1 | 1289 | — | 1000k | 3.0/15.0 | open_router |
| Qwen3.7 Max | `qwen/qwen3.7-max` | 29.5 | 1279 | — | 1000k | free lane: xkiro `:free` | open_router |
| Gemini 3.1 Pro Preview | `google/gemini-3.1-pro-preview` | 29.7 | 1257 | — | 1049k | 2.0/12.0 | open_router |
| MiniMax M3 | `minimax/minimax-m3` | 29.2 | 1262 | — | 1049k | 0.3/1.2 | open_router |
| Claude Opus 4.5 | `anthropic/claude-opus-4.5` | 29.1 | 1251 | — | 200k | 5.0/25.0 | open_router |
| GPT-5.2-Codex | `openai/gpt-5.2-codex` | 28.5 | 1142 | — | 400k | 1.75/14.0 | open_router |
| Solar Pro 4 | `upstage/solar-pro4` | 28.2 | 1186 | 11 | 524k | 0.09/0.36 | open_router |
| GLM 5 | `z-ai/glm-5` | 27.9 | 1252 | — | 205k | 0.6/1.92 | open_router, zai |
| Kimi K2.6 | `moonshotai/kimi-k2.6` | 27.0 | 1275 | — | 262k | 0.65/3.41 | open_router |
| Qwen3.6 Plus | `qwen/qwen3.6-plus` | 27.0 | 1245 | — | 1000k | free, xkiro `:free` | open_router |
| GLM 5 Turbo | `z-ai/glm-5-turbo` | 26.6 | 1267 | — | 203k | 1.2/4.0 | open_router, zai |
| MiMo-V2.5-Pro | `xiaomi/mimo-v2.5-pro` | 26.0 | 1271 | — | 1050k | 0.43/0.87 | open_router |
| Kimi K2.7 Code | `moonshotai/kimi-k2.7-code` | 25.8 | 1268 | — | 262k | 0.67/3.35 | open_router |
| Grok 4.20 | `x-ai/grok-4.20` | 25.7 | 1234 | — | 2000k | 1.25/2.5 | open_router |
| Inkling | `thinkingmachines/inkling` | 25.0 | 1228 | — | 1049k | 1.0/4.05 | open_router |
| GLM 5.1 | `z-ai/glm-5.1` | 26.1 | 1290 | — | 205k | 0.97/3.04 | open_router, zai |
| Qwen3.7 Plus | `qwen/qwen3.7-plus` | 25.2 | 1275 | — | 1000k | free, xkiro `:free` | open_router |
| MiMo-V2.5 | `xiaomi/mimo-v2.5` | 25.2 | 1270 | — | 1050k | 0.14/0.28 | open_router |
| Nemotron 3 Ultra | `nvidia/nemotron-3-ultra-550b-a55b` | 22.9 | 1149 | 16 | 1000k | free variant live | open_router |
| Grok 4.3 | `x-ai/grok-4.3` | 24.9 | 1200 | — | 1000k | 1.25/2.5 | open_router |
| GLM 5V Turbo | `z-ai/glm-5v-turbo` | 23.5 | 1234 | — | 203k | 1.2/4.0 | open_router |
| Solar Mini 4 | `upstage/solar-mini4` | 24.1 | 1168 | 15 | 524k | 0.05/0.2 | open_router |
| DeepSeek V4 Flash 0423 | `deepseek/deepseek-v4-flash` | 24.4 | 1211 | 19 | 1049k | 0.03/1.28 | open_router |

### 5.1 Subscription lane: OpenCode Go

`opencode_go` is a paid plan, not a free tier, and the FreeGuard adapter says so
(`billing_class=SUBSCRIPTION`, `requires_subscription=True`). Against an existing subscription
the models it opens cost no credits per call, so they belong in a curated list even though they
are not "free" by the guard's definition. 23 of its 31 catalogued models answered the liveness
probe; 14 of those are in the S/A shortlist and give a second live provider for the strongest
models:

| Model | Reference | Probe |
| --- | --- | --- |
| GLM 5.3 | `opencode_go/glm-5.3` | 200, 2.4 s |
| GLM 5.3 Flash | `opencode_go/glm-5.3-flash` | 200, 1.2 s |
| Kimi K3 | `opencode_go/kimi-k3` | 200, 2.2 s |
| MiMo-V2.6-Pro | `opencode_go/mimo-v2.6-pro` | 200, 2.7 s |
| GLM 5.2 | `opencode_go/glm-5.2` | 200, 2.7 s |
| MiMo-V2.6-Flash | `opencode_go/mimo-v2.6-flash` | 200, 5.7 s |
| Grok 4.6 | `opencode_go/grok-4.6` | 200, 2.7 s |
| GPT-6 Luna | `opencode_go/gpt-6-luna` | 200, 1.5 s |
| MiniMax M3 | `opencode_go/minimax-m3` | 200, 0.8 s |
| GPT-5.6 Luna | `opencode_go/gpt-5.6-luna` | 200, 1.0 s |
| MiMo-V2.5-Pro | `opencode_go/mimo-v2.5-pro` | 200, 4.7 s |
| Kimi K2.7 Code | `opencode_go/kimi-k2.7-code` | 200, 2.1 s |
| Qwen3.7 Plus | `opencode_go/qwen3.7-plus` | 200, 3.7 s |
| MiMo-V2.5 | `opencode_go/mimo-v2.5` | 200, 25.6 s |

Two account settings, not billing, explain the refusals: four models answer
`This Go model requires Global regions. Select Global in your workspace's Privacy settings to
use it` (`deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v4.1-flash`, both
`muse-spark-*-contributor`), and two answer `ModelProtocolUnsupported` (`claude-haiku-5-5`,
`minimax-m2.7`) because they do not speak the Messages protocol. Every one of these calls needs
a session — see §9.

To let strict mode accept a subscription tier, turn off Admin → Free Models →
*Require no subscription* (`FCC_FREE_GUARD_REQUIRE_NO_SUBSCRIPTION=false`). With the default
(`true`) these models are confirmed not-free and stay blocked, which is the guard behaving as
designed, not a defect.

## 6. Free lane

Only four shortlisted models answered through a `:free` variant, and one provider lane is unreliable.
All four were re-probed later the same day and answered 200 again:

| Reference | Probe |
| --- | --- |
| `open_router/nvidia/nemotron-3-ultra-550b-a55b:free` | 200, 14.2 s / re-check 1.7 s |
| `xkiro/qwen/qwen3.7-max:free` | 200, 4.0 s / re-check 3.6 s |
| `xkiro/qwen/qwen3.6-plus:free` | 200, 5.5 s / re-check 3.4 s |
| `xkiro/qwen/qwen3.7-plus:free` | 200, 13.5 s / re-check 3.6 s |

Other `:free` probes on OpenRouter returned HTTP 429 (`gemma-4-*`, `nemotron-3.5-lightning`,
`nemotron-3-super-120b-a12b`, `cohere/north-mini-code`) — the anonymous free tier is rate-limited, not
dead, so those will succeed intermittently. `xkiro` returned HTTP 503 for eight other `:free` models
after ~35 s each, so it cannot be treated as a dependable lane. Two live non-shortlisted free models
were seen in passing: `open_router/poolside/laguna-s-2.1:free` (7.7 s) and
`open_router/poolside/laguna-xs-2.1:free` (9.7 s).

## 7. Caveats

- Provider concentration. 48 of the 66 shortlisted models are reachable only through
  `open_router`; 18 have a second live provider (14 also answer on `opencode_go` when the caller
  carries a session, 6 also on `zai`, 1 also on `groq`). If the OpenRouter key runs out of credit
  or is rate-limited, what survives is the GLM set on `zai`, the Go subscription lane, one `groq`
  model, and the flaky `xkiro` free lane. A second OpenRouter account in the same setting
  (`OPENROUTER_API_KEY=k1,k2`) rotates on a spent quota before any model change, which is the
  cheapest way to widen this; see the credential-pool note in `README.md`.
- Liveness here means "answered one tiny instruction-following probe". It is not a capability test.
  `open_router/z-ai/glm-4.6` is the counter-example: HTTP 200, but it spent the whole 512-token budget
  on reasoning and returned empty text — a model that would look alive to a ping-only check.
- Prices and rankings are OpenRouter's, fetched on the snapshot date. Free-variant availability changes
  faster than the models do.
- The four-tier Claude aliases in the built-in list (`claude-fable-5`, `claude-opus-4-20250514`, …)
  resolve through `MODEL`/`MODEL_*` and are unaffected by the allowlist.
- `catalog_filter_only` and `strict_free_only` FreeGuard modes filter by *payment*, not quality. The
  allowlist is a separate, additive condition; with FreeGuard strict enabled a curated model can still
  be hidden because its eligibility evidence is unconfirmed.

## 8. Re-running the audit

The probes are ordinary HTTP calls; the shortlist can be rebuilt with `curl` alone:

```bash
# catalog
curl -s http://127.0.0.1:8082/v1/models | jq -r '.data[].id' > catalog.txt
# liveness of one reference
curl -s -X POST http://127.0.0.1:8082/v1/messages \
  -H 'content-type: application/json' \
  -d '{"model":"anthropic/open_router/z-ai/glm-5.3","max_tokens":512,
       "messages":[{"role":"user","content":"Reply with exactly: PONG-7391"}]}'
# quality signals
curl -s https://openrouter.ai/api/v1/models | jq '.data[] | {id, benchmarks, context_length}' > or.json
curl -s 'https://openrouter.ai/api/v1/models?category=programming' | jq -r '.data[].id' | nl
curl -s https://models.dev/api.json | jq '.openrouter.models | to_entries' > models_dev.json
```

The analysis scripts used for this snapshot were kept out of the repository (`.work/`, scratch).

## 9. Session and client identity

Some upstreams bind a subscription or a free tier to a session instead of to the API key, and
reject a session-less call outright. `opencode_go` is the case found here: without a session every
one of its 31 models answers HTTP 400 `MissingSessionID`, and with one 23 of them answer normally.

The proxy forwards what the caller carries and never invents an identity — binding one account's
usage to a session its caller does not own is worse than the rejection. `core/request_identity.py`
owns the header spellings; providers map them to what their upstream expects.

- Inbound session names, in precedence order: `x-opencode-session`, `session-id`, `x-session-id`,
  `x-claude-code-session-id`, `session_id`, `x-grok-session-id`,
  `x-meta-ai-gateway-session-id`, `x-tbh-session-id`, `x-fcc-launch-id`, `anthropic-session-id`,
  `x-anthropic-session-id`, `claude-session-id`, `x-claude-session-id`.
- Inbound client-name names: `x-fcc-client-label`, `x-client-name`, `x-fcc-client`, `x-client`,
  `x-app`.
- Outbound: a provider with a declared spelling (`opencode_zen`, `opencode_go` →
  `x-opencode-session`) gets exactly that header; every other upstream gets the canonical
  `x-session-id` plus `x-client-name` when the caller named itself. Non-ASCII and over-long
  values are dropped, and a caller that names nothing sends nothing.
- Wiring: `providers/opencode` (which had its own private header list), `providers/openai_chat`
  and `providers/custom`. The Chat and custom transports already accepted `extra_headers`; the
  providers were discarding `request_headers` before reaching them. The native Anthropic messages
  transport has no `extra_headers` channel and was left alone.
- Launcher side: `fcc launch claude` now sets `ANTHROPIC_CUSTOM_HEADERS` with
  `x-fcc-launch-id: <launch id>` and `x-fcc-client-label: claude-code`, preserving a value the
  user already had. Aider, Cline and Hermes launchers already sent `x-fcc-launch-id`.

Measured before this change: 0 of 5463 logged requests carried any session header, so nothing on
this server could have reached the Go lane. Reproduce the gate in one command:

```bash
SID=fcc-probe-$(date +%s)
# sessionless: HTTP 400 MissingSessionID
curl -s -X POST http://127.0.0.1:8082/v1/messages -H 'content-type: application/json' \
  -d '{"model":"anthropic/opencode_go/glm-5.3","max_tokens":64,
       "messages":[{"role":"user","content":"Reply with exactly: PONG-7391"}]}'
# with a session: HTTP 200
curl -s -X POST http://127.0.0.1:8082/v1/messages -H 'content-type: application/json' \
  -H "x-claude-code-session-id: $SID" \
  -d '{"model":"anthropic/opencode_go/glm-5.3","max_tokens":64,
       "messages":[{"role":"user","content":"Reply with exactly: PONG-7391"}]}'
```

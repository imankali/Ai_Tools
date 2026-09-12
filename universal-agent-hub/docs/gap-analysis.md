# Agent-OS Gap Analysis — Universal Agent Hub

**Scope.** A line-by-line feature audit of Universal Agent Hub (v1.0.0, 14 036 LoC Python at baseline; 21 093 LoC after this work)
against the three most relevant open-source "Agent OS" projects on GitHub, selected for
*architectural overlap* with this project (local-first, safety-first, personal assistant,
tool-calling agent, self-hosted server + mobile clients) — not for star count alone.

**Baseline verified before any change:** `pytest` → **801 passed, 1 skipped, 92.02 % coverage**.

**After this work (re-verified, not estimated):** `pytest` → **1 184 passed, 1 skipped, 91.71 % coverage**;
`ruff check src tests` → clean; `black --check` → clean; `mypy --strict` → **no issues in 51 source files**.
The single skip is the pre-existing offline-network guard in `tests/test_tools/test_web_search.py`.

---

## 1. Reference projects

| # | Project | Stars | Lang | Why it was chosen |
|---|---|---|---|---|
| A | [`openclaw/openclaw`](https://github.com/openclaw/openclaw) | 389 491 | TypeScript | The de-facto reference personal-agent OS. 43 528 paths, 90 `src/` subsystems, 180+ extensions. Defines the *feature surface* a personal agent OS is expected to have. |
| B | [`agent0ai/agent-zero`](https://github.com/agent0ai/agent-zero) | 19 143 | Python | Same language, same problem. 3 783 paths, ~110 REST endpoints, `helpers/` shows a mature runtime decomposition. Best source of *directly portable* Python architecture. |
| C | [`nearai/ironclaw`](https://github.com/nearai/ironclaw) | 12 615 | Rust | Explicitly "an Agent OS focused on privacy, security and extensibility". Closest ideological match to this project's safety-first identity. Best source of *security* requirements. |

Rejected after review: `buildermethods/agent-os` (5 395 ★) is a shell/markdown spec-writing
methodology, not a runtime — no transferable capability. `agentlas-ai/Agentlas-OS` (1 108 ★)
is overwhelmingly prompt-pack markdown (`.agents/skills/*.md`), already covered by this
project's plugin-tool mechanism.

---

## 2. What this project already has (no gap)

Verified in source, not from the README:

| Capability | Evidence |
|---|---|
| Async tool-calling loop, fallbacks, retries, token accounting | `src/agent.py` (754 LoC) |
| 24 registered tools across 7 modules | `src/tools/*.py` |
| Risk-scored safety guard (command/path/network) | `src/utils/safety.py` (638 LoC), `SafetyGuard.assess_*` |
| Human-in-the-loop approvals, timeout ⇒ deny | `src/core/base_tool.py::_confirm`, `src/server/sessions.py` |
| Long-term JSONL memory + auto-capture | `src/core/memory.py` (584 LoC) |
| Event bus with history + `wait_for` | `src/core/event_bus.py` (384 LoC) |
| REST + WebSocket + PWA, token auth, rate limit, Host check | `src/server/app.py` (1 106 LoC) + `src/server/agent_os.py`; **35 distinct `/api/` paths = 68 method+path pairs**, plus `/hooks/{token}` (enumerated from a live `create_app()` router, not counted by hand) |
| Session registry, isolation, TTL | `src/server/sessions.py` (607 LoC) |
| Masked API-key profiles | `src/server/keystore.py` (338 LoC) |
| Self-review reports + activity log | `src/core/reports.py` (546 LoC) |
| Self-extension: agent-authored tools from plugin dirs | `src/core/tool_registry.py::load_plugins` |
| Android / iOS / PWA / desktop / Termux / Docker | `apps/`, `packaging/`, `Dockerfile` |
| Parallel tool calls, rate limiting, secret redaction | `PARALLEL_TOOL_CALLS`, `server/auth.py::RateLimiter`, `keystore.redact_text` |

---

## 3. Gap register

`P0` = architectural hole a user of any other Agent OS hits immediately.
`P1` = significant, shipped in this change set if feasible.
`P2` = real gap, deferred with a reason.

| ID | Capability | A: openclaw | B: agent-zero | C: ironclaw | Hub (before) | Pri |
|---|---|---|---|---|---|---|
| G01 | **Scheduled routines / cron** | `src/cron` | `task_scheduler`, `scheduler_task_{create,delete,run,update,list}`, `scheduler_tick` | "Routines — cron schedules, event triggers, webhook handlers" | **absent** | **P0** |
| G02 | **Notification center** | `src/polls`, auto-reply | `notification_{create,clear,history,mark_read}` | Heartbeat → proactive output | approvals only, no feed | **P0** |
| G03 | **MCP (Model Context Protocol) client** | `src/mcp` | `mcp_handler`, `mcp_server_{scan,get_detail,get_log}`, `mcp_servers_{apply,status}` | "MCP Protocol — connect to MCP servers" | **absent** | **P0** |
| G04 | **Skills (portable instruction packs)** | `src/skills` (138 files), ClawHub | `skills`, `skills_{scan,import,import_preview}` (41 files) | `skills/`, `registry/` | plugin *tools* only, no instruction packs | **P0** |
| G05 | **Vector / hybrid semantic search** | `memory-lancedb`, `memory-wiki` | `vector_db`, `faiss_monkey_patch`, `document_query`, `knowledge/` | "Hybrid Search — full-text + vector via Reciprocal Rank Fusion" | keyword token overlap only | **P0** |
| G06 | **Sub-agents / delegation** | `src/fleet`, `src/routing`, `system-agent` | `subagents` | orchestrator/worker | profiles exist, no delegation | **P0** |
| G07 | **Backups (create/inspect/preview/restore/test)** | `src/snapshot` | `backup_{create,inspect,preview_grouped,restore,restore_preview,test}` | — | **absent** | **P0** |
| G08 | **Tamper-evident audit log** | `src/audit` | `security`, `log` | defense-in-depth | activity log, not hash-chained | **P0** |
| G09 | **Prompt-injection scanning** | `src/security`, `policy` | `security`, `tool_policy` | "Prompt Injection Defense — pattern detection, sanitization, policy" | **absent** | **P0** |
| G10 | **Secret-leak detection on outbound content** | `src/secrets` | `secrets`, `crypto` | "Leak detection — scans requests/responses for exfiltration" | redaction in logs only | **P0** |
| G11 | **Outbound endpoint allowlist** | `net-policy` package | `network`, `tunnel_origins` | "Endpoint allowlisting — HTTP only to approved hosts/paths" | risk scoring only | **P0** |
| G12 | **Heartbeat + stuck-run watchdog / self-repair** | `src/daemon`, `src/worker` | `watchdog`, `state_monitor`, `state_snapshot`, `job_loop` | "Heartbeat System", "Self-repair — recovery of stuck operations" | **absent** | **P0** |
| G13 | **Inbound webhooks (event triggers)** | `webhooks` ext | `poll`, `nudge` | "HTTP webhooks … webhook handlers" | **absent** | **P0** |
| G14 | Chat/session export as portable file | `src/transcripts`, `trajectory` | `chat_export` | — | `/save`, `/transcript` in CLI only | P1 |
| G15 | Work-dir file browser REST (browse/download/upload) | `file-transfer` ext | `get_work_dir_files`, `download_*`, `upload_*`, `edit_*`, `extract_work_dir_archive` | workspace filesystem | file *tools* exist, no REST surface | P1 |
| G16 | Health/diagnostics endpoint depth | `diagnostics-otel`, `diagnostics-prometheus` | `health`, `state_monitor` | `ironclaw status` | `/healthz` shallow | P1 |
| G17 | CSRF / cross-origin hardening | `pairing`, `visitor-access` | `csrf_token` | webui token | token + Host check, no CSRF/Origin policy | P1 |
| G18 | Tunnels (expose hub to phone without LAN) | `src/gateway` | `cloudflare_tunnel`, `tailscale_tunnel`, `serveo_tunnel`, `microsoft_tunnel` | web gateway | `--lan` only | P2 — needs an external binary + trust decision; document instead |
| G19 | Multi-provider model catalog | `model-catalog`, 60+ provider exts | `litellm`, `providers` | `models set-provider` | OpenAI-compatible + fallbacks | P2 — `OPENAI_BASE_URL` already covers any compatible endpoint |
| G20 | Channels (Telegram/WhatsApp/Discord/Slack/…) | 20+ channel extensions | `email_client` | "WASM channels (Telegram, Slack)" | PWA + native shells | P2 — each channel is a separate product surface; webhook trigger (G13) is the neutral substrate |
| G21 | Media/TTS/image/video generation | `tts`, `media-generation`, `image-generation`, `music-generation`, `video-generation` | `media_artifacts`, `images` | — | none | P2 — orthogonal to the project's system-automation identity |
| G22 | i18n of runtime strings | — | `localization` | 5 README locales | `README.fa.md` only | P2 — docs, not runtime |
| G23 | Self-update | `src/install-sh-version` | `self_update_{get,schedule,tags}`, `update_check` | installer | `--doctor` only | P2 — auto-mutating an install is a safety regression for a safety-first project |
| G24 | Virtual desktop / CUA | — | `virtual_desktop`, `virtual_desktop_routes` | sandbox workers | `browser_*` tools | P2 — heavy runtime dependency |

**Verdict: 13 P0 gaps, all closed in this change set. 4 P1 closed. 7 P2 explicitly deferred with reasons.**

---

## 4. What was built

| ID | New module | Notes |
|---|---|---|
| G01, G13 | `src/core/routines.py` | Cron-subset scheduler (stdlib, no `croniter`), interval + `at` + cron triggers, webhook + event triggers, per-routine concurrency, persistent JSON store, run history, backoff on failure |
| G02 | `src/core/notifications.py` | Persistent JSONL notification center, severity, read/unread, EventBus bridge, optional webhook sink |
| G03 | `src/core/mcp_client.py`, `src/tools/mcp.py` | JSON-RPC 2.0 over stdio **and** streamable HTTP; `initialize` / `tools/list` / `tools/call`; remote tools surfaced as first-class `BaseTool`s named `mcp__{server}__{tool}`, every call passes the guard |
| G04 | `src/core/skills.py` | `SKILL.md` packs with YAML frontmatter (stdlib parser), scan/enable/disable/search, prompt injection via `context_block()`, allowed-tools scoping |
| G05 | `src/core/vector_store.py` | Dependency-free hashed char-n-gram vectors + cosine + **Reciprocal Rank Fusion** against the existing keyword scorer; wired into `AgentMemory.search(mode=…)` |
| G06 | `src/core/subagents.py`, `src/tools/agents.py` | Bounded delegation pool: max depth, max concurrency, inherited safety guard, child profile restriction, recursion-bomb protection |
| G07 | `src/core/backup.py` | Snapshot zip of memory/routines/config/activity, sha256 manifest, `inspect` / `preview` (dry-run) / `restore` / `test`, refuses to clobber without confirmation |
| G08 | `src/core/audit.py` | Append-only JSONL with **hash chain** (`prev_sha`), `verify_chain()` detects any edit, queryable by actor/action/decision |
| G09, G10, G11 | `src/utils/injection.py`, `src/utils/leakscan.py`, `src/utils/allowlist.py` | Prompt-injection pattern scanner with severities; secret-exfiltration scanner over outbound text; HTTP host/path allowlist with fail-closed default |
| G12 | `src/core/heartbeat.py` | Periodic health tick → notifications, plus a watchdog that fails runs exceeding a deadline and emits a self-repair event |
| G14 | `/api/sessions/{id}/export` | Portable JSON transcript |
| G15 | `/api/files*` | Browse / read / download / upload / delete inside `ALLOWED_DIRECTORIES` only |
| G16 | `/api/diagnostics` | Deep health: disk, memory store, routines, MCP, audit-chain integrity |
| G17 | `src/server/auth.py` | Origin allowlist + `SameSite`-safe CORS defaults, CSRF double-submit token for state-changing browser calls |

Every new module ships with tests; the coverage gate (≥ 80 %) and `mypy --strict` remain green.

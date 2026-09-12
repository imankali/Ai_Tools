# Agent-OS Layer

Routines, notifications, skills, MCP, sub-agents, backups, audit, heartbeat.

These subsystems turn the agent from something that only *answers* into something
that can *act on a schedule, remember procedures, delegate, and prove what it did*.
Everything here is local-first, has no new hard dependency, and is off-by-default
wherever turning it on means running something in the background.

Composition happens in one place — [`src/core/services.py`](../src/core/services.py)
(`ServiceHub`) — so the CLI, the HTTP server and the agent loop all see the *same*
state. Never construct these services ad hoc in a new entry point.

---

## 1. Routines — `src/core/routines.py`

A routine is "run this prompt on this trigger". Five trigger kinds:

| Trigger | `expression` | Example |
|---|---|---|
| `cron` | 5-field cron | `0 22 * * *` (22:00 daily) |
| `interval` | seconds (≥ 5) | `3600` |
| `once` | ISO-8601 | `2026-12-31T09:00:00Z` |
| `webhook` | short ASCII token | `deploy` → `POST /hooks/deploy` |
| `event` | fnmatch pattern | `agent.run.*` |

```bash
agent-hub --schedule nightly --cron "0 22 * * *" --prompt "summarise today" --profile read_only
agent-hub --schedule deploy-hook --webhook deploy --prompt "run the deploy checklist"
agent-hub --routines
agent-hub --unschedule nightly
```

Design rules, in the order they bite:

1. **Fail-closed loading.** A stored routine whose `expression` no longer parses is
   *disabled*, not retried every tick.
2. **Backoff.** Each consecutive failure multiplies the next delay (`2^n`, capped at
   `max_backoff`), so a broken routine cannot burn tokens or fill the log.
3. **No concurrent self.** A routine already in flight is recorded as `skipped`.
4. **`once` disables itself** after firing.
5. **Cron follows the traditional rule**: when *both* day-of-month and day-of-week
   are restricted, they are OR-ed, not AND-ed.

The cron parser is stdlib-only (no `croniter`) so Termux/Android installs work.
It supports `*`, `a`, `a-b`, `a-b/step`, `*/step`, `a,b,c` and names (`mon`, `jan`).

### Webhooks

`POST /hooks/{token}` is deliberately **outside** `/api/`, so the server token does
not apply — the unguessable routine token *is* the credential. That is what lets an
external system (GitHub, monitoring, IFTTT) wake the agent without holding your
server token. If you do not want that surface, do not create a `webhook` routine;
no other unauthenticated path exists.

---

## 2. Notifications — `src/core/notifications.py`

Until now the only unsolicited output was an *approval request* that blocks.
The notification center is the non-blocking counterpart: a persistent JSONL feed.

```bash
agent-hub --notifications
agent-hub --clear-notifications
curl -H "Authorization: Bearer $TOKEN" http://host:8765/api/notifications
```

`attach_bus()` bridges the `EventBus`: `agent.run.failed`, `tool.blocked`,
`safety.blocked`, `routine.failed`, `backup.created`, `audit.chain_broken`,
`heartbeat.degraded` and friends become notifications automatically. Titles and
bodies pass through secret redaction before they are stored.

Set `NOTIFICATION_WEBHOOK` to also POST every notification (best-effort; a
unreachable webhook never breaks the run).

---

## 3. Skills — `src/core/skills.py`

A *tool* is a function. A *skill* is knowledge + procedure. Drop a folder with a
`SKILL.md` into `SKILLS_DIRS`:

```markdown
---
name: postgres-backup
description: Take a consistent logical backup of a PostgreSQL database.
version: 1.0.0
tags: [db, ops]
allowed_tools: [terminal_run, read_file]
---
# PostgreSQL backup
1. Run pg_dump --format=custom.
2. Verify the archive with pg_restore --list.
```

The frontmatter parser is a deliberate **subset** of YAML (scalars, inline lists,
block lists) — not a full parser, and no PyYAML dependency.

Two safety properties:

* A skill whose body trips the injection scanner at `high` or above is **rejected**
  and reported in `errors`, never injected.
* `enabled` state lives in `skills-state.json` **inside** the skill directory, so two
  installations (or two test runs) cannot silently share state.

The agent reaches skills through `skill_list` / `skill_load`.

---

## 4. MCP — `src/core/mcp_client.py`

Connect to any [Model Context Protocol](https://modelcontextprotocol.io) server and
its tools become first-class agent tools named `mcp__{server}__{tool}` — registered
into the normal `ToolRegistry`, so the model sees them exactly like `read_file`.

### The bridge (`src/tools/mcp.py`)

Registration builds one `BaseTool` subclass **per remote tool** rather than one
generic dispatcher, because `ToolRegistry` is class-keyed (`register(tool_class)`,
`get(name) ⇒ tool_class(config)`). Building a class per tool means schemas,
filtering, the `registry.changed` event and the confirmation flow all work with no
changes to the registry.

Two independent rejection layers, both tested:

1. `MCPClient` scans every tool description at `tools/list` and drops the tool.
2. `build_tool_class()` scans again, so a description that slips past the first
   layer is still not registered.

A rejected tool returns `None` instead of raising, so one poisoned tool cannot take
down the other nine on the same server. `MCPToolAdapter` itself carries an empty
`name` on purpose — otherwise `discover_tools()` would register the base class as a
phantom `mcp_tool` the model could call and that does nothing.

`ServiceHub.connect_mcp()` registers the tools after all servers connect, and
`stop()` unregisters them; `/api/diagnostics` reports `mcp.registered_tools` so a
silent rejection is visible somewhere.

```json
{
  "mcpServers": {
    "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"] },
    "remote":     { "url": "https://example.com/mcp", "headers": { "Authorization": "Bearer …" } }
  }
}
```

Defaults to `~/.universal-agent-hub/mcp.json` (`MCP_CONFIG_FILE`). The `mcpServers`
key matches the ecosystem convention, so existing config files work unchanged.

Security properties that are *not* optional:

* **stdio subprocesses start with a near-empty environment** — only `PATH`, `HOME`,
  `LANG`, plus whatever you list explicitly under `"env"`. Your `.env` secrets do
  **not** leak to third-party MCP servers by default.
* **Tool descriptions are untrusted input** and are scanned; a tool advertising
  "ignore all previous instructions…" is never registered.
* **`tools/call` arguments pass the `SafetyGuard`**: a `command`/`path`/`url`
  argument is assessed exactly like a local tool call, and a blocked one raises.
* Response size is capped at 4 MiB and every request has a timeout.

---

## 5. Sub-agents — `src/core/subagents.py` + `agent_delegate`

Delegation is bounded on purpose:

| Brake | Default | Env |
|---|---|---|
| Max chain depth | 2 | `SUBAGENT_MAX_DEPTH` |
| Max concurrent | 3 | `SUBAGENT_MAX_CONCURRENT` |
| Allowed profiles | all | `SUBAGENT_PROFILES` |

A child agent inherits the parent's guard and cannot loosen it; a child's profile
can only be *more* restrictive. `agent_delegate` is `requires_confirmation = True`,
because spawning a second agent with its own tool budget is a user-visible decision.
Depth exhaustion returns `rejected` — it does not throw, and it does not recurse.

---

## 6. Backups — `src/core/backup.py`

Backs up **agent state only** (memory, routines, notifications, audit, activity;
`keys.json` only with `include_keys=True`, default `false`) — never your files.

```bash
agent-hub --backup          # create
agent-hub --backups         # list, each verified against its sha256 manifest
```

`restore` requires `confirm=True`. Without it you get a dry-run `RestorePlan`
listing what *would* be written, skipped or overwritten. A real restore first takes
a safety backup of the current (possibly broken) state, so a bad restore is always
reversible. `test()` verifies checksums **without** restoring.

Source files are re-read at every `create()` — a state file created after startup
(e.g. the first `memory.jsonl`) is not silently missed.

---

## 7. Audit log — `src/core/audit.py`

Append-only JSONL where each entry hashes the previous one:

```
sha = sha256(canonical_json(entry_without_sha))
```

`verify_chain()` reports the exact index where the chain breaks and why
(`index gap` / `prev_sha mismatch` / `sha mismatch` / `unparsable`). This is a
checksum chain, not a blockchain — it answers exactly one question: *did anyone
edit the log?*

Detail fields named `*token*`, `*secret*`, `*password*`, `*api_key*`, `*credential*`
are redacted before they touch disk. Rotation resets the chain from genesis and
keeps one previous generation (`.1`).

The heartbeat re-verifies the chain on every tick and raises a `critical`
notification if it is broken.

---

## 8. Security layers — `src/utils/{injection,leakscan,allowlist}.py`

### Prompt-injection scanner
19 patterns (IDs `PI-001…PI-016`, plus `PI-101…PI-103` for Persian, because this
project is bilingual and so is the attack surface). Text is NFKC-normalised first so
invisible-joiner and full-width tricks do not split a pattern. `sanitize_untrusted()`
strips chat-template control tokens (`<|im_start|>`, `[INST]`) and wraps the rest in
an explicit `<untrusted_content>` fence whose backtick run is longer than anything
in the payload — so the payload cannot close it.

Honest scope: this is a **detector**, not a proof. No regex catches every injection.

### Leak detector
Unlike generic `redact_secrets`, this knows *your actual values* — the secrets in
your environment and keystore — and redacts them from outbound text. Overlapping
matches resolve in favour of a known secret over a generic pattern, because a
finding with a named source is the actionable one.

### Endpoint allowlist
Fail-closed: an empty list denies everything, and `*` allows everything only when
written explicitly. Supports `*.example.com`, `example.com/api/` and
scheme-locked `https://example.com`.

---

## 9. Heartbeat & watchdog — `src/core/heartbeat.py`

* **Heartbeat** — periodic checks (disk space, audit-chain integrity, plus anything
  you register) → notifications. **Off by default** (`HEARTBEAT_ENABLED=false`):
  a background loop should be an explicit choice.
* **Watchdog** — tracks in-flight runs and releases any that outlive
  `WATCHDOG_TIMEOUT`, emitting a notification and an audit record. Without it, one
  stuck `await` means a session stuck in "running" forever.

Neither deletes anything. They report and keep state consistent; deleting is your call.

---

## 10. Memory search modes — `src/core/memory.py`

`AgentMemory.search(query, mode=…)` accepts three modes:

| Mode | Ranking |
|---|---|
| `keyword` (default) | `MemoryRecord.score()` — token overlap **+ recency + pin + hits** |
| `vector` | cosine over hashed char n-grams (`src/core/vector_store.py`) |
| `hybrid` | the two above fused with Reciprocal Rank Fusion |

The default is deliberately unchanged, so no existing behaviour shifts. The
interesting part is *why* hybrid needs its own lexical channel: `MemoryRecord.score()`
adds recency, so a brand-new record with **zero** word overlap still scores ≈ 1.5 and
clears the `0.9` threshold. Feeding that into RRF made hybrid *worse* than vector —
measured, not assumed. The RRF keyword channel therefore comes from
`_lexical_overlap()` (pure token overlap), not from `score()`.

The vector index is cached and invalidated by comparing the **set of record ids**,
not by a mutation counter, because records change through several paths
(`add` / `forget` / `import` / `prune`) and one forgotten counter means silently
stale search results.

---

## 11. HTTP surface

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/routines` | GET / POST | list / create |
| `/api/routines/{id}` | PATCH / DELETE | update / delete |
| `/api/routines/{id}/history` | GET | run history |
| `/api/notifications` | GET / DELETE | feed / clear |
| `/api/notifications/read` | POST | mark one or all read |
| `/api/skills`, `/api/skills/{name}` | GET | packs / one body |
| `/api/mcp` | GET | servers and remote tools |
| `/api/backup` | GET / POST | list / create |
| `/api/backup/{name}/restore` | POST | restore (needs `confirm`) |
| `/api/backup/{name}` | DELETE | delete |
| `/api/audit` | GET | entries + chain verdict |
| `/api/diagnostics` | GET | every subsystem at once |
| `/hooks/{token}` | POST | routine webhook (see §1) |

Everything under `/api/` keeps the existing token auth, Host check and rate limit.

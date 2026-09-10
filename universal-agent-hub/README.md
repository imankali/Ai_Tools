# Universal Agent Hub

**A production-grade, safety-guarded AI agent with real (and configurable) system access —
on every OS, from a CLI, a desktop window, or your phone.**

You give it one model API key on your own machine. It reads your environment, plans, uses
tools (shell, files, browser, search, system info), remembers what it learned between runs,
asks before doing anything destructive, and reports exactly what it did.

```
        your machine                                     your phone / laptop
┌───────────────────────────────┐                ┌────────────────────────────────┐
│ UniversalAgent  · SafetyGuard │   REST + WS    │ Android APK · iOS app · PWA    │
│ 23 tools · memory · reports   │ ◄─────────────►│ chat · approvals · tools ·     │
│ src/server (aiohttp, token)   │   token only   │ keys · settings · reports      │
└───────────────────────────────┘                └────────────────────────────────┘
        model API key never leaves this machine
```

[![Tests](https://github.com/imankali/Ai_Tools/actions/workflows/tests.yml/badge.svg)](https://github.com/imankali/Ai_Tools/actions/workflows/tests.yml)
[![Lint](https://github.com/imankali/Ai_Tools/actions/workflows/lint.yml/badge.svg)](https://github.com/imankali/Ai_Tools/actions/workflows/lint.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

---

## 60-second start

```bash
git clone https://github.com/imankali/Ai_Tools && cd Ai_Tools/universal-agent-hub
make install                                   # venv + deps
cp .env.example .env && $EDITOR .env           # OPENAI_API_KEY=…

make run                                       # interactive chat in the terminal
make serve-lan                                 # phone-ready UI: prints URL + one-time token
```

Then open the printed URL on your phone (or in a browser) and paste the token. That's it.

Prefer one-liners:

```bash
curl -fsSL https://raw.githubusercontent.com/imankali/Ai_Tools/main/universal-agent-hub/scripts/install.sh | bash    # macOS/Linux
powershell -c "irm https://raw.githubusercontent.com/imankali/Ai_Tools/main/universal-agent-hub/scripts/install.ps1 | iex"  # Windows
```

## What you get

| | |
|---|---|
| **Agent core** | async tool-calling loop with model fallbacks, retries, token accounting, history windowing, event stream |
| **23 tools** | shell `terminal_run` · files `read_file` `write_file` `list_directory` `search_files` `move_file` `delete_file` · system `os_info` `cpu_info` `memory_info` `disk_info` `network_info` · web `web_search` `search_advanced` `browser_browse` `browser_screenshot` `browser_extract_text` `browser_click` `browser_fill_form` · memory `memory_write` `memory_read` `memory_search` `memory_forget` |
| **Safety layer** | `SafetyGuard`: command/path/network risk assessment, allow-lists, deny or confirm policies, redaction of secrets in every log/response |
| **Human approvals** | every risky action blocks until you approve it — in the CLI, the browser, or a push-style notification on your phone; timeout or disconnect = denied |
| **Long-term memory** | JSONL memory store (`src/core/memory.py`): preferences, procedures, decisions, open plans — auto-loaded into the prompt, auto-captured after runs |
| **Self-review reports** | `agent-hub --report` / the Reports tab: what it did per day, which tools it leans on, what it wants to do next, and where it was blocked |
| **Self-extension** | agent-authored tools are loaded from plugin dirs (`AGENT_HUB_TOOL_DIRS`) and writing them needs your approval; feedback is stored in memory (`--remember`) and can be turned into a patch proposal in `proposals/` plus a `make test` run — a human commits, never the agent |
| **API server** | REST + WebSocket + PWA on aiohttp; token auth, rate limiting, session isolation, API-key profiles (masked, never echoed) |
| **Apps** | Android WebView shell, iOS SwiftUI shell, `--desktop` window (pywebview) or browser, PWA install anywhere |
| **Quality** | 801 passing tests, ≥ 92 % coverage, mypy + ruff + black clean, CI, Docker, PyInstaller bundles per OS |

## CLI

```bash
agent-hub                                    # interactive chat — /help lists every slash command
agent-hub --prompt "what uses my disk?"       # one-shot run
agent-hub --prompt "…" --json                 # machine-readable result
agent-hub --prompt "…" --profile read_only    # restricted tool set
agent-hub --prompt "…" --dry-run              # show what *would* be called, never run it
agent-hub --serve --lan --print-token         # API + UI for phones and desktops
agent-hub --serve --desktop                   # native window (pywebview) or the default browser
agent-hub --doctor                            # environment, keys, tools, port and memory self-check
agent-hub --report                            # what it did, what it plans next, what it cost
agent-hub --remember "prefers concise Persian answers"   # write straight into long-term memory
```

Inside the chat: `/tools`, `/schema <tool>`, `/config`, `/safety`, `/model`, `/profile`,
`/cd`, `/history`, `/events`, `/transcript`, `/save`, `/load`, `/clear`, plus
`/memory [search|list|add|forget]`, `/report` and `/doctor`.

`--doctor` is the "why isn't this working" command: Python version, `.env` discovery,
model reachability (dry-run), tool imports, safety policy, port availability, memory file.

## Library

```python
import asyncio
from src.core.agent_factory import AgentFactory

async def main() -> None:
    agent = AgentFactory.create("developer")            # profile = tool set + limits
    result = await agent.ask("audit this repo and list the three riskiest files")
    print(result.text)
    print(result.tool_names, result.duration_ms, result.usage.total_tokens)
    await agent.close()

asyncio.run(main())
```

Every tool is a `BaseTool` subclass, every run is an event stream, every risky call goes
through the guard — see [`docs/architecture.md`](docs/architecture.md) and
[`docs/adding_tools.md`](docs/adding_tools.md).

## Apps and downloads

| Platform | How | Guide |
|---|---|---|
| Android | `agent-hub-android-debug.apk` (WebView shell, notifications, QR pairing) | [`apps/android/README.md`](apps/android/README.md) |
| iOS | SwiftUI app (Xcode + XcodeGen), or Safari → Add to Home Screen | [`apps/ios/README.md`](apps/ios/README.md) |
| Termux | agent *on* the phone: `scripts/install-termux.sh` | [`docs/apps.md`](docs/apps.md) |
| Windows / macOS / Linux | `agent-hub --serve --desktop`, or unsigned single-folder bundles | [`packaging/README.md`](packaging/README.md) |
| Docker | `docker compose up hub-server` | [`Dockerfile`](Dockerfile) |

Ready-made binaries for every OS are attached to each release:
<https://github.com/imankali/Ai_Tools/releases/latest>

The phone is only a remote control: the agent runs on your machine, and the model API key
is never sent to the phone or the browser — the app stores just the server URL and an access
token (Android Keystore / iOS Keychain).

## Configuration

Everything is environment-driven (`.env` via pydantic-settings) — see
[`.env.example`](.env.example) for the full annotated list.

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | – | model key (also `OPENAI_BASE_URL` for any OpenAI-compatible endpoint) |
| `MODEL_NAME` / `MODEL_FALLBACKS` | `gpt-6-astra` / ordered list | what to try, in which order |
| `ENABLE_SAFETY_GUARD` | `true` | the guard; disabling it is a deliberate act |
| `DANGEROUS_COMMAND_POLICY` | `confirm` | `confirm` · `deny` · `off` |
| `ALLOWED_DIRECTORIES` | `.` (project root) | where file tools may write |
| `UNRESTRICTED_FILESYSTEM` | `false` | `true` = whole disk, approvals still on |
| `MAX_COMMAND_TIMEOUT` / `MAX_TOOL_ITERATIONS` | `60` / `10` | run-away protection |
| `MEMORY_ENABLED` / `MEMORY_AUTO_CAPTURE` | `true` / `true` | long-term memory store |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8765` | app server bind |
| `SERVER_TOKEN` | – | required for any non-loopback bind |
| `SERVER_ALLOW_DIRECT_TOOLS` | `false` | allow `/api/tools/{name}/invoke` (admin panel) |
| `SERVER_APPROVAL_TIMEOUT` | `180` | seconds to wait for you, then deny |

## Safety model in one paragraph

The agent has your permissions, so the guardrail is not a sandbox — it is *evaluation +
consent + auditability*. `SafetyGuard` scores every command, path and URL (`safe → critical`),
blocks the unsurvivable (`rm -rf /`, `dd of=/dev/sda`, writing `/etc`, cloud metadata IPs),
and asks about the rest; asks expire as denials, never as approvals; every secret is masked
before it reaches a log, an error message, or your phone. Full write-up, threat model and
hardening checklist: [`docs/security.md`](docs/security.md) and
[`docs/autonomy.md`](docs/autonomy.md).

## Documentation

| Doc | Contents |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | layers, request lifecycle, extension points, design decisions |
| [`docs/server_api.md`](docs/server_api.md) | every REST endpoint, WebSocket protocol, error codes |
| [`docs/apps.md`](docs/apps.md) | pairing, Android/iOS/desktop/Termux/Docker, TLS, troubleshooting |
| [`docs/security.md`](docs/security.md) | guard internals, approvals, keystore, hardening |
| [`docs/autonomy.md`](docs/autonomy.md) | memory, planning, reports, self-extension, autonomy levels |
| [`docs/adding_tools.md`](docs/adding_tools.md) | write a tool in 20 minutes, test it, ship it |
| [`docs/api_reference.md`](docs/api_reference.md) | public classes and functions |
| [`examples/`](examples) | runnable scripts (`python examples/01_quickstart.py`) |

## Development

```bash
make install && make test      # pytest + coverage gate (fails under 80 %)
make lint                      # ruff + black --check + mypy
make fmt                       # black + ruff --fix
make serve                     # server on loopback
```

Tests never touch the network or a real LLM: `tests/fakes.py` provides a fake OpenAI client,
and tool tests monkeypatch every outbound call.

## Privacy

Local-first. Nothing phones home: no telemetry, no analytics, no CDN in the UI. Outbound
traffic is only what your tasks cause (the model endpoint, sites the browser/search tools
visit). Logs, memory and API-key profiles live in `~/.universal-agent-hub/` and can be wiped
with `rm -rf ~/.universal-agent-hub`.

## Disclaimer

This tool can run arbitrary commands on the machine it is installed on. Use it on machines
you own or are explicitly allowed to automate, keep `SERVER_TOKEN` secret, and leave the
safety guard on. You are responsible for what the agent does with your credentials, your
money, and the services you use — anything irreversible is designed to require your approval.

## License

MIT — see [LICENSE](LICENSE).

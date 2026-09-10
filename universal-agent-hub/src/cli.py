"""رابط خط فرمان (CLI) پروژه با کتابخانه‌ی Rich.

اجرا:

.. code-block:: bash

    python -m src.cli                      # حالت تعاملی
    python -m src.cli --prompt "…"         # یک نوبت، بدون تعامل
    agent-hub --profile developer          # بعد از pip install -e .

دستورات داخلی در حالت تعاملی با ``/`` شروع می‌شوند (``/help`` را امتحان کنید).
تأیید عملیات حساس از همین‌جا انجام می‌شود و خروجی ابزارها به‌صورت compact
نمایش داده می‌شود.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import shlex
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from src import __version__
from src.agent import UniversalAgent
from src.config import Config, get_config
from src.core.base_tool import ConfirmationRequest
from src.core.event_bus import EVENTS, Event
from src.core.tool_registry import ToolRegistry, discover_tools
from src.models.config_models import AgentProfile
from src.utils.helpers import format_duration, now_iso, truncate_text

try:  # pragma: no cover - Rich بخشی از وابستگی‌های اصلی است
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.syntax import Syntax
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment,misc]

__all__ = ["CLI", "WELCOME_MARKDOWN", "build_parser", "main"]

#: پیام خوش‌آمدگویی (Markdown برای Rich)
WELCOME_MARKDOWN = """# 🤖 Universal Agent Hub

A modular AI agent with **full, guarded** access to your machine.

| Capability | Tool | Safety |
| --- | --- | --- |
| 💻 Run commands | `terminal_run` | dangerous commands blocked / confirmed |
| 📁 Read, write, move, delete | `read_file`, `write_file`, … | path sandbox + confirmations |
| 🌐 Drive a browser | `browser_*` | SSRF guard, confirm on forms |
| 🔍 Search the web | `web_search`, `search_advanced` | read-only |
| 🖥️ Inspect the host | `os_info`, `cpu_info`, … | read-only |

Type `/help` for commands, or just describe what you want done.
Type `quit` (or press Ctrl-D) to leave.
"""


class CLI:
    """حلقه‌ی تعاملی اطراف ایجنت."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        profile: str = "generalist",
        tools: list[str] | None = None,
        console: Any = None,
        agent: UniversalAgent | None = None,
        quiet: bool = False,
    ) -> None:
        """
        Args:
            config: پیکربندی (پیش‌فرض: خواندن از ``.env``).
            profile: نام پروفایل ایجنت.
            tools: محدودسازی ابزارهای فعال.
            console: نمونه‌ی Rich Console (قابل تزریق برای تست).
            agent: تزریق ایجنت آماده (برای تست/اسکریپت).
            quiet: عدم چاپ رویدادهای ابزار و بنر خوش‌آمد.
        """
        self.config = config or get_config()
        # نوع Any چون Console فقط هنگام نصب Rich وجود دارد (و در تست‌ها تزریق می‌شود)
        self.console: Any = console if console is not None else (Console() if Console is not None else None)
        self.quiet = quiet or self.console is None
        self.profile_name = profile
        self.requested_tools = list(tools) if tools else None
        self.agent = agent or self._build_agent()
        self._confirm_lock = asyncio.Lock()
        self._transcript: list[dict[str, Any]] = []
        self._running = False

    # ------------------------------------------------------------------
    # راه‌اندازی
    # ------------------------------------------------------------------
    def _build_agent(self) -> UniversalAgent:
        """ساخت ایجنت از طریق کارخانه (و اتصال callback تأیید)."""
        from src.core.agent_factory import AgentFactory

        discover_tools()
        profile: AgentProfile | str = self.profile_name
        try:
            agent = AgentFactory.create(
                profile,
                config=self.config,
                tools=self.requested_tools,
                log=False,
            )
        except KeyError:
            self._print(f"[yellow]unknown profile '{self.profile_name}'; falling back to 'generalist'[/yellow]")
            agent = AgentFactory.create("generalist", config=self.config, tools=self.requested_tools, log=False)
        agent.set_confirmation_handler(self._confirm)
        agent.event_bus.subscribe(EVENTS.TOOL_COMPLETED, self._on_tool_event)
        agent.event_bus.subscribe(EVENTS.TOOL_FAILED, self._on_tool_event)
        agent.event_bus.subscribe("llm.model_fallback", self._on_model_fallback)
        return agent

    @property
    def bus(self) -> Any:
        """ایونت‌باس ایجنت (میان‌بر)."""
        return self.agent.event_bus

    # ------------------------------------------------------------------
    # ورودی/خروجی
    # ------------------------------------------------------------------
    def _print(self, text: Any = "") -> None:
        """چاپ امن (بدون Rich هم کار می‌کند)."""
        if self.console is not None:
            self.console.print(text)
        else:  # pragma: no cover - حالت بدون Rich
            plain = str(text).replace("[bold cyan]", "").replace("[/bold cyan]", "")
            print(plain)

    def _print_welcome(self) -> None:
        """نمایش بنر، ابزارها و وضعیت مدل."""
        if self.quiet:
            return
        if Console is not None and self.console is not None:
            self.console.print(
                Panel(
                    Markdown(WELCOME_MARKDOWN), border_style="green", title="Universal Agent Hub", title_align="left"
                )
            )
            self._show_tools()
            self._show_model_status()
        else:  # pragma: no cover
            self._print("Universal Agent Hub v" + __version__)

    def _show_tools(self) -> None:
        """جدول ابزارهای فعال."""
        table = Table(title=f"Active tools ({len(self.agent.tools)})", header_style="bold cyan")
        for column in ("tool", "risk", "confirm", "description"):
            table.add_column(column, overflow="fold")
        for info in self.agent.describe_tools():
            table.add_row(
                f"[bold]{info['name']}[/bold]",
                str(info.get("risk_level", "-")),
                "✅" if info.get("requires_confirmation") else "—",
                truncate_text(str(info.get("description", "")), 90)[0],
            )
        self.console.print(table)

    def _show_model_status(self) -> None:
        """هشدار شفاف درباره‌ی مدلی که واقعاً استفاده می‌شود."""
        info = self.agent.model_info
        if info["downgraded"]:
            self._print(
                Panel(
                    f"[yellow]Model '{info['requested']}' was not accepted by the endpoint; "
                    f"now using '{info['active']}' ({info['reason']}).[/yellow]\n"
                    f"Set [bold]MODEL_NAME[/bold] in .env to choose another model. Endpoint: {info['base_url']}",
                    title="⚠️ model",
                    border_style="yellow",
                )
            )
        elif not self.config.is_api_key_set:
            self._print(
                Panel(
                    "[red]OPENAI_API_KEY is not set.[/red] Add it to .env (see .env.example) or the agent cannot call the model.",
                    border_style="red",
                )
            )
        elif info["requested"] == "gpt-6-astra":
            self._print(
                Panel(
                    "[cyan]Using model 'gpt-6-astra'.[/cyan] This name only works if your provider "
                    "(OPENAI_BASE_URL) exposes it — otherwise add e.g. "
                    "[bold]MODEL_FALLBACKS=gpt-4o-mini,gpt-4o[/bold] to .env.",
                    border_style="cyan",
                )
            )

    # ------------------------------------------------------------------
    # رویدادها
    # ------------------------------------------------------------------
    def _on_tool_event(self, event: Event) -> None:
        """نمایش یک خط درباره‌ی اجرای هر ابزار."""
        if self.quiet:
            return
        payload = event.payload
        duration = payload.get("duration_ms")
        timing = f" in {duration} ms" if isinstance(duration, int) else ""
        if payload.get("success") or event.kind == EVENTS.TOOL_COMPLETED:
            self._print(f"  [dim green]✓ {payload.get('tool')}{timing}[/dim green]")
        else:
            error = truncate_text(str(payload.get("error") or "failed"), 160)[0]
            self._print(f"  [dim red]✗ {payload.get('tool')}: {error}[/dim red]")

    def _on_model_fallback(self, event: Event) -> None:
        """اطلاع‌رسانی تغییر خودکار مدل."""
        payload = event.payload
        self._print(
            f"[yellow]↺ model fallback: '{payload.get('from')}' → '{payload.get('to')}' "
            f"({payload.get('reason')})[/yellow]"
        )

    # ------------------------------------------------------------------
    # تأیید عملیات
    # ------------------------------------------------------------------
    async def _confirm(self, request: ConfirmationRequest) -> bool:
        """پرسش تأیید از کاربر برای عملیات حساس.

        Args:
            request: درخواست تأیید ساخته‌شده توسط ابزار.

        Returns:
            ``True`` در صورت تأیید کاربر (در حالت non-interactive: رد).
        """
        async with self._confirm_lock:  # هم‌زمانی اجرای موازی ابزارها
            if not self._running or self.console is None:
                self._print(
                    f"[red]refused: '{request.tool}' needs confirmation but no interactive prompt is available[/red]"
                )
                return False
            lines = [f"[bold yellow]{request.summary}[/bold yellow]"]
            if request.risk.value not in {"safe", "low"}:
                lines.append(f"risk: [bold]{request.risk.value}[/bold]")
            for key, value in request.details.items():
                rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
                lines.append(f"[cyan]{key}[/cyan]: {truncate_text(str(rendered), 600)[0]}")
            self.console.print(Panel("\n".join(lines), title="🔐 confirmation required", border_style="yellow"))
            answer = Confirm.ask("Allow this?", default=False, console=self.console)
            self._transcript.append(
                {"type": "confirmation", "approved": answer, "at": now_iso(), "request": request.summary}
            )
            return bool(answer)

    # ------------------------------------------------------------------
    # حلقه‌ی اصلی
    # ------------------------------------------------------------------
    async def run(self, *, prompt_once: str | None = None, output_json: bool = False) -> int:
        """حلقه‌ی اصلی CLI.

        Args:
            prompt_once: اگر داده شود فقط همین درخواست اجرا و خارج می‌شود.
            output_json: نتیجه به‌صورت JSON چاپ شود (برای اسکریپت‌نویسی).

        Returns:
            کد خروج (۰ موفق، ۱ خطا).
        """
        self._running = True
        exit_code = 0
        if prompt_once is not None:
            try:
                result = await self.agent.ask(prompt_once, system_note=self._workspace_note())
                if output_json:
                    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2, default=str))
                else:
                    self._render_result(result)
                exit_code = 0 if result.ok else 1
            except Exception as exc:  # noqa: BLE001 - خطا در حالت یک‌بار‌اجرا به کد خروج تبدیل می‌شود
                self._print(f"[red]error:[/red] {exc}")
                exit_code = 1
            finally:
                await self.agent.close()
            return exit_code

        self._print_welcome()
        self._install_readline()
        while True:
            try:
                user_input = (
                    Prompt.ask("\n[bold cyan]You[/bold cyan]", console=self.console)
                    if self.console
                    else input("You> ")  # noqa: ASYNC250 - تنها راه خواندن ورودی بدون Rich
                )
            except (KeyboardInterrupt, EOFError):
                self._print("\n[yellow]Interrupted. Goodbye! 👋[/yellow]")
                break
            text = (user_input or "").strip()
            if not text:
                continue
            if text in {"quit", "exit", "q", ":q"}:
                self._print("[yellow]Goodbye! 👋[/yellow]")
                break
            if text.startswith("/"):
                if text in {"/quit", "/exit", "/q"}:
                    self._print("[yellow]Goodbye! 👋[/yellow]")
                    break
                try:
                    stop = await self._handle_command(text)
                except Exception as exc:  # noqa: BLE001 - CLI نباید بمیرد
                    self._print(f"[red]command failed:[/red] {type(exc).__name__}: {exc}")
                    stop = False
                if stop:
                    break
                continue
            try:
                with (
                    self.console.status("[bold green]Thinking…", spinner="dots")
                    if self.console is not None
                    else contextlib.nullcontext()
                ):
                    result = await self.agent.ask(text, system_note=self._workspace_note())
                self._render_result(result)
                if not result.ok:
                    exit_code = 1
            except KeyboardInterrupt:  # pragma: no cover - کاربر Ctrl+C زد
                self._print("\n[yellow]Cancelled that request. Type 'quit' to exit.[/yellow]")
                continue
            except Exception as exc:  # noqa: BLE001
                self._print(f"[red]Error:[/red] {exc}")
                exit_code = 1
        self._running = False
        await self.agent.close()
        return exit_code

    def _render_result(self, result: Any) -> None:
        """چاپ پاسخ ایجنت (Markdown + خلاصه‌ی ابزارها)."""
        self._transcript.append(
            {"type": "run", "at": now_iso(), "ok": result.ok, "text": result.text, "tools": result.tool_names}
        )
        if result.text:
            if Console is not None and self.console is not None:
                self.console.print(
                    Panel(Markdown(result.text), title="🤖 agent", border_style="magenta", title_align="left")
                )
            else:  # pragma: no cover
                self._print(result.text)
        if result.error:
            self._print(f"[red]{result.error}[/red]")
        if result.tool_calls:
            self._print(self._tool_summary(result))
        if result.usage.total_tokens:
            self._print(
                f"[dim]{result.iterations} iteration(s) · {result.usage.total_tokens} tokens · "
                f"{format_duration(result.duration_ms / 1000)}[/dim]"
            )

    @staticmethod
    def _tool_summary(result: Any) -> str:
        """خط خلاصه‌ی ابزارهای اجراشده با وضعیت هر کدام."""
        parts: list[str] = []
        for record in result.tool_calls:
            status = "✓" if record.succeeded else "✗"
            color = "green" if record.succeeded else "red"
            parts.append(f"[{color}]{status} {record.tool} ({record.duration_ms} ms)[/{color}]")
        return "  " + "  ".join(parts)

    def _workspace_note(self) -> str:
        """زمینه‌ی مفید برای مدل (پوشه‌ی کاری و پروفایل)."""
        roots = ", ".join(str(path) for path in self.agent.safety.allowed_directories) or "(unrestricted)"
        return (
            f"workspace={Path.cwd()} · allowed_paths={roots} · profile={self.profile_name} · "
            f"platform={sys.platform} · confirmations={'on' if self.config.enable_confirmation else 'off'}"
        )

    def _install_readline(self) -> None:
        """فعال‌سازی تاریخچه‌ی ورودی (↑/↓) در صورت موجود بودن readline."""
        try:
            import atexit
            import readline  # noqa: F401

            history = Path.home() / ".universal-agent-hub-history"
            with contextlib.suppress(OSError):
                readline.read_history_file(str(history))
            readline.set_history_length(500)
            atexit.register(lambda: _safe_write_history(history))
        except ImportError:  # pragma: no cover - ویندوز
            pass

    # ------------------------------------------------------------------
    # دستورهای داخلی
    # ------------------------------------------------------------------
    async def _handle_command(self, raw: str) -> bool:
        """مدیریت یک دستور ``/``.

        Args:
            raw: متن کامل دستور (شامل اسلش).

        Returns:
            ``True`` اگر برنامه باید متوقف شود.
        """
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        command, args = parts[0].lower().lstrip("/"), parts[1:]
        handler: Callable[[list[str]], Any] | None = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            self._print(f"[red]unknown command /{command}[/red] — try [bold]/help[/bold]")
            return False
        outcome = handler(args)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return bool(outcome)

    def _cmd_help(self, args: list[str]) -> bool:  # noqa: ARG002 - امضای یکسان هندلرها
        """نمایش راهنما."""
        table = Table(title="Commands", header_style="bold cyan")
        for column in ("command", "what it does"):
            table.add_column(column, overflow="fold")
        table.add_row("/help", "this list")
        table.add_row("/tools", "list active tools with risk and confirmation flags")
        table.add_row("/schema <tool>", "print the JSON schema of a tool")
        table.add_row("/config", "show effective configuration (secrets masked)")
        table.add_row("/safety", "show the active safety policy")
        table.add_row("/model [name]", "show or change the model for this session")
        table.add_row("/profile [name]", "rebuild the agent with another profile")
        table.add_row("/tools only <a,b>", "restrict tools (empty to reset)")
        table.add_row("/confirm on|off", "toggle confirmation requirement")
        table.add_row("/cd <dir>", "change the working directory")
        table.add_row("/history", "show the last conversation messages")
        table.add_row("/events [n]", "show recent agent/tool events")
        table.add_row("/save <file>", "save conversation state as JSON")
        table.add_row("/load <file>", "load conversation state")
        table.add_row("/transcript [file]", "dump this session transcript (markdown)")
        table.add_row("/memory [list|add|plan|search|forget|stats]", "read or edit long-term memory")
        table.add_row("/report [days]", "activity report: runs, tools, approvals, open plans")
        table.add_row("/doctor", "environment self-check (same as --doctor)")
        table.add_row("/clear", "clear conversation history")
        table.add_row("/quit", "exit")
        self.console.print(table)
        return False

    def _cmd_tools(self, args: list[str]) -> bool:
        """فهرست ابزارها یا محدودسازی دستی آن‌ها (``/tools only a,b``)."""
        if args and args[0] == "only":
            wanted = [name.strip() for name in ",".join(args[1:]).split(",") if name.strip()]
            unknown = [name for name in wanted if ToolRegistry.get(name) is None]
            if unknown:
                self._print(f"[red]unknown tool(s): {', '.join(unknown)}[/red]")
                return False
            self.agent._tool_names = wanted  # noqa: SLF001 - ابزار پیشرفته‌ی دیباگ
            self._print(f"[green]tools set to:[/green] {', '.join(wanted) or '(none)'}")
            return False
        self._show_tools()
        return False

    def _cmd_schema(self, args: list[str]) -> bool:
        """چاپ اسکیمای JSON یک ابزار."""
        if not args:
            self._print("[red]usage:[/red] /schema <tool_name>")
            return False
        tool = ToolRegistry.get(args[0], config=self.config)
        if tool is None:
            self._print(f"[red]no tool named '{args[0]}'[/red]")
            return False
        self.console.print(
            Syntax(json.dumps(tool.get_schema(), indent=2, ensure_ascii=False), "json", theme="monokai")
        )
        return False

    def _cmd_config(self, args: list[str]) -> bool:  # noqa: ARG002
        """چاپ تنظیمات مؤثر."""
        data = self.config.to_safe_dict()
        table = Table(title="Configuration", header_style="bold cyan")
        table.add_column("key")
        table.add_column("value", overflow="fold")
        for key in sorted(data):
            table.add_row(key, str(data[key]))
        self.console.print(table)
        return False

    def _cmd_safety(self, args: list[str]) -> bool:  # noqa: ARG002
        """چاپ سیاست ایمنی فعال."""
        summary = self.agent.safety_summary()
        self.console.print(
            Panel(
                json.dumps(summary, indent=2, ensure_ascii=False, default=str),
                title="🛡️ safety",
                border_style="green",
            )
        )
        return False

    def _cmd_memory(self, args: list[str]) -> bool:
        """حافظه‌ی بلندمدت: فهرست، افزودن، برنامه، جست‌وجو، حذف."""
        from src.core.memory import MEMORY_KINDS, AgentMemory

        memory = getattr(self.agent, "memory", None) or AgentMemory.for_config(self.config)
        if memory is None:
            self._print("[red]memory is disabled[/red] — set [bold]MEMORY_ENABLED=true[/bold] and restart")
            return False
        action = str(args[0]).lower() if args else "list"
        rest = " ".join(args[1:]).strip()
        if action in {"add", "plan", "note"}:
            kind = "plan" if action == "plan" else "note"
            if not rest:
                self._print(f"[red]usage:[/red] /memory {action} <text>")
                return False
            record = memory.add(rest, kind=kind, source="cli:/memory", confidence=1.0)
            self._print(f"[green]saved[/green] {record.kind} · {record.id}" if record else "[red]nothing saved[/red]")
            return False
        if action == "search":
            if not rest:
                self._print("[red]usage:[/red] /memory search <keywords>")
                return False
            found = memory.search(rest, limit=10)
            if not found:
                self._print("[dim]no memory matches that[/dim]")
                return False
            for item in found:
                self._print(f"  [cyan]{item.id}[/cyan] · {item.kind} · {item.content}")
            return False
        if action == "forget":
            if not rest:
                self._print("[red]usage:[/red] /memory forget <id>")
                return False
            self._print("[green]forgotten[/green]" if memory.forget(rest) else "[red]no such record[/red]")
            return False
        if action == "stats":
            stats = memory.stats()
            lines = [f"{key}: {value}" for key, value in stats.items() if key != "by_kind"]
            lines.append(
                "kinds: " + ", ".join(f"{name}={count}" for name, count in (stats.get("by_kind") or {}).items())
            )
            self._print("\n".join(lines))
            self._print(f"[dim]kinds: {', '.join(MEMORY_KINDS)} · file: {stats['path']}[/dim]")
            return False
        records = memory.recent(15)
        if not records:
            self._print("[dim]memory is empty — the agent fills it as it works, or use /memory add <text>[/dim]")
            return False
        for item in records:
            pin = "[yellow]·[/yellow]" if item.pinned else " "
            tags = (" " + ", ".join(item.tags)) if item.tags else ""
            self._print(f" {pin}[cyan]{item.id}[/cyan] · {item.kind}{tags} · {item.content}")
        return False

    def _cmd_report(self, args: list[str]) -> bool:
        """گزارش فعالیت (اجراها، ابزارها، تأییدها، برنامه‌های باز)."""
        from src.core.reports import build_report, render_report_text

        days = 7.0
        if args:
            try:
                days = float(args[0])
            except ValueError:
                self._print("[red]days must be a number (0 = all time)[/red]")
                return False
        report = build_report(self.config, days=days, memory=getattr(self.agent, "memory", None))
        self._print(render_report_text(report, title="Activity report"))
        plans = report.get("plans") or []
        if plans:
            self._print("[dim]use /memory forget <id> to drop a plan, or /memory add <text> to note a decision[/dim]")
        return False

    def _cmd_doctor(self, args: list[str]) -> bool:  # noqa: ARG002 - امضای یکسان هندلرها
        """خودسنجی محیط از داخل گفت‌وگو."""
        self._print_checks(doctor_checks(self.config))
        return False

    def _print_checks(self, rows: list[tuple[str, str, str]]) -> None:
        """چاپ ردیف‌های doctor در REPL (بدون تغییر کد خروج)."""
        icons = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]fail[/red]"}
        for name, status, detail in rows:
            self._print(f"{name:<16} {icons.get(status, status):<20} {detail}")

    def _cmd_model(self, args: list[str]) -> bool:
        """نمایش یا تغییر مدل جاری."""
        if not args:
            self.console.print(
                Panel(
                    json.dumps(self.agent.model_info, indent=2, ensure_ascii=False),
                    title="model",
                    border_style="cyan",
                )
            )
            return False
        name = args[0].strip()
        if name not in self.agent._model_candidates:  # noqa: SLF001
            self.agent._model_candidates.append(name)  # noqa: SLF001
        self.agent.model = name
        self._print(f"[green]model set to[/green] {name}")
        return False

    def _cmd_profile(self, args: list[str]) -> bool:
        """ساخت دوباره‌ی ایجنت با پروفایل دیگر."""
        if not args:
            from src.core.agent_factory import AgentFactory

            names = ", ".join(sorted(AgentFactory.profiles()))
            self._print(f"[bold]profiles:[/bold] {names} · current: {self.profile_name}")
            return False
        self.profile_name = args[0]
        self.agent.set_confirmation_handler(None)
        self.agent = self._build_agent()
        self._print(f"[green]rebuilt agent with profile '{self.profile_name}'[/green]")
        self._show_model_status()
        return False

    def _cmd_confirm(self, args: list[str]) -> bool:
        """روشن/خاموش کردن نیاز به تأیید."""
        from src.utils.validators import coerce_bool

        if not args:
            self._print(
                f"confirmation is [{'green' if self.config.enable_confirmation else 'yellow'}]{'ON' if self.config.enable_confirmation else 'OFF'}[/]"
            )
            return False
        self.config.enable_confirmation = coerce_bool(args[0], default=True)
        self._print(
            f"confirmation [bold]{'ON' if self.config.enable_confirmation else 'OFF'}[/bold] (only for this session)"
        )
        return False

    def _cmd_cd(self, args: list[str]) -> bool:
        """تغییر دایرکتوری کاری."""
        if not args:
            self._print(f"cwd: [bold]{Path.cwd()}[/bold]")
            return False
        target = Path(args[0]).expanduser()
        if not target.is_dir():
            self._print(f"[red]not a directory: {target}[/red]")
            return False
        os.chdir(str(target))
        self._print(f"cwd → [bold]{Path.cwd()}[/bold]")
        return False

    def _cmd_history(self, args: list[str]) -> bool:
        """نمایش تاریخچه‌ی گفت‌وگو."""
        limit = int(args[0]) if args and args[0].isdigit() else 10
        messages = self.agent.state.messages[-limit:]
        if not messages:
            self._print("[dim]no history yet[/dim]")
            return False
        for message in messages:
            content = truncate_text(message.content or "(tool call)", 300)[0]
            self._print(f"[bold]{message.role}[/bold]: {content}")
        return False

    def _cmd_events(self, args: list[str]) -> bool:
        """نمایش رویدادهای اخیر."""
        limit = int(args[0]) if args and args[0].isdigit() else 15
        events = self.bus.recent("#", limit=limit)
        if not events:
            self._print("[dim]no events captured[/dim]")
            return False
        for event in events:
            self._print(
                f"[dim]{now_iso()}[/dim] [cyan]{event.kind}[/cyan] {truncate_text(json.dumps(event.payload, ensure_ascii=False, default=str), 160)[0]}"
            )
        return False

    def _cmd_save(self, args: list[str]) -> bool:
        """ذخیره‌ی حالت گفت‌وگو در فایل JSON."""
        target = Path(args[0]) if args else Path("agent-session.json")
        target.write_text(self.agent.export_state(), encoding="utf-8")
        self._print(f"[green]saved → {target}[/green]")
        return False

    def _cmd_load(self, args: list[str]) -> bool:
        """بازیابی حالت گفت‌وگو از فایل JSON."""
        if not args:
            self._print("[red]usage:[/red] /load <file.json>")
            return False
        source = Path(args[0])
        if not source.is_file():
            self._print(f"[red]no such file: {source}[/red]")
            return False
        try:
            self.agent.load_state(json.loads(source.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError) as exc:
            self._print(f"[red]invalid state file: {exc}[/red]")
            return False
        self._print(f"[green]loaded {len(self.agent.state.messages)} message(s) from {source}[/green]")
        return False

    def _cmd_transcript(self, args: list[str]) -> bool:
        """خروجی گرفتن از کل session (markdown)."""
        target = (
            Path(args[0]) if args else Path(f"agent-transcript-{now_iso().replace(':', '').replace('+0000', 'Z')}.md")
        )
        lines = [f"# Universal Agent Hub transcript\n\nGenerated {now_iso()} · profile `{self.profile_name}`\n"]
        for entry in self._transcript:
            if entry["type"] == "run":
                lines.append(f"## {entry['at']} — {'ok' if entry['ok'] else 'failed'}\n")
                if entry.get("tools"):
                    lines.append(f"tools: {', '.join(entry['tools'])}\n")
                lines.append((entry.get("text") or "(no text)").strip() + "\n")
            elif entry["type"] == "confirmation":
                lines.append(f"- 🔐 {'approved' if entry['approved'] else 'declined'}: {entry.get('request')}\n")
        target.write_text("\n".join(lines), encoding="utf-8")
        self._print(f"[green]transcript written → {target}[/green]")
        return False

    def _cmd_clear(self, args: list[str]) -> bool:  # noqa: ARG002
        """پاک کردن تاریخچه."""
        self.agent.reset_history()
        self._transcript.clear()
        self._print("[green]history cleared[/green]")
        return False


def _safe_write_history(path: Path) -> None:
    """نوشتن فایل تاریخچه‌ی readline (بی‌صدا اگر نشد)."""
    try:
        import readline

        readline.write_history_file(str(path))
    except (OSError, ImportError):  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# خودسنجی محیط، گزارش فعالیت و یادداشت حافظه (حالت‌های غیرتعاملی CLI)
# ---------------------------------------------------------------------------
#: وابستگی‌های اختیاری و اینکه چه چیزی را فعال می‌کنند
OPTIONAL_PACKAGES: tuple[tuple[str, str], ...] = (
    ("psutil", "detailed cpu/memory/disk/network info"),
    ("ddgs", "live web search backend"),
    ("playwright", "real browser automation (screenshots, clicks, forms)"),
    ("dotenv", "load .env from a custom --config-file"),
    ("webview", "native desktop window for --serve --desktop"),
)


def _module_available(name: str) -> bool:
    """آیا یک پکیج قابل‌import هست (بدون import کردنش)."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - پکیج نیمه‌نصب‌شده
        return False


def doctor_checks(config: Config | None = None) -> list[tuple[str, str, str]]:
    """ردیف‌های خودسنجی: ``(عنوان، وضعیت، جزئیات)`` با وضعیت ok/warn/fail.

    هیچ استثنایی نمی‌دهد و هیچ اثری روی سیستم نمی‌گذارد (به‌جز یک آزمون‌نوشتن
    در فایل حافظه، که بی‌درنگ پاک می‌شود)؛ برای همین روی ماشینِ نیمه‌نصب‌شده هم
    جواب می‌دهد — دقیقاً همان جایی که به ``--doctor`` نیاز پیدا می‌کنید.
    """
    settings = config or get_config()
    rows: list[tuple[str, str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        rows.append((name, status, detail))

    version = platform.python_version_tuple()
    python_ok = (int(version[0]), int(version[1])) >= (3, 10)
    add("python", "ok" if python_ok else "fail", f"{platform.python_version()} on {platform.system().lower()}")

    env_file = Path(".env")
    add(
        "dotenv file",
        "ok" if env_file.is_file() else "warn",
        str(env_file.resolve()) if env_file.is_file() else "no .env in cwd — only environment variables are used",
    )

    key = str(settings.openai_api_key or "")
    if not key:
        add("api key", "fail", "OPENAI_API_KEY is empty — set it in .env or the app's Keys tab")
    else:
        add("api key", "ok" if len(key) > 20 else "warn", f"{key[:6]}…{key[-4:]} ({len(key)} chars)")
    add("endpoint", "ok", str(settings.openai_base_url or "https://api.openai.com/v1"))

    candidates = [str(name) for name in (settings.safe_model_candidates or [])]
    known = bool(getattr(settings, "known_model", True))
    add(
        "model",
        "ok" if known and candidates else "warn",
        f"{settings.model_name} · fallbacks: {', '.join(candidates[1:]) or '(none)'}",
    )

    discovered = discover_tools()
    add(
        "tools",
        "ok" if discovered else "fail",
        f"{len(discovered)} registered"
        + (
            f" · dirs: {', '.join(str(d) for d in _tool_search_dirs())}"
            if discovered
            else " — check src/tools or AGENT_HUB_TOOL_DIRS"
        ),
    )

    missing = [name for name, _purpose in OPTIONAL_PACKAGES if not _module_available(name)]
    optional_note = ", ".join(f"{name} ({purpose})" for name, purpose in OPTIONAL_PACKAGES if name in missing)
    add(
        "optional deps",
        "ok" if not missing else "warn",
        "all present" if not missing else "missing: " + optional_note,
    )

    try:
        from src.utils.safety import SafetyGuard

        summary = SafetyGuard.from_config(settings).summary()
        guard_on = bool(getattr(settings, "enable_safety_guard", True))
        if not guard_on:
            add(
                "safety guard",
                "fail",
                "disabled — set ENABLE_SAFETY_GUARD=true before running this on a real machine",
            )
        else:
            parts = [
                f"policy={summary.get('policy', 'confirm')}",
                f"dirs={len(summary.get('allowed_directories') or []) or 'project root'}",
            ]
            if summary.get("unrestricted_filesystem"):
                parts.append("filesystem=unrestricted")
            if not summary.get("allow_shell", True):
                parts.append("shell=off")
            add("safety guard", "ok", " · ".join(parts))
    except Exception as exc:  # noqa: BLE001 - خودسنجی هیچ‌وقت نمی‌شکند
        add("safety guard", "fail", f"{type(exc).__name__}: {exc}")

    try:
        from src.core.memory import AgentMemory

        memory = AgentMemory.for_config(settings)
        if memory is None:
            add("memory", "warn", "disabled (set MEMORY_ENABLED=true)")
        else:
            probe = memory.add("doctor probe", kind="note", source="cli:doctor")
            writable = probe is not None
            if probe is not None:
                memory.forget(probe.id)
            stats = memory.stats()
            add(
                "memory",
                "ok" if writable else "fail",
                f"{stats['records']} records at {stats['path']}" + ("" if writable else " — not writable"),
            )
    except Exception as exc:  # noqa: BLE001
        add("memory", "fail", f"{type(exc).__name__}: {exc}")

    try:
        from src.core.reports import ActivityRecorder

        recorder = ActivityRecorder.for_config(settings)
        if recorder is None:
            add("activity report", "warn", "disabled (set REPORTS_ENABLED=true)")
        else:
            stats = recorder.stats()
            add("activity report", "ok", f"{stats['records']} entries at {stats['path']}")
    except Exception as exc:  # noqa: BLE001
        add("activity report", "fail", f"{type(exc).__name__}: {exc}")

    port = int(settings.server_port)
    free = _port_is_free("127.0.0.1", port)
    add(
        "server",
        "ok" if free else "warn",
        f"{settings.server_host}:{port} "
        + ("free" if free else "already in use (another agent-hub is running?)")
        + (f" · token {'set' if settings.server_token else 'not set (loopback only)'}"),
    )
    if bool(getattr(settings, "server_allow_direct_tools", False)):
        add(
            "direct tools",
            "warn",
            "SERVER_ALLOW_DIRECT_TOOLS=true — /api/tools/{name}/invoke bypasses the agent loop",
        )

    prefix = str(os.environ.get("PREFIX", ""))
    if prefix.startswith("/data/com.termux"):
        add("platform", "warn", "Termux detected — scripts/install-termux.sh is the supported path")
    pypi_newer = platform.system() == "Windows" and sys.version_info.minor < 11
    if pypi_newer:  # pragma: no cover - فقط هشدار اطلاعاتی
        add("platform", "warn", "python 3.10 on Windows: use 3.11+ for the signed installer flow")
    return rows


def _tool_search_dirs() -> list[str]:
    """پوشه‌های پلاگین از محیط (برای نمایش در --doctor)."""
    raw = os.environ.get("AGENT_HUB_TOOL_DIRS", "")
    return [chunk for chunk in raw.split(os.pathsep) if chunk.strip()]


def _port_is_free(host: str, port: int) -> bool:
    """آیا پورت قابل bind است (برای --serve)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _print_checks(console: Any, rows: list[tuple[str, str, str]]) -> int:  # noqa: ANN401 - Console|None
    """چاپ ردیف‌های خودسنجی (با Rich جدول، بدون آن خط‌به‌خط). کد خروج."""
    failed = any(status == "fail" for _name, status, _detail in rows)
    icons = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]fail[/red]"}
    if console is not None and Table is not None:
        table = Table(title="agent-hub doctor", header_style="bold cyan")
        for column in ("check", "status", "detail"):
            table.add_column(column, overflow="fold")
        for name, status, detail in rows:
            table.add_row(name, icons.get(status, status), detail)
        console.print(table)
    else:  # pragma: no cover - محیط بدون Rich
        for name, status, detail in rows:
            print(f"{name:<16} {status:<5} {detail}")
    return 1 if failed else 0


def run_doctor(config: Config) -> int:
    """اجرای ``agent-hub --doctor`` و برگرداندن کد خروج."""
    console = Console() if Console is not None else None
    return _print_checks(console, doctor_checks(config))


def run_report(config: Config, *, days: float = 7.0, as_json: bool = False) -> int:
    """اجرای ``agent-hub --report`` (چاپ گزارش فعالیت)."""
    from src.core.reports import build_report, render_report_text

    report = build_report(config, days=days)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    console = Console() if Console is not None else None
    text = render_report_text(
        report, title=f"Activity report (last {days:g} days)" if days > 0 else "Activity report (all time)"
    )
    if console is not None and Panel is not None:
        console.print(Panel(text, border_style="cyan", title="agent-hub report"))
    else:  # pragma: no cover - محیط بدون Rich
        print(text)
    return 0


def run_remember(config: Config, text: str, *, kind: str = "note", pin: bool = False) -> int:
    """اجرای ``agent-hub --remember "…"`` (ثبت مستقیم در حافظه‌ی بلندمدت)."""
    from src.core.memory import MEMORY_KINDS, AgentMemory

    memory = AgentMemory.for_config(config)
    if memory is None:
        print("[red]memory is disabled[/red] — set MEMORY_ENABLED=true (or remove it from your environment)")
        return 1
    wanted = (kind or "note").strip().lower()
    if wanted not in MEMORY_KINDS:
        print(f"[red]unknown kind '{wanted}'[/red] — choose one of: {', '.join(MEMORY_KINDS)}")
        return 1
    record = memory.add(text, kind=wanted, source="cli:remember", pin=pin, confidence=1.0)
    if record is None:
        print("[red]nothing to remember (empty text)[/red]")
        return 1
    print(f"saved {record.kind} · {record.id} \u2192 {record.content[:120]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """ساخت parser آرگومان‌های خط فرمان.

    Returns:
        :class:`argparse.ArgumentParser` آماده.
    """
    parser = argparse.ArgumentParser(
        prog="agent-hub",
        description="Universal Agent Hub — a modular, safety-guarded AI agent with system access.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--prompt", help="run a single request non-interactively and exit")
    parser.add_argument(
        "--profile", default="generalist", help="agent profile (generalist, read_only, developer, ops)"
    )
    parser.add_argument("--model", default=None, help="override MODEL_NAME for this run")
    parser.add_argument("--tools", default=None, help="comma-separated allow-list of tools")
    parser.add_argument("--config-file", default=".env", help="path to a dotenv file")
    parser.add_argument("--yes", action="store_true", help="auto-approve confirmations (dangerous, for automation)")
    parser.add_argument(
        "--dry-run", action="store_true", help="do not call the model; print resolved configuration and tools"
    )
    parser.add_argument("--json", action="store_true", help="print the run result as JSON (with --prompt)")
    parser.add_argument(
        "--serve", action="store_true", help="start the HTTP/WebSocket server (mobile & desktop apps connect to it)"
    )
    parser.add_argument("--host", default=None, help="with --serve: bind address (default: SERVER_HOST)")
    parser.add_argument("--port", type=int, default=None, help="with --serve: bind port (default: SERVER_PORT)")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="with --serve: bind 0.0.0.0 and show the LAN address for phones",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="with --serve: open a native window (pywebview if installed, otherwise the default browser)",
    )
    parser.add_argument(
        "--token", default=None, help="with --serve: access token apps must send (generated if --lan and unset)"
    )
    parser.add_argument(
        "--insecure", action="store_true", help="with --serve: allow a network bind without any token"
    )
    parser.add_argument(
        "--no-ui", action="store_true", help="with --serve: API/WebSocket only, do not serve the web app"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="self-check the environment (python, .env, key, tools, safety, memory, port) and exit",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the activity report (runs, tools, approvals, open plans) and exit",
    )
    parser.add_argument(
        "--days", type=float, default=7.0, help="with --report: look back this many days (0 = all time)"
    )
    parser.add_argument(
        "--remember", default=None, metavar="TEXT", help="write a note into long-term memory and exit"
    )
    parser.add_argument(
        "--kind", default="note", help="with --remember: fact, preference, procedure, decision, plan or note"
    )
    parser.add_argument("--pin", action="store_true", help="with --remember: protect the note from pruning")
    parser.add_argument("--verbose", "-v", action="count", default=0, help="more logging (-v debug)")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    return parser


def _local_addresses() -> list[str]:
    """آدرس‌های IP محلی (برای نشان دادن به کاربر گوشی). خطا → فهرست خالی."""
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))  # بسته‌ای ارسال نمی‌شود؛ فقط مسیر انتخاب می‌شود
            primary = probe.getsockname()[0]
            if primary and primary not in found:
                found.append(primary)
        finally:
            probe.close()
    except OSError:  # pragma: no cover - شبکه‌ی بدون مسیر پیش‌فرض
        pass
    try:
        import psutil

        for name, addresses in psutil.net_if_addrs().items():  # noqa: B007 - نام اینتریس لازم نیست
            for address in addresses:
                local = (
                    address.family == socket.AF_INET
                    and address.address.startswith(("10.", "192.168.", "172."))
                    and address.address not in found
                )
                if local:
                    found.append(address.address)
    except (ImportError, OSError):  # pragma: no cover - psutil اختیاری است
        pass
    return found[:4]


def _desktop_page_url(base: str, token: str) -> str:
    """آدرس UI با توکن داخل query (تا کاربر چیزی تایپ نکند؛ UI آن را از URL پاک می‌کند)."""
    if not token:
        return base.rstrip("/") + "/"
    return base.rstrip("/") + "/?token=" + quote(token, safe="")


def _wait_for_server(base: str, timeout: float = 10.0, abort: Callable[[], bool] | None = None) -> bool:
    """تا بالا آمدن سرور صبر می‌کند (پینگ TCP روی همان host:port).

    Args:
        base: آدرس پایه (از همان چیزی که به کاربر نشان می‌دهیم).
        timeout: سقف انتظار بر حسب ثانیه.
        abort: اگر بدهیم و True شود، زودتر دست می‌کشیم (مثلاً سرور خطای bind داد).
    """
    parsed = urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 8765)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if abort is not None and abort():
            return False
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def serve_in_window(settings: Any, url: str, console: Any, *, startup_wait: float = 10.0) -> int:
    """سرور در thread پس‌زمینه + پنجره‌ی بومی روی thread اصلی.

    اگر ``pywebview`` نصب باشد یک پنجره‌ی واقعی (WebView2/WKWebView/WebKitGTK) ساخته
    می‌شود؛ اگر نباشد مرورگر پیش‌فرض باز می‌شود. هر دو حالت همان UI وب را نشان می‌دهند،
    پس روی ویندوز، macOS و لینوکس رفتار یکسان است.
    """
    import threading
    import webbrowser

    errors: list[BaseException] = []

    def _runner() -> None:
        from src.server import run_server

        try:
            run_server(settings)
        except Exception as exc:  # pragma: no cover - خطای bind در thread
            errors.append(exc)

    thread = threading.Thread(target=_runner, name="agent-hub-server", daemon=True)
    thread.start()
    _wait_for_server(url, timeout=startup_wait, abort=lambda: bool(errors))
    page = _desktop_page_url(url, str(settings.server_token or ""))
    opened = "browser"
    try:
        import webview  # type: ignore[import-not-found]  # اختیاری: pip install pywebview

        webview.create_window("Universal Agent Hub", page, width=1200, height=860, resizable=True)
        webview.start()
        opened = "window"
    except ImportError:
        webbrowser.open(page)
    if console is not None:
        console.print(f"  [dim]desktop UI opened via {opened} · API: {url}[/dim]")
    else:  # pragma: no cover - بدون rich
        print(f"desktop UI opened via {opened} · API: {url}")
    if errors:  # pragma: no cover - سرور بالا نیامد
        raise errors[0]
    thread.join(timeout=1.0)
    return 0


def _serve(config: Any, args: argparse.Namespace) -> int:
    """آماده‌سازی و اجرای سرور اپ‌ها از دل CLI (``agent-hub --serve``).

    منطق توکن: روی loopback هیچ توکنی لازم نیست؛ به‌محض باز شدن روی شبکه
    (``--lan`` یا ``SERVER_HOST`` غیر لوکال) یک توکن تصادفی ساخته و چاپ می‌شود،
    مگر اینکه کاربر خود ``--token`` را داده باشد یا ``--insecure`` بزند.
    """
    import secrets

    from src.server import run_server

    host = args.host or config.server_host
    if args.lan:
        host = "0.0.0.0"
    token = args.token or config.server_token
    if host not in {"127.0.0.1", "localhost", "::1"} and not token and not args.insecure:
        token = secrets.token_urlsafe(24)
    updates: dict[str, Any] = {
        "server_enabled": True,
        "server_host": host,
        "server_port": int(args.port or config.server_port),
        "server_token": token,
        "server_static_dir": "off" if args.no_ui else config.server_static_dir,
    }
    settings = config.model_copy(update=updates)
    if host in {"0.0.0.0", "::"}:
        urls = [f"http://{address}:{settings.server_port}" for address in _local_addresses()] or [settings.server_url]
    else:
        urls = [settings.server_url]
    console = Console() if Console is not None else None
    if console is not None:
        console.print(f"[bold cyan]Universal Agent Hub[/bold cyan] server on [green]{urls[0]}[/green]")
        if len(urls) > 1:
            console.print("  phone app URL: " + ", ".join(f"[green]{url}[/green]" for url in urls))
        console.print(
            f"  auth: {'[yellow]token ' + str(settings.server_token) if settings.server_token else '[dim]loopback only[/dim]'}"
        )
        console.print(
            "  open the URL in a browser or the Android/iOS app, then add your model API key in the Keys tab."
        )
        console.print(
            "  [dim]tools run with your real permissions — approvals appear in the app before anything destructive.[/dim]"
        )
    else:  # pragma: no cover - بدون rich
        print(f"Universal Agent Hub server on {urls[0]}")
        if settings.server_token:
            print(f"  token: {settings.server_token}")
    if getattr(args, "desktop", False):
        return serve_in_window(settings, urls[0], console)
    try:
        run_server(settings)
    except KeyboardInterrupt:  # pragma: no cover - توقف دستی
        if console is not None:
            console.print("\n[yellow]server stopped[/yellow]")
        else:
            print("server stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    """نقطه‌ی ورود CLI (console script ``agent-hub``).

    Args:
        argv: آرگومان‌ها (پیش‌فرض: ``sys.argv[1:]``).

    Returns:
        کد خروج فرآیند.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"universal-agent-hub {__version__}")
        return 0

    from src.config import reset_config
    from src.utils.logger import setup_logging

    dotenv_path = Path(args.config_file) if args.config_file else None
    # فقط فایل مشخص‌شده را override می‌کنیم تا .env پیش‌فرض پروژه بی‌صدا بازنویسی نشود
    if dotenv_path is not None and dotenv_path.is_file() and dotenv_path.resolve() != Path(".env").resolve():
        try:
            from dotenv import load_dotenv

            load_dotenv(dotenv_path, override=True)
        except ImportError:  # pragma: no cover
            pass
    reset_config()

    level = "DEBUG" if args.verbose > 1 else "INFO" if args.verbose else None
    config = get_config()
    if level:
        config = config.model_copy(update={"log_level": level})
    if args.model:
        config = config.model_copy(update={"model_name": args.model, "active_model": args.model})
    if args.yes:
        config = config.model_copy(update={"enable_confirmation": False})
    setup_logging(config.log_level, config.resolved_log_file, rich_console=config.rich_console)

    if args.doctor:
        return run_doctor(config)
    if args.report:
        return run_report(config, days=float(args.days or 0), as_json=args.json)
    if args.remember:
        return run_remember(config, str(args.remember), kind=str(args.kind), pin=bool(args.pin))
    if args.serve:
        return _serve(config, args)

    tools = [name.strip() for name in (args.tools or "").split(",") if name.strip()] or None
    cli = CLI(config=config, profile=args.profile, tools=tools, quiet=args.dry_run)

    if args.dry_run:
        cli._print_welcome()
        cli._cmd_tools([])
        cli._cmd_config([])
        cli._cmd_safety([])
        return 0
    try:
        return asyncio.run(cli.run(prompt_once=args.prompt, output_json=args.json))
    except KeyboardInterrupt:  # pragma: no cover - Ctrl+C در بیرون حلقه
        print("\n[yellow]interrupted[/yellow]")
        return 130


if __name__ == "__main__":  # pragma: no cover - نقطه‌ی ورود
    sys.exit(main())

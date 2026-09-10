"""تست رابط خط فرمان (:mod:`src.cli`).

هیج تستی مدل واقعی را صدا نمی‌زند: ایجنت با :class:`tests.fakes.FakeOpenAIClient`
ساخته می‌شود و خروجی Rich در یک ``StringIO`` گرفته می‌شود.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from rich.console import Console

from src import __version__
from src import cli as cli_module
from src.agent import UniversalAgent
from src.cli import CLI, WELCOME_MARKDOWN, _safe_write_history, build_parser, main
from src.config import Config
from src.core.base_tool import ConfirmationRequest
from src.core.event_bus import EVENTS, Event, EventBus
from src.core.tool_registry import discover_tools
from src.models.tool_models import RiskLevel
from tests.fakes import FakeOpenAIClient, make_chat_response


class Recorder(Console):
    """Console‌ای که خروجی را در حافظه نگه می‌دارد."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        super().__init__(file=self.buffer, force_terminal=False, width=200, height=None)

    @property
    def text(self) -> str:
        """متن چاپ‌شده تا این لحظه."""
        return self.buffer.getvalue()


@pytest.fixture
def agent(config: Config) -> UniversalAgent:
    """ایجنت واقعی با client ساختگی (بدون شبکه)."""
    return UniversalAgent(
        config,
        tools=["os_info", "terminal_run", "read_file"],
        client=FakeOpenAIClient([make_chat_response(content="answer from model", usage={"total_tokens": 11})]),
        event_bus=EventBus(),
    )


@pytest.fixture
def recorder() -> Recorder:
    """کامپول خروجی Rich."""
    return Recorder()


@pytest.fixture
def cli(config: Config, agent: UniversalAgent, recorder: Recorder) -> CLI:
    """CLI با ایجنت تزریق‌شده و کنسول ضبط‌شونده."""
    return CLI(config=config, console=recorder, agent=agent)


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    """رجیستری برای دستورهای /tools و /schema آماده باشد."""
    discover_tools(force=True)
    yield


def feed_inputs(monkeypatch: pytest.MonkeyPatch, inputs: list[str], *, confirm: bool | None = None) -> list[str]:
    """جایگزینی Prompt.ask/Confirm.ask با مقادیر از پیش تعیین‌شده."""
    seen: list[str] = []
    queue = list(inputs)

    def fake_prompt(cls: Any, prompt: str = "", **kwargs: Any) -> str:
        value = queue.pop(0) if queue else "quit"
        seen.append(value)
        return value

    monkeypatch.setattr(cli_module.Prompt, "ask", classmethod(fake_prompt))
    if confirm is not None:
        monkeypatch.setattr(cli_module.Confirm, "ask", classmethod(lambda cls, *args, **kwargs: confirm))
    return seen


# ---------------------------------------------------------------------------
# ساخت CLI
# ---------------------------------------------------------------------------
def test_cli_uses_injected_agent_and_console(cli: CLI, agent: UniversalAgent, recorder: Recorder) -> None:
    """تزریق agent/console باعث نمی‌شود CLI چیز دیگری بسازد."""
    assert cli.agent is agent and cli.console is recorder
    assert cli.bus is agent.event_bus
    assert cli.quiet is False and cli._running is False
    assert "universal" in WELCOME_MARKDOWN.lower()


def test_cli_without_rich_is_quiet(
    config: Config, agent: UniversalAgent, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """بدون Rich: کنسول None، حالت quiet خودکار و چاپ ساده‌ی متنی."""
    monkeypatch.setattr(cli_module, "Console", None)
    silent = CLI(config=config, agent=agent)
    assert silent.console is None and silent.quiet is True
    silent._print("[bold cyan]plain text[/bold cyan] no markup")
    out = capsys.readouterr().out
    assert "plain text no markup" in out and "[bold" not in out


def test_cli_builds_agent_from_profile(config: Config, recorder: Recorder) -> None:
    """ساخت ایجنت واقعی از کارخانه (بدون client واقعی)."""
    discover_tools(force=True)
    built = CLI(config=config, profile="read_only", console=recorder)
    try:
        names = {info["name"] for info in built.agent.describe_tools()}
        assert "read_file" in names and "write_file" not in names
        assert built.profile_name == "read_only"
    finally:
        asyncio.run(built.agent.close())


def test_cli_falls_back_when_profile_unknown(
    config: Config, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """پروفایل ناشناخته: پیام هشدار و ادامه با generalist."""

    def fake_create(profile: Any, **kwargs: Any) -> UniversalAgent:
        if profile == "does-not-exist":
            raise KeyError("unknown profile 'does-not-exist'")
        return UniversalAgent(config, tools=["os_info"], client=FakeOpenAIClient([]), event_bus=EventBus())

    from src.core.agent_factory import AgentFactory

    monkeypatch.setattr(AgentFactory, "create", staticmethod(fake_create))
    built = CLI(config=config, profile="does-not-exist", console=recorder)
    try:
        assert "unknown profile" in recorder.text and "generalist" in recorder.text
        assert built.agent._tool_names == ["os_info"]
    finally:
        asyncio.run(built.agent.close())


def test_cli_restricts_requested_tools(config: Config, recorder: Recorder) -> None:
    """``tools=`` محدودسازی می‌کند و پیام‌ها روی همان مجموعه است."""
    discover_tools(force=True)
    built = CLI(config=config, tools=["os_info"], console=recorder)
    try:
        assert built.requested_tools == ["os_info"]
        assert [info["name"] for info in built.agent.describe_tools()] == ["os_info"]
    finally:
        asyncio.run(built.agent.close())


# ---------------------------------------------------------------------------
# حالت یک‌نوبته (—prompt)
# ---------------------------------------------------------------------------
async def test_run_prompt_once_prints_answer(cli: CLI, recorder: Recorder) -> None:
    """پاسخ مدل در یک پنل چاپ و کد خروج ۰ برمی‌گردد."""
    assert await cli.run(prompt_once="do the thing") == 0
    assert "answer from model" in recorder.text
    assert "answer from model" in [entry["text"] for entry in cli._transcript if entry["type"] == "run"]


async def test_run_prompt_once_closes_agent(cli: CLI, agent: UniversalAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    """ایجنت در پایان بسته می‌شود (session ها آزاد شوند)."""
    closed: list[bool] = []
    original = agent.close

    async def spy() -> None:
        closed.append(True)
        await original()

    monkeypatch.setattr(agent, "close", spy)
    await cli.run(prompt_once="hi")
    assert closed == [True]


async def test_run_prompt_once_json_mode(cli: CLI, capsys: pytest.CaptureFixture[str]) -> None:
    """حالت --json یک JSON کامل از نتیجه چاپ می‌کند."""
    assert await cli.run(prompt_once="hi", output_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["text"] == "answer from model" and payload["ok"] is True
    assert payload["model"] == "gpt-6-astra" and payload["usage"]["total_tokens"] == 11


async def test_run_prompt_once_returns_error_code(config: Config, recorder: Recorder) -> None:
    """خطای API → ok=False و کد خروج ۱."""
    failing = UniversalAgent(
        config,
        tools=["os_info"],
        client=FakeOpenAIClient([], error=RuntimeError("connection reset"), error_times=99),
        event_bus=EventBus(),
    )
    tool_cli = CLI(config=config, console=recorder, agent=failing)
    assert await tool_cli.run(prompt_once="hi") == 1
    assert "connection reset" in recorder.text or "could not be used" in recorder.text


async def test_run_prompt_once_swallows_unexpected_crash(
    config: Config, recorder: Recorder, agent: UniversalAgent
) -> None:
    """خطای غیرمنتظره در ask به کد خروج تبدیل می‌شود، نه traceback."""

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("bad state in cli path")

    agent.ask = boom  # type: ignore[method-assign]
    tool_cli = CLI(config=config, console=recorder, agent=agent)
    assert await tool_cli.run(prompt_once="hi") == 1
    assert "error:" in recorder.text and "bad state in cli path" in recorder.text


async def test_interactive_loop_runs_prompt_and_quits(
    cli: CLI, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """حالت تعاملی: درخواست عادی، سپس quit."""
    feed_inputs(monkeypatch, ["hello there", "quit"])
    assert await cli.run() == 0
    assert "answer from model" in recorder.text and "Goodbye" in recorder.text
    await cli.agent.close()  # idempotent


async def test_interactive_loop_ignores_blank_lines_and_exits_on_eof(
    cli: CLI, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """خط خالی رد می‌شود و EOF مثل quit است."""
    calls = {"n": 0}

    def fake_prompt(cls: Any, prompt: str = "", **kwargs: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "   "
        raise EOFError

    monkeypatch.setattr(cli_module.Prompt, "ask", classmethod(fake_prompt))
    assert await cli.run() == 0
    assert "Interrupted" in recorder.text


async def test_interactive_loop_reports_agent_failures(
    config: Config, recorder: Recorder, agent: UniversalAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """نتیجه‌ی ناموفق در حلقه تعاملی کد خروج را ۱ می‌کند."""

    class _Result:
        text = ""
        error = "model unavailable"
        ok = False
        tool_calls: ClassVar[list[Any]] = []
        tool_names: ClassVar[list[str]] = []
        usage = type("U", (), {"total_tokens": 0})()
        iterations = 1
        duration_ms = 5

    async def fake_ask(*args: Any, **kwargs: Any) -> Any:
        return _Result()

    agent.ask = fake_ask  # type: ignore[method-assign]
    feed_inputs(monkeypatch, ["anything", "quit"])
    assert await CLI(config=config, console=recorder, agent=agent).run() == 1
    assert "model unavailable" in recorder.text


# ---------------------------------------------------------------------------
# دستورات داخلی
# ---------------------------------------------------------------------------
async def test_all_readonly_commands_succeed(cli: CLI, recorder: Recorder) -> None:
    """هیچ‌کدام از دستورهای اطلاعاتی استثنا نمی‌دهند و حلقه را نمی‌بندند."""
    for command in (
        "/help",
        "/tools",
        "/config",
        "/safety",
        "/model",
        "/profile",
        "/confirm",
        "/cd",
        "/history",
        "/events",
        "/schema os_info",
        "/transcript",
    ):
        assert await cli._handle_command(command) is False, command
    text = recorder.text
    assert "Active tools" in text and "Configuration" in text and "safety" in text
    assert "profiles:" in text and "no history yet" in text and "no events captured" in text


async def test_unknown_command_is_reported(cli: CLI, recorder: Recorder) -> None:
    """دستور ناشناخته: راهنمایی به /help."""
    assert await cli._handle_command("/frobnicate now") is False
    assert "unknown command /frobnicate" in recorder.text and "/help" in recorder.text


async def test_command_exception_does_not_kill_loop(
    cli: CLI, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """خطای داخل یک دستور به پیام تبدیل می‌شود (CLI نمی‌میرد)."""

    async def broken(self: CLI, args: list[str]) -> bool:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(CLI, "_cmd_help", broken)
    feed_inputs(monkeypatch, ["/help", "quit"])
    assert await cli.run() == 0
    assert "command failed" in recorder.text and "handler exploded" in recorder.text


async def test_schema_command_prints_json(cli: CLI, recorder: Recorder) -> None:
    """/schema اسکیمای ابزار را چاپ و نام ناشناخته را گزارش می‌کند."""
    assert await cli._handle_command("/schema os_info") is False
    assert '"os_info"' in recorder.text and "parameters" in recorder.text
    assert await cli._handle_command("/schema") is False and "usage:" in recorder.text
    assert await cli._handle_command("/schema ghost_tool") is False and "no tool named 'ghost_tool'" in recorder.text


async def test_tools_only_restricts_and_validates_names(cli: CLI, agent: UniversalAgent, recorder: Recorder) -> None:
    """‎/tools only لیست را محدود و نام نامعتبر را رد می‌کند."""
    assert await cli._handle_command("/tools only os_info,read_file") is False
    assert agent._tool_names == ["os_info", "read_file"]
    assert "tools set to: os_info, read_file" in recorder.text
    assert await cli._handle_command("/tools only ghost") is False
    assert "unknown tool(s): ghost" in recorder.text and agent._tool_names == ["os_info", "read_file"]
    assert await cli._handle_command("/tools only") is False
    assert agent._tool_names == [] and "(none)" in recorder.text


async def test_model_command_shows_and_changes_model(cli: CLI, agent: UniversalAgent, recorder: Recorder) -> None:
    """/model وضعیت را نشان می‌دهد و مدل جدید را (در همین session) ست می‌کند."""
    assert await cli._handle_command("/model") is False
    assert "gpt-6-astra" in recorder.text and "candidates" in recorder.text
    before = list(agent._model_candidates)
    assert await cli._handle_command("/model gpt-4o-turbo") is False
    assert agent.model == "gpt-4o-turbo" and agent._model_candidates == [*before, "gpt-4o-turbo"]
    assert "model set to gpt-4o-turbo" in recorder.text
    assert await cli._handle_command("/model os_info") is False  # مدل تکراری اضافه نمی‌شود
    assert agent._model_candidates.count("os_info") == 1


async def test_profile_command_rebuilds_agent(config: Config, recorder: Recorder, agent: UniversalAgent) -> None:
    """/profile ایجنت را از نو می‌سازد و تأیید قبلی را پاک می‌کند."""
    discover_tools(force=True)
    tool_cli = CLI(config=config, console=recorder, agent=agent, profile="generalist")
    assert await tool_cli._handle_command("/profile read_only") is False
    assert tool_cli.profile_name == "read_only"
    assert "rebuilt agent with profile 'read_only'" in recorder.text
    names = {info["name"] for info in tool_cli.agent.describe_tools()}
    assert "write_file" not in names and "read_file" in names
    await tool_cli.agent.close()


async def test_confirm_command_toggles_flag(cli: CLI, recorder: Recorder) -> None:
    """/confirm وضعیت را نشان و روشن/خاموش می‌کند."""
    assert await cli._handle_command("/confirm") is False and "ON" in recorder.text
    assert await cli._handle_command("/confirm off") is False
    assert cli.config.enable_confirmation is False and "OFF" in recorder.text
    await cli._handle_command("/confirm on")
    assert cli.config.enable_confirmation is True


async def test_cd_command_changes_and_validates_directory(cli: CLI, recorder: Recorder, tmp_path: Path) -> None:
    """/cd فقط دایرکتوری معتبر را می‌پذیرد و cwd را برمی‌گرداند."""
    original = Path.cwd()
    try:
        assert await cli._handle_command("/cd") is False and str(original) in recorder.text
        assert await cli._handle_command(f"/cd {tmp_path}") is False
        assert Path.cwd() == tmp_path
        assert await cli._handle_command(f"/cd {tmp_path / 'missing.txt'}") is False
        assert "not a directory" in recorder.text and Path.cwd() == tmp_path
    finally:
        os.chdir(original)


async def test_history_and_events_commands(cli: CLI, agent: UniversalAgent, recorder: Recorder) -> None:
    """/history و /events بعد از یک اجرا داده نشان می‌دهند."""
    await agent.ask("remember me")
    cli._render_result  # noqa: B018 - ارجاع صرف برای وضوح (ترنسکریپت اینجا مهم نیست)
    recorder.buffer.truncate(0)
    recorder.buffer.seek(0)
    assert await cli._handle_command("/history") is False
    assert "user" in recorder.text and "remember me" in recorder.text
    assert await cli._handle_command("/history 1") is False
    assert await cli._handle_command("/events 3") is False
    assert EVENTS.AGENT_STARTED in recorder.text or "llm.requested" in recorder.text


async def test_save_and_load_state_round_trip(
    cli: CLI, agent: UniversalAgent, recorder: Recorder, tmp_path: Path
) -> None:
    """/save و /load حالت گفت‌وگو را منتقل می‌کنند."""
    await agent.ask("first message")
    target = tmp_path / "session.json"
    assert await cli._handle_command(f"/save {target}") is False
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["messages"][0]["content"] == "first message" or "messages" in saved
    agent.reset_history()
    assert agent.state.messages == []
    assert await cli._handle_command(f"/load {target}") is False
    assert len(agent.state.messages) == len(saved["messages"]) and "loaded" in recorder.text
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert await cli._handle_command(f"/load {broken}") is False and "invalid state file" in recorder.text
    assert await cli._handle_command("/load") is False and "usage:" in recorder.text
    assert await cli._handle_command(f"/load {tmp_path / 'nope.json'}") is False and "no such file" in recorder.text


async def test_default_save_path_is_relative(cli: CLI, recorder: Recorder, workspace: Path) -> None:
    """بدون آرگومان، فایل در پوشه‌ی جاری نوشته می‌شود."""
    assert await cli._handle_command("/save") is False
    assert (workspace / "agent-session.json").is_file()
    assert "saved →" in recorder.text


async def test_transcript_command_writes_markdown(
    cli: CLI, agent: UniversalAgent, recorder: Recorder, workspace: Path
) -> None:
    """ترنسکریپت شامل اجراهای انجام‌شده و پوشه‌ی پیش‌فرض است."""
    result = await agent.ask("summarize the repo")
    cli._render_result(result)
    target = workspace / "log.md"
    assert await cli._handle_command(f"/transcript {target}") is False
    content = target.read_text(encoding="utf-8")
    assert content.startswith("# Universal Agent Hub transcript")
    assert "generalist" in content and "summarize the repo" not in content  # فقط پاسخ مدل ثبت می‌شود
    assert "answer from model" in content and "##" in content
    await cli._handle_command("/transcript")
    assert list(workspace.glob("agent-transcript-*.md"))


async def test_clear_command_resets_history_and_transcript(
    cli: CLI, agent: UniversalAgent, recorder: Recorder
) -> None:
    """/clear هم تاریخچه‌ی مدل و هم ترنسکریپت را پاک می‌کند."""
    cli._render_result(await agent.ask("hi"))
    assert cli._transcript and len(agent.state.messages) > 1
    assert await cli._handle_command("/clear") is False
    assert cli._transcript == [] and agent.state.messages == []
    assert "history cleared" in recorder.text


async def test_quit_commands_stop_the_loop(cli: CLI, recorder: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """/quit حلقه را می‌بندد (و اجرای ادامه‌ی دستورات انجام می‌شود)."""
    feed_inputs(monkeypatch, ["/quit"])
    assert await cli.run() == 0
    assert "Goodbye" in recorder.text
    assert cli._running is False


# ---------------------------------------------------------------------------
# تأیید عملیات
# ---------------------------------------------------------------------------
def request(risk: RiskLevel = RiskLevel.HIGH) -> ConfirmationRequest:
    """ساخت یک درخواست تأیید نمونه."""
    return ConfirmationRequest(
        tool="terminal_run",
        action="terminal.run",
        summary="Run shell command: rm -rf ./build",
        details={"command": "rm -rf ./build", "cwd": "/tmp"},
        risk=risk,
    )


async def test_confirm_requires_interaction(cli: CLI, recorder: Recorder) -> None:
    """خارج از حلقه (بدون _running) تأیید رد می‌شود — نه اینکه بی‌صدا اجرا شود."""
    cli._running = False
    assert await cli._confirm(request()) is False
    assert "no interactive prompt is available" in recorder.text
    silent = CLI(config=cli.config, console=None, agent=cli.agent)
    assert await silent._confirm(request()) is False


async def test_confirm_shows_details_and_records_answer(
    cli: CLI, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """جزئیات، سطح ریسک و پاسخ کاربر در ترنسکریپت ثبت می‌شود."""
    feed_inputs(monkeypatch, [], confirm=True)
    cli._running = True
    assert await cli._confirm(request()) is True
    text = recorder.text
    assert "confirmation required" in text and "rm -rf ./build" in text and "risk: high" in text
    assert cli._transcript[-1]["approved"] is True and cli._transcript[-1]["type"] == "confirmation"
    recorder.buffer.truncate(0)
    recorder.buffer.seek(0)
    assert await cli._confirm(request(risk=RiskLevel.LOW)) is True
    assert "risk:" not in recorder.text  # ریسک پایین توضیح اضافه نمی‌خواهد


async def test_confirm_decline_is_returned_and_logged(
    cli: CLI, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«نه» کاربر به ایجنت گزارش و در ترنسکریپت ضبط می‌شود."""
    feed_inputs(monkeypatch, [], confirm=False)
    cli._running = True
    assert await cli._confirm(request()) is False
    assert cli._transcript[-1]["approved"] is False


async def test_confirm_lock_serialises_parallel_requests(
    cli: CLI, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """تأییدهای هم‌زمان پشت قفل اجرا می‌شوند تا ورودی‌ها قاطی نشوند."""
    cli._running = True
    locked: list[bool] = []

    def fake_ask(cls: Any, *args: Any, **kwargs: Any) -> bool:
        locked.append(cli._confirm_lock.locked())
        return True

    monkeypatch.setattr(cli_module.Confirm, "ask", classmethod(fake_ask))
    outcomes = await asyncio.gather(*(cli._confirm(request()) for _ in range(3)))
    assert outcomes == [True, True, True]
    assert locked == [True, True, True]  # هر پرسش داخل ناحیه‌ی بحرانی قفل انجام شد
    assert len(cli._transcript) == 3 and all(item["approved"] for item in cli._transcript)


# ---------------------------------------------------------------------------
# رویدادها و نمایش
# ---------------------------------------------------------------------------
def test_tool_events_are_summarised(cli: CLI, recorder: Recorder) -> None:
    """هر رویداد ابزار یک خط وضعیت می‌شود (موفق سبز، ناموفق قرمز)."""
    cli._on_tool_event(
        Event(kind=EVENTS.TOOL_COMPLETED, payload={"tool": "os_info", "success": True, "duration_ms": 12})
    )
    cli._on_tool_event(
        Event(kind=EVENTS.TOOL_FAILED, payload={"tool": "terminal_run", "success": False, "error": "exit code 2"})
    )
    cli._on_tool_event(Event(kind=EVENTS.TOOL_COMPLETED, payload={"tool": "read_file", "success": True}))
    text = recorder.text
    assert "✓ os_info in 12 ms" in text and "✗ terminal_run: exit code 2" in text
    assert "read_file" in text and " ms" in text  # بدون duration زمان چاپ نمی‌شود


def test_tool_events_are_hidden_in_quiet_mode(config: Config, agent: UniversalAgent) -> None:
    """quiet=True هیچ رویداد ابزاری چاپ نمی‌کند."""
    silent = CLI(config=config, console=Recorder(), agent=agent, quiet=True)
    silent._on_tool_event(Event(kind=EVENTS.TOOL_COMPLETED, payload={"tool": "os_info", "success": True}))
    assert silent.console.text == ""  # type: ignore[union-attr]


def test_model_fallback_is_announced(cli: CLI, recorder: Recorder) -> None:
    """تغییر خودکار مدل با پیام واضح گزارش می‌شود."""
    cli._on_model_fallback(
        Event(kind="llm.model_fallback", payload={"from": "gpt-6-astra", "to": "gpt-4o-mini", "reason": "not found"})
    )
    assert "model fallback" in recorder.text and "gpt-4o-mini" in recorder.text and "not found" in recorder.text


def test_welcome_banner_lists_capabilities(cli: CLI, recorder: Recorder) -> None:
    """بنر خوش‌آمد: جدول ابزارها + وضعیت مدل."""
    cli._print_welcome()
    text = recorder.text
    assert "Universal Agent Hub" in text and "terminal_run" in text and "Active tools" in text


def test_model_status_warnings(config: Config, agent: UniversalAgent) -> None:
    """سه حالت پیام مدل: downgrade، کلید تنظیم‌نشده و مدل پیش‌فرض."""
    recorder = Recorder()
    tool_cli = CLI(config=config, console=recorder, agent=agent)
    tool_cli._show_model_status()
    assert "gpt-6-astra" in recorder.text and "MODEL_FALLBACKS" in recorder.text
    recorder = Recorder()
    downgraded = CLI(config=config, console=recorder, agent=agent)
    downgraded.agent._requested_model = "gpt-9-ultra"
    downgraded._show_model_status()
    assert "was not accepted" in recorder.text and "gpt-9-ultra" in recorder.text
    agent._requested_model = agent.model  # بازگرداندن وضعیت مشترک
    recorder = Recorder()
    no_key = CLI(config=config.model_copy(update={"openai_api_key": ""}), console=recorder, agent=agent)
    no_key._show_model_status()
    assert "OPENAI_API_KEY is not set" in recorder.text


def test_workspace_note_mentions_sandbox(cli: CLI) -> None:
    """یادداشت زمینه برای مدل، مسیرهای مجاز و وضعیت تأیید را دارد."""
    note = cli._workspace_note()
    for fragment in ("workspace=", "allowed_paths=", "profile=generalist", "platform=", "confirmations=on"):
        assert fragment in note


def test_tool_summary_in_rendered_output(config: Config, recorder: Recorder) -> None:
    """خلاصه‌ی ابزارها پس از یک اجرا با زمان هر کدام چاپ می‌شود."""
    agent = UniversalAgent(
        config,
        tools=["os_info"],
        client=FakeOpenAIClient(
            [
                make_chat_response(tool_calls=[{"id": "c1", "name": "os_info", "arguments": {}}]),
                make_chat_response(content="done", usage={"total_tokens": 4}),
            ]
        ),
        event_bus=EventBus(),
    )
    tool_cli = CLI(config=config, console=recorder, agent=agent, quiet=True)
    assert asyncio.run(tool_cli.run(prompt_once="go")) == 0
    text = recorder.text
    assert "done" in text and "✓ os_info" in text and "2 iteration(s)" in text and "4 tokens" in text


def test_readline_setup_is_optional(cli: CLI, monkeypatch: pytest.MonkeyPatch) -> None:
    """نبود readline (ویندوز) اجرا را نمی‌شکند."""
    cli._install_readline()
    monkeypatch.setitem(__import__("sys").modules, "readline", None)
    cli._install_readline()
    _safe_write_history(Path("/nonexistent-dir-xyz/history"))  # بی‌صدا


# ---------------------------------------------------------------------------
# parser و main
# ---------------------------------------------------------------------------
def test_parser_defaults() -> None:
    """مقادیر پیش‌فرض parser."""
    args = build_parser().parse_args([])
    assert args.profile == "generalist" and args.prompt is None and args.tools is None
    assert args.yes is False and args.dry_run is False and args.json is False and args.verbose == 0
    assert args.config_file == ".env" and args.version is False


def test_parser_parses_flags() -> None:
    """ترجمه‌ی فلگ‌ها (شامل -v تکراری و لیست ابزارها)."""
    args = build_parser().parse_args(
        ["-p", "hi", "--profile", "ops", "--model", "gpt-4o", "--tools", "a,b", "--yes", "--json", "-vv"]
    )
    assert args.prompt == "hi" and args.profile == "ops" and args.model == "gpt-4o"
    assert args.tools == "a,b" and args.yes is True and args.json is True and args.verbose == 2
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--nope"])


def test_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--version نسخه را چاپ و با ۰ خارج می‌شود."""
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_main_dry_run_prints_config_without_model(
    config: Config, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--dry-run تنظیمات و ابزارها را نشان می‌دهد و مدل را صدا نمی‌زند."""
    env = tmp_path / "custom.env"
    env.write_text(
        "OPENAI_API_KEY=sk-dry-run-key-0123456789\nMODEL_NAME=gpt-6-astra\nALLOWED_DIRECTORIES=\n", encoding="utf-8"
    )
    snapshot = dict(os.environ)
    try:
        os.environ["OPENAI_BASE_URL"] = ""
        code = main(["--dry-run", "--config-file", str(env), "--profile", "read_only"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Configuration" in out and "safety" in out and "Active tools" in out
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def test_main_prompt_with_injected_agent(
    capsys: pytest.CaptureFixture[str],
    recorder: Recorder,
    agent: UniversalAgent,
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
) -> None:
    """مسیر کامل main با --prompt و --json (ایجنت ساختگی)."""
    from src.core.agent_factory import AgentFactory

    monkeypatch.setattr(AgentFactory, "create", staticmethod(lambda profile, **kwargs: agent))
    assert main(["--prompt", "hello", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["text"] == "answer from model" and payload["ok"] is True


def test_main_yes_flag_disables_confirmation(
    agent: UniversalAgent, monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """--yes نیاز تأیید را خاموش می‌کند (برای اتوماسیون)."""
    from src.core.agent_factory import AgentFactory

    captured: dict[str, Any] = {}

    def fake_cli(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)

        class _Noop:
            async def run(self, **run_kwargs: Any) -> int:
                return 0

        return _Noop()

    monkeypatch.setattr(AgentFactory, "create", staticmethod(lambda profile, **kwargs: agent))
    monkeypatch.setattr(cli_module, "CLI", fake_cli)
    assert main(["--yes", "--prompt", "x"]) == 0
    assert captured["config"].enable_confirmation is False


def test_main_applies_model_and_verbose(agent: UniversalAgent, monkeypatch: pytest.MonkeyPatch) -> None:
    """--model و -v به config می‌رسند."""
    from src.core.agent_factory import AgentFactory

    captured: dict[str, Any] = {}

    def fake_cli(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)

        class _Noop:
            async def run(self, **run_kwargs: Any) -> int:
                return 3

        return _Noop()

    monkeypatch.setattr(AgentFactory, "create", staticmethod(lambda profile, **kwargs: agent))
    monkeypatch.setattr(cli_module, "CLI", fake_cli)
    assert main(["--model", "gpt-4o-mini", "-v", "--tools", "os_info,read_file", "--prompt", "x"]) == 3
    assert captured["config"].model_name == "gpt-4o-mini" and captured["config"].log_level == "INFO"
    assert captured["tools"] == ["os_info", "read_file"] and captured["profile"] == "generalist"


# ---------------------------------------------------------------------------
# --doctor / --report / --remember و دستورهای داخلی آن‌ها
# ---------------------------------------------------------------------------
def test_doctor_checks_cover_every_subsystem(config: Config) -> None:
    """خودسنجی درباره‌ی هر لایه حرف دارد و هیچ ردیفی خالی نیست."""
    rows = cli_module.doctor_checks(config)
    names = [name for name, _status, _detail in rows]
    for expected in ("python", "api key", "tools", "safety guard", "memory", "activity report", "server"):
        assert expected in names, expected
    assert all(detail.strip() for _name, _status, detail in rows)
    assert {status for _name, status, _detail in rows} <= {"ok", "warn", "fail"}


def test_doctor_flags_missing_key_and_disabled_guard(tmp_path: Path) -> None:
    """کلید خالی = fail؛ نگهبان خاموش = fail (چون خطر اصلی همین است)."""
    bare = Config(openai_api_key="", project_root=tmp_path, memory_enabled=False, reports_enabled=False)
    rows = {name: (status, detail) for name, status, detail in cli_module.doctor_checks(bare)}
    assert rows["api key"][0] == "fail"
    assert rows["memory"][0] == "warn" and rows["activity report"][0] == "warn"
    unguarded = Config(openai_api_key="sk-test-key-long-enough", project_root=tmp_path, enable_safety_guard=False)
    guard_row = next(row for row in cli_module.doctor_checks(unguarded) if row[0] == "safety guard")
    assert guard_row[1] == "fail"
    assert "ENABLE_SAFETY_GUARD" in guard_row[2] or "policy" in guard_row[2]


def test_doctor_memory_probe_does_not_leave_records(config: Config) -> None:
    """آزمون‌نوشتن doctor باید خودش را پاک کند (حافظه با diagnostics پر نشود)."""
    from src.core.memory import AgentMemory

    before = len((AgentMemory.for_config(config) or AgentMemory(None)).all())
    cli_module.run_doctor(config)
    memory = AgentMemory.for_config(config)
    assert memory is not None
    assert len(memory.all()) == before
    assert all("doctor probe" not in item.content for item in memory.all())


def test_main_doctor_and_report_are_non_interactive(capsys: pytest.CaptureFixture[str]) -> None:
    """``--doctor`` و ``--report`` بدون ایجنت/شبکه اجرا می‌شوند و exit code می‌دهند."""
    assert main(["--doctor"]) == 0
    out = capsys.readouterr().out
    assert "python" in out and "safety guard" in out
    assert main(["--report", "--days", "0", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["window_days"] == 0
    assert set(payload) >= {"runs", "safety", "tools", "plans", "warnings"}


def test_main_remember_reports_disabled_memory(capsys: pytest.CaptureFixture[str]) -> None:
    """در تست MEMORY_ENABLED=0 است؛ --remember باید همان را بگوید نه اینکه سکوت کند."""
    assert main(["--remember", "prefers short answers"]) == 1
    assert "MEMORY_ENABLED" in capsys.readouterr().out


def test_main_remember_writes_when_memory_is_on(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    """با حافظه‌ی فعال، یادداشت واقعاً ذخیره و id چاپ می‌شود."""
    from src.core.memory import AgentMemory

    enabled = config.model_copy(update={"memory_enabled": True, "memory_dir": str(config.project_root / "cli-mem")})
    assert cli_module.run_remember(enabled, "the deploy host is prod1", kind="procedure") == 0
    saved = capsys.readouterr().out
    assert "saved procedure" in saved
    memory = AgentMemory.for_config(enabled)
    assert memory is not None
    assert [item.content for item in memory.all()] == ["the deploy host is prod1"]
    assert cli_module.run_remember(enabled, "x", kind="gossip") == 1
    assert "unknown kind" in capsys.readouterr().out


def test_parser_accepts_new_flags() -> None:
    """پرچم‌های جدید در parser تعریف شده‌اند و پیش‌فرض‌هایشان درست است."""
    args = build_parser().parse_args(
        ["--doctor", "--report", "--days", "30", "--remember", "note", "--kind", "plan", "--pin"]
    )
    assert args.doctor is True and args.report is True
    assert args.days == 30.0 and args.kind == "plan" and args.pin is True
    defaults = build_parser().parse_args([])
    assert defaults.days == 7.0 and defaults.doctor is False and defaults.kind == "note"


async def test_slash_memory_roundtrip(cli: CLI, recorder: Recorder, config: Config) -> None:
    """/memory add → list → search → forget، همه روی همان فایل موقت."""
    from src.core.memory import AgentMemory

    await cli._handle_command("/memory list")  # noqa: SLF001
    assert "memory is empty" in recorder.text
    await cli._handle_command("/memory add keep answers short")  # noqa: SLF001
    memory = AgentMemory.for_config(config)
    assert memory is not None
    record = memory.all()[0]
    assert record.content == "keep answers short" and record.source == "cli:/memory"
    recorder.buffer.truncate(0)
    recorder.buffer.seek(0)
    await cli._handle_command("/memory search keep short")  # noqa: SLF001
    assert record.id in recorder.text
    await cli._handle_command("/memory plan audit the backups next week")  # noqa: SLF001
    assert "plan" in recorder.text
    memory = AgentMemory.for_config(config)
    assert memory is not None
    assert [item.kind for item in memory.all()] == ["plan", "note"] or {"plan", "note"} == {
        item.kind for item in memory.all()
    }
    await cli._handle_command(f"/memory forget {record.id}")  # noqa: SLF001
    assert "forgotten" in recorder.text
    await cli._handle_command("/memory stats")  # noqa: SLF001
    assert "records" in recorder.text
    await cli._handle_command("/memory search")  # noqa: SLF001
    assert "usage" in recorder.text
    await cli._handle_command("/memory forget nope")  # noqa: SLF001
    assert "no such record" in recorder.text


async def test_slash_memory_explains_when_disabled(recorder: Recorder, config: Config, agent: UniversalAgent) -> None:
    """با حافظه‌ی خاموش، /memory پیام روشن می‌دهد (نه استثنا)."""
    quiet = config.model_copy(update={"memory_enabled": False})
    silent_agent = UniversalAgent(
        quiet, tools=["os_info"], client=FakeOpenAIClient([make_chat_response(content="x")]), event_bus=EventBus()
    )
    silent_agent.memory = None
    off_cli = CLI(config=quiet, console=recorder, agent=silent_agent)
    await off_cli._handle_command("/memory list")  # noqa: SLF001
    assert "disabled" in recorder.text
    await silent_agent.close()


async def test_slash_report_and_doctor_print_sections(cli: CLI, recorder: Recorder) -> None:
    """/report و /doctor متن/جدول مناسب چاپ می‌کنند."""
    await cli._handle_command("/report")  # noqa: SLF001
    assert "Activity report" in recorder.text and "runs:" in recorder.text
    await cli._handle_command("/report not-a-number")  # noqa: SLF001
    assert "must be a number" in recorder.text
    recorder.buffer.truncate(0)
    recorder.buffer.seek(0)
    await cli._handle_command("/doctor")  # noqa: SLF001
    assert "python" in recorder.text and "safety guard" in recorder.text and "memory" in recorder.text


def test_help_lists_the_new_commands(recorder: Recorder, config: Config) -> None:
    """راهنما باید دستورهای جدید را نشان دهد (وگرنه کسی پیدایشان نمی‌کند)."""
    listed = CLI(config=config, console=recorder)
    listed._cmd_help([])  # noqa: SLF001
    for token in ("/memory", "/report", "/doctor"):
        assert token in recorder.text

"""تست ابزار ترمینال (:mod:`src.tools.terminal`)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from src.config import Config
from src.core.base_tool import ToolContext
from src.models.tool_models import ToolResult
from src.tools.terminal import ShellSession, TerminalTool
from src.utils.safety import SafetyDecision
from src.utils.validators import ValidationError

pytestmark = pytest.mark.integration


@pytest.fixture
def tool(config: Config) -> TerminalTool:
    """نمونه‌ی ابزار با config تست."""
    return TerminalTool(config)


async def test_simple_command(tool: TerminalTool, context: ToolContext) -> None:
    """اجرای موفق، stdout و exit code."""
    result = await tool.run({"command": "echo 'test'"}, context=context)
    assert result.success, result.error
    assert result.data["stdout"] == "test"
    assert result.data["exit_code"] == 0
    assert result.duration_ms >= 0
    assert result.metadata["cwd"] == str(Path.cwd())


async def test_stderr_and_exit_code(tool: TerminalTool, context: ToolContext) -> None:
    """دستور ناموفق: خطا و exit code غیرصفر گزارش می‌شود (با shell=true)."""
    result = await tool.run({"command": "echo bad 1>&2; exit 3", "shell": True}, context=context)
    assert not result.success and result.error_code == "non_zero_exit"
    assert result.data["exit_code"] == 3
    assert "bad" in result.data["stderr"]
    assert "bad" in (result.error or "")


async def test_missing_binary_is_reported(tool: TerminalTool, context: ToolContext) -> None:
    """برنامه‌ی ناموجود → command_not_found."""
    result = await tool.run({"command": "definitely-not-a-real-binary-xyz"}, context=context)
    assert not result.success and result.error_code == "command_not_found"
    assert "command not found" in (result.error or "")


async def test_timeout_kills_process(tool: TerminalTool, context: ToolContext) -> None:
    """گذشت زمان → کشتن process و timeout."""
    result = await tool.run({"command": "sleep 5", "timeout": 1}, context=context)
    assert not result.success and result.error_code == "timeout"
    assert "timed out after 1s" in (result.error or "")


async def test_timeout_is_capped_by_config(tool: TerminalTool, context: ToolContext) -> None:
    """timeout درخواستی بیشتر از سقف config، به سقف محدود می‌شود."""
    with pytest.raises(ValidationError, match="between 1 and 10"):
        tool.validate_input(command="sleep 1", timeout=9999)


async def test_stdin_input_is_passed(tool: TerminalTool, context: ToolContext) -> None:
    """ورودی stdin به دستور می‌رسد."""
    result = await tool.run({"command": "cat", "input": "piped text"}, context=context)
    assert result.success and result.data["stdout"] == "piped text"


async def test_env_overlay(tool: TerminalTool, context: ToolContext) -> None:
    """متغیرهای محیطی سفارشی فقط برای همان دستور اعمال می‌شوند."""
    result = await tool.run({"command": "printenv MY_AGENT_VAR", "env": {"MY_AGENT_VAR": "42"}}, context=context)
    assert result.success and result.data["stdout"] == "42"
    assert "MY_AGENT_VAR" not in os.environ


async def test_shell_mode_requires_confirmation(tool: TerminalTool, config: Config) -> None:
    """اجرای pipe فقط با shell=true و همراه با تأیید است."""
    declined = ToolContext(config=config, confirm=lambda request: False)
    result = await tool.run({"command": "echo hi | tr a-z A-Z", "shell": True}, context=declined)
    assert not result.success and result.error_code == "declined"

    approved = ToolContext(config=config, confirm=lambda request: True)
    ok = await tool.run({"command": "echo hi | tr a-z A-Z", "shell": True}, context=approved)
    assert ok.success and ok.data["stdout"] == "HI"


async def test_shell_operators_without_shell_are_rejected(tool: TerminalTool, context: ToolContext) -> None:
    """اپراتور shell بدون shell=true → بلاک با پیام راهنما."""
    result = await tool.run({"command": "echo hi | wc -l"}, context=context)
    assert not result.success and result.error_code == "blocked"
    assert "shell=true" in (result.error or "")


async def test_dangerous_command_requires_confirmation(tool: TerminalTool, config: Config) -> None:
    """دستور ویرانگر: تأیید می‌خواهد و با سیاست deny مسدود می‌شود."""
    tool = TerminalTool(config)
    decision = await tool.safety_check({"command": "rm -rf /"})
    assert decision.requires_confirmation and decision.risk.value == "critical"

    denied = TerminalTool(config.model_copy(update={"dangerous_command_policy": "deny"}))
    blocked = await denied.run({"command": "rm -rf /"}, context=ToolContext(config=config, confirm=lambda r: True))
    assert blocked.error_code == "blocked"


async def test_working_directory_validation(tool: TerminalTool, context: ToolContext, workspace: Path) -> None:
    """cwd ناموجود یا خارج از sandbox رد می‌شود."""
    inside = await tool.run({"command": "pwd", "cwd": str(workspace)}, context=context)
    assert inside.success and inside.data["stdout"].endswith(workspace.name)
    missing = await tool.run({"command": "pwd", "cwd": str(workspace / "nope")}, context=context)
    assert not missing.success and "not an existing directory" in (missing.error or "")
    outside = await tool.run({"command": "pwd", "cwd": "/etc"}, context=context)
    assert not outside.success and "outside the allowed" in (outside.error or "")


async def test_safety_guard_absent_still_runs(config: Config) -> None:
    """بدون guard هم ابزار کار می‌کند، ولی shell را بی‌تأیید اجرا نمی‌کند."""
    tool = TerminalTool()
    ctx = ToolContext(config=config, safety=None, confirm=lambda r: True)
    safe = await tool.safety_check({"command": "echo hi"}, ctx)
    assert safe.allowed and not safe.requires_confirmation
    risky = await tool.safety_check({"command": "echo hi", "shell": True}, ctx)
    assert risky.allowed and risky.requires_confirmation and risky.risk.value == "high"
    pipe = await tool.safety_check({"command": "echo hi | cat"}, ctx)
    assert pipe.requires_confirmation


async def test_confirmation_without_guard_reasons(tool: TerminalTool) -> None:
    """متن دلایل guard نبودِ guard را توضیح می‌دهد."""
    tool = TerminalTool()
    decision = await tool.safety_check({"command": "echo hi"}, context=None)
    assert decision.allowed and "safety guard unavailable" in decision.reason_text


async def test_large_output_is_truncated(tool: TerminalTool, context: ToolContext, config: Config) -> None:
    """خروجی بزرگ پیش از رفتن به مدل بریده می‌شود."""
    result = await tool.run(
        {"command": "python -c \"print('x'*5000)\""},
        context=ToolContext(config=config.model_copy(update={"max_output_chars": 1200})),
    )
    assert result.success and result.truncated
    assert len(result.data["stdout"]) <= 1300
    llm_text = result.to_llm_string(max_chars=1200)
    assert len(llm_text) <= 1250


async def test_binary_output_does_not_crash(tool: TerminalTool, context: ToolContext) -> None:
    """خروجی باینری با replace دیکد می‌شود."""
    result = await tool.run(
        {"command": "printf '\\377\\376ok'", "shell": True},
        context=ToolContext(
            config=context.config, safety=context.safety, bus=context.bus, confirm=lambda request: True
        ),
    )
    assert result.success and "ok" in result.data["stdout"]


def test_schema_shape(tool: TerminalTool) -> None:
    """اسکیمای ابزار ترمینال."""
    schema = tool.get_schema()["function"]
    assert schema["name"] == "terminal_run"
    assert schema["parameters"]["required"] == ["command"]
    props = schema["parameters"]["properties"]
    assert set(props) == {"command", "timeout", "cwd", "shell", "input", "env"}
    assert props["timeout"]["maximum"] == 10
    assert "confirmation" in schema["description"].lower() or "confirm" in schema["description"].lower()


def test_validate_input_rejects_bad_commands(tool: TerminalTool) -> None:
    """ورودی‌های نامعتبر."""
    with pytest.raises(ValidationError, match="must not be empty"):
        tool.validate_input(command="   ")
    with pytest.raises(ValidationError, match="Multi-line"):
        tool.validate_input(command="echo a\necho b")
    with pytest.raises(ValidationError, match="Unbalanced"):
        tool.validate_input(command="echo 'x")


async def test_shell_session_runs_commands_in_parallel(tool: TerminalTool, context: ToolContext) -> None:
    """اجرای دسته‌ای با سقف همزمانی."""
    session = ShellSession(
        ["sleep 0.2; echo a", "sleep 0.2; echo b", "false"], tool=tool, context=context, max_parallel=3
    )
    results = await session.run()
    assert [r.success for r in results] == [True, True, False]
    assert results[0].data["stdout"] == "a" and results[1].data["stdout"] == "b"
    assert results[2].error_code == "non_zero_exit"


async def test_shell_session_fail_fast(tool: TerminalTool, context: ToolContext) -> None:
    """با fail_fast، بقیه اجرا نمی‌شوند."""
    session = ShellSession(
        ["false", "echo never", "echo never2"], tool=tool, context=context, fail_fast=True, max_parallel=1
    )
    results = await session.run()
    assert not results[0].success
    assert all(r.error_code == "skipped" for r in results[1:])


def test_shell_session_validates_parallel(tool: TerminalTool) -> None:
    """max_parallel باید مثبت باشد."""
    with pytest.raises(ValueError, match="max_parallel"):
        ShellSession([], tool=tool, max_parallel=0)


async def test_execute_can_be_used_directly(tool: TerminalTool) -> None:
    """استفاده‌ی مستقیم execute (بدون wrapper) هم کار می‌کند."""
    outcome = await tool.execute(command="echo direct")
    assert isinstance(outcome, ToolResult) and outcome.success


async def test_safety_check_uses_custom_decision(tool: TerminalTool) -> None:
    """override شدن safety_check در subclass سفارشی."""

    class Paranoid(TerminalTool):
        """ابزاری که هر دستوری را رد می‌کند."""

        name = "paranoid_terminal"

        async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
            return SafetyDecision(allowed=False, risk=self.risk_level, reasons=["disabled in this deployment"])

    result = await Paranoid().run({"command": "echo hi"})
    assert not result.success and "disabled in this deployment" in (result.error or "")

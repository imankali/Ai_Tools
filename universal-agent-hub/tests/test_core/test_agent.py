"""تست حلقه‌ی ایجنت (:mod:`src.agent`).

API واقعی صدا زده نمی‌شود؛ از :class:`tests.fakes.FakeOpenAIClient` استفاده می‌کنیم
تا رفتار retry، fallback مدل، اجرای ابزار، تأیید و مدیریت خطا قطعی تست شوند.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent import DEFAULT_SYSTEM_PROMPT, UniversalAgent
from src.config import Config
from src.core.base_tool import BaseTool, ConfirmationRequest, ToolContext
from src.core.event_bus import EVENTS, EventBus
from src.core.tool_registry import ToolRegistry, discover_tools
from src.models.agent_models import AgentState, Message
from src.models.tool_models import ToolCategory, ToolResult
from tests.fakes import FakeOpenAIClient, make_chat_response


@pytest.fixture(autouse=True)
def _registered(config: Config) -> None:
    """رجیستری برای هر تست آماده باشد."""
    ToolRegistry.set_default_config(config)
    discover_tools(force=True)


def build_agent(
    responses: list[Any],
    *,
    config: Config,
    tools: list[str] | None = None,
    error: BaseException | None = None,
    error_times: int = 1,
    event_bus: EventBus | None = None,
    **kwargs: Any,
) -> UniversalAgent:
    """ساخت ایجنت با client ساختگی."""
    client = FakeOpenAIClient(responses, error=error, error_times=error_times)
    return UniversalAgent(
        config,
        tools=tools if tools is not None else ["os_info", "terminal_run"],
        client=client,
        event_bus=event_bus or EventBus(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# مسیر موفق
# ---------------------------------------------------------------------------
async def test_plain_answer_without_tools(config: Config) -> None:
    """پاسخ متنی ساده، بدون هیچ فراخوانی ابزاری."""
    agent = build_agent(
        [
            make_chat_response(
                content="**hello**", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            )
        ],
        config=config,
    )
    result = await agent.ask("hi")
    assert result.ok and result.text == "**hello**"
    assert result.tool_calls == [] and result.iterations == 1
    assert result.usage.total_tokens == 5
    payload = agent.client.payloads[0]
    assert payload["model"] == "gpt-6-astra"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][-1] == {"role": "user", "content": "hi"}
    assert payload["tools"] and all(tool["type"] == "function" for tool in payload["tools"])


async def test_tool_call_round_trip(config: Config) -> None:
    """مدل ابزار می‌خواهد → اجرا → پاسخ نهایی."""
    agent = build_agent(
        [
            make_chat_response(tool_calls=[{"id": "c1", "name": "os_info", "arguments": {}}]),
            make_chat_response(content="here is the info"),
        ],
        config=config,
    )
    result = await agent.ask("what os?")
    assert result.ok and result.text == "here is the info"
    assert result.iterations == 2
    assert result.tool_names == ["os_info"]
    record = result.tool_calls[0]
    assert record.succeeded and record.result.data["system"]
    second = agent.client.payloads[1]
    tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert json.loads(tool_messages[0]["content"])["data"]["system"]
    assistant = second["messages"][-2]
    assert assistant["tool_calls"][0]["function"]["name"] == "os_info"


async def test_string_arguments_are_parsed(config: Config) -> None:
    """arguments به‌صورت رشته‌ی JSON هم پذیرفته می‌شود."""
    agent = build_agent(
        [
            make_chat_response(
                tool_calls=[{"name": "terminal_run", "arguments": json.dumps({"command": "echo one"})}]
            ),
            make_chat_response(content="done"),
        ],
        config=config,
    )
    result = await agent.ask("run echo")
    stdout = result.tool_calls[0].result.data["stdout"]
    assert stdout == "one"


async def test_broken_json_arguments_do_not_crash(config: Config) -> None:
    """JSON خراب/ناقص → پیام invalid_input به مدل، بدون انفجار اجرا."""
    agent = build_agent(
        [
            make_chat_response(tool_calls=[{"name": "terminal_run", "arguments": "{not-json"}]),
            make_chat_response(tool_calls=[{"name": "os_info", "arguments": '{"unexpected": '}]),
            make_chat_response(content="I need to fix that"),
        ],
        config=config,
    )
    result = await agent.ask("weird args")
    assert result.ok
    tool_message = next(m for m in agent.client.payloads[1]["messages"] if m["role"] == "tool")
    assert tool_message["content"].startswith('{"error": "missing required parameter(s): command"')
    # فراخوانی بعدی: ورودی parse نشده به شکل رشته رد می‌شود و اجرا ادامه می‌یابد
    second = [m for m in agent.client.payloads[2]["messages"] if m["role"] == "tool"][-1]
    assert "ignored" not in second["content"]  # پارامتر ناشناخته بی‌صدا رد شد و ابزار اجرا شد
    assert json.loads(second["content"])["data"]["system"] == "Linux"


async def test_unknown_tool_is_reported_back(config: Config) -> None:
    """ابزار ناموجود: پیام راهنما به مدل داده می‌شود."""
    agent = build_agent(
        [
            make_chat_response(tool_calls=[{"name": "telepathy", "arguments": {}}]),
            make_chat_response(content="no such tool, sorry"),
        ],
        config=config,
    )
    result = await agent.ask("read my mind")
    assert result.failed_tools == ["telepathy"]
    tool_message = next(m for m in agent.client.payloads[1]["messages"] if m["role"] == "tool")
    assert "unknown tool 'telepathy'" in tool_message["content"]
    assert "os_info" in tool_message["content"]


async def test_multiple_independent_calls_run_in_parallel(config: Config) -> None:
    """چند فراخوانی مستقل، موازی اجرا می‌شوند."""
    agent = build_agent(
        [
            make_chat_response(
                tool_calls=[
                    {"id": "a", "name": "os_info", "arguments": {}},
                    {"id": "b", "name": "memory_info", "arguments": {}},
                    {"id": "c", "name": "disk_info", "arguments": {}},
                ]
            ),
            make_chat_response(content="all three done"),
        ],
        config=config.model_copy(update={"parallel_tool_calls": True}),
        tools=["os_info", "memory_info", "disk_info"],
    )
    result = await agent.ask("three at once")
    assert result.ok and len(result.tool_calls) == 3
    tool_messages = [m for m in agent.client.payloads[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["a", "b", "c"]
    assert len({m["name"] for m in tool_messages}) == 3


async def test_serial_mode_when_parallel_disabled(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """با PARALLEL_TOOL_CALLS=false ترتیبی اجرا می‌شود."""
    seen: list[str] = []
    original = ToolRegistry.get

    def spy(name: str, *, config: Any = None) -> Any:  # noqa: ANN001
        tool = original(name, config=config)
        if tool is not None:
            seen.append(name)
        return tool

    monkeypatch.setattr(ToolRegistry, "get", staticmethod(spy))
    agent = build_agent(
        [
            make_chat_response(
                tool_calls=[{"name": "os_info", "arguments": {}}, {"name": "memory_info", "arguments": {}}]
            ),
            make_chat_response(content="serial ok"),
        ],
        config=config.model_copy(update={"parallel_tool_calls": False}),
        tools=["os_info", "memory_info"],
    )
    assert (await agent.ask("serial")).text == "serial ok"
    assert "os_info" in seen


# ---------------------------------------------------------------------------
# ایمنی و تأیید
# ---------------------------------------------------------------------------
async def test_declined_confirmation_is_reported_to_model(config: Config) -> None:
    """کاربر تأیید نکند → نتیجه declined و مدل آگاه می‌شود."""
    agent = build_agent(
        [
            make_chat_response(tool_calls=[{"name": "delete_file", "arguments": {"path": "sample.txt"}}]),
            make_chat_response(content="understood, I will not delete it"),
        ],
        config=config.model_copy(update={"enable_confirmation": True}),
        tools=["delete_file"],
        confirm_callback=lambda request: False,
    )
    result = await agent.ask("delete my file please")
    assert result.ok and "not delete" in result.text
    assert result.tool_calls[0].result.error_code == "declined"
    tool_message = next(m for m in agent.client.payloads[1]["messages"] if m["role"] == "tool")
    assert "declined by the user" in tool_message["content"]
    assert (config.project_root / "sample.txt").exists()  # فایل دست‌نخورده ماند


async def test_approved_confirmation_runs_the_tool(config: Config) -> None:
    """تأیید کاربر → اجرای ابزار و شمارش در state."""
    asked: list[str] = []

    def confirm(request: ConfirmationRequest) -> bool:
        asked.append(request.tool)
        return True

    agent = build_agent(
        [
            make_chat_response(
                tool_calls=[{"name": "delete_file", "arguments": {"path": "sample.txt", "dry_run": True}}]
            ),
            make_chat_response(content="preview done"),
        ],
        config=config,
        tools=["delete_file"],
        confirm_callback=confirm,
    )
    result = await agent.ask("preview deletion")
    assert asked == ["delete_file"]
    assert result.tool_calls[0].succeeded and result.tool_calls[0].result.data["dry_run"] is True
    assert agent.state.confirmations_granted == 1


async def test_blocked_command_never_reaches_subprocess(config: Config) -> None:
    """دستور ویرانگر با سیاست deny اجرا نمی‌شود."""
    strict = config.model_copy(update={"dangerous_command_policy": "deny"})
    agent = build_agent(
        [
            make_chat_response(tool_calls=[{"name": "terminal_run", "arguments": {"command": "rm -rf /"}}]),
            make_chat_response(content="I refused that"),
        ],
        config=strict,
        tools=["terminal_run"],
    )
    result = await agent.ask("delete everything")
    assert result.tool_calls[0].result.error_code == "blocked"
    assert result.text == "I refused that"


async def test_tool_failure_does_not_break_the_run(config: Config) -> None:
    """خطای ابزار (exit code غیرصفر) فقط گزارش می‌شود."""
    agent = build_agent(
        [
            make_chat_response(tool_calls=[{"name": "terminal_run", "arguments": {"command": "false"}}]),
            make_chat_response(content="it failed as expected"),
        ],
        config=config,
        tools=["terminal_run"],
    )
    result = await agent.ask("run false")
    assert result.ok and not result.tool_calls[0].succeeded
    assert result.tool_calls[0].result.error_code == "non_zero_exit"


async def test_iteration_limit_is_enforced(config: Config) -> None:
    """حلقه‌ی بی‌پایان ابزار، پس از سقف متوقف می‌شود."""
    endless = make_chat_response(tool_calls=[{"name": "os_info", "arguments": {}}])
    agent = build_agent([endless], config=config.model_copy(update={"max_tool_iterations": 3}), tools=["os_info"])
    result = await agent.ask("loop forever")
    assert result.iterations == 3
    assert "iteration limit" in result.text
    assert len(result.tool_calls) == 3


# ---------------------------------------------------------------------------
# خطاهای مدل
# ---------------------------------------------------------------------------
async def test_transient_error_is_retried(config: Config) -> None:
    """خطای موقت → تلاش مجدد (با backoff) و موفقیت."""

    class Transient(Exception):
        """خطای ۴۲۹ ساختگی."""

        status_code = 429

    agent = build_agent(
        [make_chat_response(content="recovered")],
        config=config.model_copy(update={"max_retries": 2, "request_timeout": 5}),
        error=Transient("Too Many Requests"),
        error_times=1,
    )
    result = await agent.ask("hi")
    assert result.ok and result.text == "recovered"
    assert agent.client.call_count == 2


async def test_permanent_error_is_reported(config: Config) -> None:
    """خطای غیرقابل تلاش‌مجدد → ok=False با پیام خوانا."""

    class Permanent(Exception):
        """خطای ۴۰۱ ساختگی."""

        status_code = 401

    agent = build_agent(
        [make_chat_response(content="never")],
        config=config.model_copy(update={"max_retries": 3}),
        error=Permanent("Invalid API key provided"),
        error_times=99,
    )
    result = await agent.ask("hi")
    assert not result.ok and "Invalid API key" in (result.error or "")
    assert agent.client.call_count == 1


async def test_model_not_found_triggers_fallback(config: Config) -> None:
    """مدل ناشناخته → سوییچ خودکار به اولین fallback موجود."""

    class NoModel(Exception):
        """خطای «مدل یافت نشد»."""

        status_code = 404

    agent = build_agent(
        [make_chat_response(content="fallback answer")],
        config=config.model_copy(update={"model_name": "gpt-6-astra", "model_fallbacks": ["gpt-4o-mini"]}),
        error=NoModel("The model 'gpt-6-astra' does not exist"),
        error_times=1,
    )
    result = await agent.ask("hi")
    assert result.ok and result.text == "fallback answer"
    assert agent.model == "gpt-4o-mini" and result.model == "gpt-4o-mini"
    info = agent.model_info
    assert info["downgraded"] and "not available" in info["reason"]
    assert [p["model"] for p in agent.client.payloads] == ["gpt-6-astra", "gpt-4o-mini"]
    assert any(event.kind == "llm.model_fallback" for event in agent.event_bus.history)


async def test_missing_api_key_is_explained(config: Config) -> None:
    """کلید تنظیم‌نشده: پیام راهنما به‌جای stack trace."""

    class AuthError(Exception):
        """خطای احراز هویت."""

        status_code = 401

    keyless = config.model_copy(update={"openai_api_key": ""})
    agent = build_agent(
        [make_chat_response(content="x")],
        config=keyless,
        error=AuthError("Incorrect API key provided"),
        error_times=5,
    )
    result = await agent.ask("hi")
    assert not result.ok and "OPENAI_API_KEY is not configured" in (result.error or "")


async def test_empty_input_short_circuits(config: Config) -> None:
    """ورودی خالی اصلاً به مدل فرستاده نمی‌شود."""
    agent = build_agent([make_chat_response(content="x")], config=config)
    result = await agent.ask("   ")
    assert not result.ok and result.error == "empty input"
    assert agent.client.call_count == 0
    with pytest.raises(ValueError):
        await agent.ask("", raise_on_error=True)


async def test_error_can_be_raised(config: Config) -> None:
    """حالت raise_on_error برای استفاده‌های برنامه‌نویسی."""

    class Boom(Exception):
        """خطای شبکه."""

        status_code = 599

    agent = build_agent(
        [make_chat_response(content="x")], config=config, error=Boom("connection reset by peer"), error_times=99
    )
    with pytest.raises(RuntimeError, match="connection reset"):
        await agent.ask("hi", raise_on_error=True)


# ---------------------------------------------------------------------------
# تاریخچه و حالت
# ---------------------------------------------------------------------------
async def test_history_is_windowed_and_resettable(config: Config) -> None:
    """تاریخچه رشد می‌کند، محدود می‌شود و قابل پاک‌کردن است."""
    agent = build_agent(
        [make_chat_response(content="first"), make_chat_response(content="second")],
        config=config,
        max_history_messages=4,
    )
    await agent.ask("one")
    await agent.ask("two")
    assert agent.state.turn_count == 2
    assert len(agent.state.messages) <= 4
    assert [m.role for m in agent.state.messages][:2] == ["user", "assistant"]
    third = agent.client.payloads[-1]["messages"]
    assert third[0]["role"] == "system" and third[-1] == {"role": "user", "content": "two"}
    agent.reset_history()
    assert agent.state.messages == [] and agent.state.turn_count == 0
    await agent.ask("three")
    assert agent.state.turn_count == 1


async def test_history_can_be_disabled(config: Config) -> None:
    """keep_history=False → هر نوبت مستقل."""
    agent = build_agent(
        [make_chat_response(content="a"), make_chat_response(content="b")], config=config, keep_history=False
    )
    await agent.ask("one")
    await agent.ask("two")
    assert agent.state.messages == []
    assert all(len(p["messages"]) == 2 for p in agent.client.payloads)


async def test_state_export_and_load(config: Config) -> None:
    """ذخیره/بازیابی حالت گفت‌وگو."""
    agent = build_agent([make_chat_response(content="remember me")], config=config)
    await agent.ask("hello")
    payload = json.loads(agent.export_state())
    assert payload["messages"][0]["role"] == "user"
    other = build_agent([make_chat_response(content="ok")], config=config)
    other.load_state(payload)
    assert other.state.messages[0].content == "hello"
    assert isinstance(other.state, AgentState)
    with pytest.raises(ValueError):  # ValidationError subclass است؛ هیچ خطای دیگری نباید نشت کند
        other.load_state({"messages": "not-a-list"})


async def test_system_note_and_profile_prompt(config: Config) -> None:
    """یادداشت زمینه و system prompt پروفایل به مدل می‌رسند."""
    from src.models.config_models import AgentProfile

    profile = AgentProfile(name="custom", system_prompt_extra="Be terse.")
    agent = build_agent([make_chat_response(content="ok")], config=config, profile=profile, system_prompt="BASE")
    await agent.ask("hi", system_note="cwd=/tmp")
    system = agent.client.payloads[0]["messages"][0]["content"]
    assert system.startswith("BASE") and "Be terse." in system and "cwd=/tmp" in system
    assert DEFAULT_SYSTEM_PROMPT.split()[0] == "You"


# ---------------------------------------------------------------------------
# ابزارهای کمکی ایجنت
# ---------------------------------------------------------------------------
async def test_run_helper_returns_text_only(config: Config) -> None:
    """متد سازگاری ``run`` فقط متن را می‌دهد."""
    agent = build_agent([make_chat_response(content="plain text")], config=config)
    assert await agent.run("hi") == "plain text"
    broken = build_agent([make_chat_response(content="x")], config=config, error=RuntimeError("nope"), error_times=5)
    assert (await broken.run("hi")).startswith("[error]")


def test_validate_tool_arguments(config: Config) -> None:
    """اعتبارسنجی خشک ورودی ابزار (برای UI)."""
    agent = build_agent([], config=config)
    assert agent.validate_tool_arguments("terminal_run", {}) == "missing required parameter(s): command"
    assert agent.validate_tool_arguments("terminal_run", {"command": "ls"}) is None
    assert "unknown tool" in (agent.validate_tool_arguments("nope", {}) or "")


def test_tool_filtering_and_description(config: Config) -> None:
    """فیلتر ابزارها و توضیح آن‌ها."""
    agent = build_agent([], config=config, tools=["os_info"])
    assert agent.tool_names == ["os_info"]
    assert len(agent.schemas) == 1
    described = agent.describe_tools()
    assert described[0]["name"] == "os_info" and described[0]["risk_level"] == "safe"
    empty = build_agent([], config=config, tools=[])
    assert len(empty.tools) == len(ToolRegistry.names())  # None/خالی → همه‌ی ابزارها


def test_add_tool_and_close(config: Config) -> None:
    """افزودن ابزار زنده و آزادسازی منابع."""
    agent = build_agent([], config=config, tools=["os_info"])
    fake = MyTinyTool()
    agent.add_tool(fake)
    assert "my_tool" in agent.tool_names
    agent._context.session["browser.session"] = object()  # noqa: SLF001
    import asyncio

    asyncio.run(agent.close())
    assert agent._context.session == {}  # noqa: SLF001


async def test_events_are_published(config: Config) -> None:
    """رویدادهای چرخه‌ی حیات ایجنت."""
    bus = EventBus()
    agent = build_agent(
        [make_chat_response(tool_calls=[{"name": "os_info", "arguments": {}}]), make_chat_response(content="done")],
        config=config,
        event_bus=bus,
    )
    await agent.ask("collect")
    kinds = [event.kind for event in bus.history]
    for expected in (
        EVENTS.AGENT_STARTED,
        EVENTS.LLM_REQUESTED,
        EVENTS.LLM_RESPONDED,
        EVENTS.TOOL_REQUESTED,
        EVENTS.TOOL_COMPLETED,
        EVENTS.AGENT_COMPLETED,
    ):
        assert expected in kinds, f"missing event {expected} in {kinds}"
    assert bus.recent(EVENTS.TOOL_COMPLETED)[0].payload["success"] is True


async def test_failure_emits_agent_failed_event(config: Config) -> None:
    """خطای کشنده رویداد agent.failed می‌فرستد."""
    bus = EventBus()

    class Fatal(Exception):
        """خطای غیرقابل بازیابی."""

        status_code = 403

    agent = build_agent(
        [make_chat_response(content="x")],
        config=config,
        event_bus=bus,
        error=Fatal("forbidden region"),
        error_times=9,
    )
    result = await agent.ask("hi")
    assert not result.ok
    assert any(event.kind == EVENTS.AGENT_FAILED for event in bus.history)
    assert any(event.kind == EVENTS.LLM_FAILED for event in bus.history)


async def test_content_parts_and_reasoning_are_extracted(config: Config) -> None:
    """سازگاری با پاسخ‌های لیست‌بخشی‌شده/استدلال‌محور."""
    parts = make_chat_response(content=None)
    parts.choices[0].message.content = [{"type": "text", "text": "chunk one"}, {"type": "text", "text": "chunk two"}]
    agent = build_agent([parts], config=config)
    assert (await agent.ask("hi")).text == "chunk one\nchunk two"

    reasoned = make_chat_response(content=None, reasoning="thinking out loud")
    agent2 = build_agent([reasoned], config=config)
    assert (await agent2.ask("hi")).text == "thinking out loud"


async def test_response_without_choices_is_an_error(config: Config) -> None:
    """پاسخ بی‌choice خطای خوانا می‌دهد."""
    agent = build_agent([SimpleNamespace(choices=[], usage=None, model="m")], config=config)
    result = await agent.ask("hi")
    assert not result.ok and "without choices" in (result.error or "")


async def test_missing_message_payload(config: Config) -> None:
    """choice بدون message هم مدیریت می‌شود."""
    agent = build_agent(
        [SimpleNamespace(choices=[SimpleNamespace(message=None)], usage=None, model="m")], config=config
    )
    result = await agent.ask("hi")
    assert not result.ok and "without a message" in (result.error or "")


async def test_large_tool_output_is_truncated_in_messages(config: Config) -> None:
    """خروجی حجیم ابزار پیش از رفتن به مدل بریده می‌شود."""
    small = config.model_copy(update={"max_output_chars": 900})
    ToolRegistry.register(BigEchoTool, replace=True)
    agent = build_agent(
        [make_chat_response(tool_calls=[{"name": "big_echo", "arguments": {}}]), make_chat_response(content="ok")],
        config=small,
        tools=["big_echo"],
    )
    await agent.ask("give me a lot")
    tool_message = next(m for m in agent.client.payloads[1]["messages"] if m["role"] == "tool")
    assert len(tool_message["content"]) <= 950


def test_safety_summary_shape(config: Config) -> None:
    """خلاصه‌ی ایمنی برای CLI."""
    agent = build_agent([], config=config)
    summary = agent.safety_summary()
    assert summary["policy"] == "confirm"
    assert summary["allowed_directories"] == [str(config.project_root)]
    assert summary["confirmation_enabled"] is True
    assert summary["has_confirmation_channel"] is False


async def test_client_construction_uses_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """base_url سفارشی به client داده می‌شود (بدون ارسال واقعی)."""
    captured: dict[str, Any] = {}

    class _Client:
        """fake AsyncOpenAI که kwargs را ضبط می‌کند."""

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("src.agent.AsyncOpenAI", _Client)
    cfg = Config(
        openai_api_key="sk-real",
        log_file=None,
        openai_base_url="http://localhost:11434/v1",
        request_timeout=7,
        max_retries=2,
    )
    agent = UniversalAgent(cfg, tools=["os_info"])
    assert captured["base_url"] == "http://localhost:11434/v1"
    assert captured["max_retries"] == 0 and captured["timeout"] == 7
    assert agent.client is not None
    await agent.close()


async def test_message_conversion_is_compact() -> None:
    """پیام‌های خالی کلید اضافه ندارند."""
    assert Message(role="user", content="hi").to_api_dict() == {"role": "user", "content": "hi"}
    assert Message(role="tool", content="c", tool_call_id="1", name="t").to_api_dict() == {
        "role": "tool",
        "content": "c",
        "tool_call_id": "1",
        "name": "t",
    }


def test_confirmation_handler_can_be_swapped(config: Config) -> None:
    """تزریق/حذف callback تأیید در زمان اجرا."""
    agent = build_agent([], config=config)
    assert agent._context.confirm.__func__ is UniversalAgent._confirm_through_callback  # noqa: SLF001
    agent.set_confirmation_handler(None)

    async def _check() -> None:
        assert await agent._confirm_through_callback(ConfirmationRequest(tool="t", action="a")) == (
            not config.enable_confirmation
        )  # noqa: SLF001

    import asyncio

    asyncio.run(_check())
    assert agent.confirm_callback is None


def test_tool_context_defaults(config: Config) -> None:
    """مقادیر پیش‌فرض ToolContext از config یا امن."""
    ctx = ToolContext(config=config)
    assert ctx.max_output_chars == config.max_output_chars and ctx.timeout == config.max_command_timeout
    bare = ToolContext()
    assert bare.max_output_chars == 12000 and bare.timeout == 30


class MyTinyTool(BaseTool):
    """ابزار کوچکِ ثبت‌شده در زمان اجرا (برای تست add_tool)."""

    name = "my_tool"
    description = "registered at runtime"
    category = ToolCategory.CUSTOM

    async def execute(self, value: str = "", *, context: ToolContext | None = None) -> ToolResult:  # type: ignore[override]
        return ToolResult.ok(value or "tiny", tool=self.name)

    def get_schema(self) -> dict[str, Any]:
        return self.function_schema(
            self.name, self.description, required=(), properties={"value": {"type": "string", "description": "text"}}
        )


class BigEchoTool(BaseTool):
    """ابزاری که عمداً خروجی بسیار حجیم برمی‌گرداند."""

    name = "big_echo"
    description = "returns 20k characters"

    async def execute(self, *, context: ToolContext | None = None) -> ToolResult:  # type: ignore[override]
        return ToolResult.ok("x" * 20_000, tool=self.name)

    def get_schema(self) -> dict[str, Any]:
        return self.function_schema(self.name, self.description, required=(), properties={})

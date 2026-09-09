"""تست کلاس پایه‌ی ابزارها (:mod:`src.core.base_tool`).

این تست‌ها «قرارداد» ابزارها را تثبیت می‌کنند: اعتبارسنجی، ایمنی، تأیید،
زمان‌سنجی، برش خروجی و تبدیل استثنا به نتیجه‌ی خوانا.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.config import Config
from src.core.base_tool import BaseTool, ConfirmationDecision, ConfirmationRequest, ToolContext
from src.core.event_bus import Event, EventBus
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.safety import SafetyDecision, SafetyGuard
from src.utils.validators import ValidationError


class SampleTool(BaseTool):
    """ابزار تستی با همه‌ی مسیرهای ممکن."""

    name = "sample_tool"
    description = "a tool used only in tests"
    category = ToolCategory.CUSTOM
    required_parameters = ("value",)
    optional_parameters = ("size", "context")

    def __init__(
        self, config: Any = None, *, decision: SafetyDecision | None = None, raises: Exception | None = None
    ) -> None:
        super().__init__(config)
        self._decision = decision
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def execute(self, value: str, size: int = 1, *, context: ToolContext | None = None) -> ToolResult:  # type: ignore[override]
        self.calls.append({"value": value, "size": size})
        if self._raises:
            raise self._raises
        return ToolResult.ok(f"{value}×{size}", tool=self.name, metadata={"size": size})

    async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
        return self._decision or SafetyDecision(allowed=True, risk=RiskLevel.LOW)

    def get_schema(self) -> dict[str, Any]:
        return self.function_schema(
            self.name,
            self.description,
            required=("value",),
            properties={
                "value": {"type": "string", "description": "text"},
                "size": {"type": "integer", "description": "repeat count"},
            },
        )


def make_tool(**kwargs: Any) -> SampleTool:
    """ساخت ابزار تستی."""
    return SampleTool(**kwargs)


async def test_run_returns_result_with_duration_and_tool_name() -> None:
    """مسیر موفق: نام ابزار و زمان اجرا پر می‌شود."""
    tool = make_tool()
    result = await tool.run({"value": "a", "size": 3})
    assert result.success and result.data == "a×3"
    assert result.tool == "sample_tool"
    assert result.duration_ms >= 0
    assert result.metadata["size"] == 3
    assert tool.calls == [{"value": "a", "size": 3}]


async def test_missing_required_parameter_is_reported() -> None:
    """پارامتر اجباریِ گمشده → invalid_input (بدون استثنا)."""
    result = await make_tool().run({})
    assert not result.success
    assert result.error_code == "invalid_input"
    assert "value" in (result.error or "")


async def test_unknown_parameter_is_reported() -> None:
    """پارامتر ناشناخته رد می‌شود تا مدل توهم‌زایی نکند."""
    result = await make_tool().run({"value": "x", "surprise": 1})
    assert not result.success and "surprise" in (result.error or "")


async def test_execute_signature_filtering() -> None:
    """پارامترهای خارج از امضای execute قبل از اجرا فیلتر می‌شوند."""

    class Narrow(SampleTool):
        """ابزاری با execute بدون context (اما validate_input آن context را می‌پذیرد)."""

        name = "narrow_tool"
        optional_parameters = ("size", "context", "unused")

        async def execute(self, value: str, size: int = 1) -> ToolResult:  # type: ignore[override]
            return ToolResult.ok(f"{value}/{size}", tool=self.name)

    result = await Narrow().run({"value": "v", "size": 2, "unused": True})
    assert result.success and result.data == "v/2"


async def test_var_keyword_tools_receive_everything() -> None:
    """ابزارهایی که **kwargs می‌گیرند همه را دریافت می‌کنند."""

    class Loose(BaseTool):
        """ابزار منعطف."""

        name = "loose_tool"
        description = "accepts anything"

        async def execute(self, **kwargs: Any) -> ToolResult:  # type: ignore[override]
            return ToolResult.ok(sorted(kwargs), tool=self.name)

        def get_schema(self) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": {"type": "object", "properties": {}},
                },
            }

    result = await Loose().run({"b": 1, "a": 2})
    assert result.success and result.data == ["a", "b"]


async def test_raw_return_value_is_wrapped() -> None:
    """برگشت مقدار خام (غیر ToolResult) هم پذیرفته می‌شود."""

    class Raw(BaseTool):
        """ابزار با خروجی خام."""

        name = "raw_tool"
        description = "returns a bare string"

        async def execute(self, **kwargs: Any) -> str:  # type: ignore[override]
            return "just text"

        def get_schema(self) -> dict[str, Any]:
            return self.function_schema(self.name, self.description, required=(), properties={})

    result = await Raw().run({})
    assert result.success and result.data == "just text" and result.tool == "raw_tool"


async def test_exceptions_become_failed_results() -> None:
    """استثنای execute به نتیجه‌ی خطا تبدیل می‌شود."""
    tool = make_tool(raises=RuntimeError("boom"))
    result = await tool.run({"value": "x"})
    assert not result.success
    assert result.error_code == "tool_error"
    assert "boom" in (result.error or "")


async def test_safety_block_is_reported() -> None:
    """تصمیم «مسدود» نتیجه‌ی blocked می‌دهد و execute اجرا نمی‌شود."""
    tool = make_tool(decision=SafetyDecision(allowed=False, risk=RiskLevel.CRITICAL, reasons=["rm of root"]))
    result = await tool.run({"value": "x"})
    assert not result.success and result.error_code == "blocked"
    assert "rm of root" in (result.error or "")
    assert tool.calls == []


async def test_confirmation_declined_short_circuits() -> None:
    """رد کردن تأیید → declined."""
    tool = make_tool(decision=SafetyDecision(allowed=True, requires_confirmation=True, risk=RiskLevel.HIGH))
    ctx = ToolContext(config=Config(openai_api_key="sk", log_file=None), confirm=lambda request: False)
    result = await tool.run({"value": "x"}, context=ctx)
    assert not result.success and result.error_code == "declined"
    assert tool.calls == []


async def test_confirmation_approved_runs_tool() -> None:
    """تأیید کاربر → اجرا."""
    tool = make_tool(decision=SafetyDecision(allowed=True, requires_confirmation=True, risk=RiskLevel.HIGH))
    asked: list[str] = []

    def confirm(request: ConfirmationRequest) -> ConfirmationDecision:
        asked.append(request.tool)
        return ConfirmationDecision.yes()

    ctx = ToolContext(config=Config(openai_api_key="sk", log_file=None), confirm=confirm)
    result = await tool.run({"value": "x"}, context=ctx)
    assert result.success and asked == ["sample_tool"]
    assert tool.calls


async def test_async_confirmation_callback_supported() -> None:
    """callback async هم پشتیبانی می‌شود."""
    tool = make_tool(decision=SafetyDecision(allowed=True, requires_confirmation=True))

    async def confirm(request: ConfirmationRequest) -> bool:
        await asyncio.sleep(0)
        return request.details.get("value") == "yes"

    ctx = ToolContext(confirm=confirm)
    assert (await tool.run({"value": "yes"}, context=ctx)).success
    assert not (await tool.run({"value": "no"}, context=ctx)).success


async def test_confirmation_required_without_channel_is_safe() -> None:
    """ابزارِ always-confirm بدون کانال تأیید و با config سخت‌گیر: اجرا نمی‌شود."""

    class AlwaysConfirm(SampleTool):
        """ابزاری که همیشه تأیید می‌خواهد."""

        name = "always_confirm"
        requires_confirmation = True

    strict = Config(openai_api_key="sk", log_file=None, enable_confirmation=True)
    result = await AlwaysConfirm(config=strict).run({"value": "x"}, context=ToolContext(config=strict, safety=None))
    assert not result.success and result.error_code == "confirmation_unavailable"

    permissive = Config(openai_api_key="sk", log_file=None, enable_confirmation=False)
    assert (
        await AlwaysConfirm(config=permissive).run({"value": "x"}, context=ToolContext(config=permissive))
    ).success


async def test_output_is_truncated_for_huge_data() -> None:
    """خروجی رشته‌ای بلند به سقف config می‌رسد."""

    class Verbose(SampleTool):
        """ابزار پرحرف."""

        name = "verbose_tool"

        async def execute(self, value: str, size: int = 1, *, context: ToolContext | None = None) -> ToolResult:  # type: ignore[override]
            return ToolResult.ok("Z" * 5000, tool=self.name)

    cfg = Config(openai_api_key="sk", log_file=None, max_output_chars=900)
    result = await Verbose(config=cfg).run({"value": "x"}, context=ToolContext(config=cfg))
    assert result.success and len(result.data) <= 900 and result.truncated


async def test_list_output_is_capped() -> None:
    """لیست‌های بسیار بلند کوتاه می‌شوند."""

    class Listy(SampleTool):
        """ابزار لیست‌باز."""

        name = "listy_tool"

        async def execute(self, value: str, size: int = 1, *, context: ToolContext | None = None) -> ToolResult:  # type: ignore[override]
            return ToolResult.ok([f"line-{i}" for i in range(5000)], tool=self.name)

    cfg = Config(openai_api_key="sk", log_file=None, max_output_chars=900)
    result = await Listy(config=cfg).run({"value": "x"}, context=ToolContext(config=cfg))
    assert len(result.data) <= 200 and result.truncated


async def test_events_are_emitted() -> None:
    """رویدادهای درخواست/تأیید/پایان منتشر می‌شوند."""
    bus = EventBus()
    kinds: list[str] = []
    bus.subscribe("#", lambda event: kinds.append(event.kind))
    tool = make_tool(decision=SafetyDecision(allowed=True, requires_confirmation=True, risk=RiskLevel.HIGH))
    ctx = ToolContext(bus=bus, confirm=lambda request: True, config=Config(openai_api_key="sk", log_file=None))
    await tool.run({"value": "secret"}, context=ctx)
    assert kinds == ["tool.requested", "safety.confirmation_requested", "tool.approved"]


async def test_sensitive_parameters_are_masked_in_events() -> None:
    """پارامترهای حساس در رویدادها ماسک می‌شوند."""

    class Secretive(SampleTool):
        """ابزار با ورودی حساس."""

        name = "secretive_tool"
        sensitive_parameters = ("value",)

    captured: list[Event] = []
    bus = EventBus()
    bus.subscribe("tool.requested", captured.append)
    await Secretive().run({"value": "super-secret-token"}, context=ToolContext(bus=bus))
    assert captured and "super-secret-token" not in str(captured[0].payload)


async def test_registry_events_do_not_break_without_bus() -> None:
    """بدون bus هم اجرا می‌شود (bus اختیاری است)."""
    result = await make_tool().run({"value": "x"}, context=None)
    assert result.success


def test_to_info_and_repr() -> None:
    """اطلاعات نمایشی ابزار."""
    tool = make_tool()
    info = tool.to_info()
    assert info.name == "sample_tool" and info.parameters == ["value", "size", "context"]
    assert info.risk_level is RiskLevel.LOW and not info.requires_confirmation
    assert repr(tool).startswith("<SampleTool name='sample_tool'")


def test_function_schema_helper() -> None:
    """ساخت‌کننده‌ی اسکیمای استاندارد."""
    schema = SampleTool.function_schema(
        "x", "desc", required=("a",), properties={"a": {"type": "string", "description": "d"}}, strict=True
    )
    assert schema["type"] == "function"
    assert schema["function"]["strict"] is True
    assert schema["function"]["parameters"] == {
        "type": "object",
        "properties": {"a": {"type": "string", "description": "d"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    assert SampleTool.function_schema("y", "d", properties={})["function"]["parameters"]["required"] == []


def test_safety_guard_from_config_and_context_precedence(config: Config, guard: SafetyGuard) -> None:
    """اولویت guard زمینه بر guard ابزار."""
    tool = make_tool(config=config)
    assert tool._safety_guard is not None  # noqa: SLF001
    assert tool.safety_guard(ToolContext(safety=guard)) is guard
    assert tool.safety_guard() is tool._safety_guard  # noqa: SLF001
    assert make_tool().safety_guard() is None  # بدون config → بدون guard


def test_broken_config_does_not_break_construction() -> None:
    """config بی‌شکل باعث استثنا در سازنده نمی‌شود."""

    class Weird:
        """هر چیزی جز config."""

        dangerous_command_policy = object()
        allowed_directories = object()
        unrestricted_filesystem = object()
        project_root = object()
        allow_shell = object()

    tool = make_tool(config=Weird())
    assert tool._safety_guard is None  # noqa: SLF001


def test_base_tool_cannot_be_instantiated() -> None:
    """کلاس پایه‌ی انتزاعی قابل ساخت نیست."""
    with pytest.raises(TypeError):
        BaseTool()  # type: ignore[abstract]


async def test_before_execute_hook_can_mutate_arguments() -> None:
    """هوک before_execute می‌تواند ورودی را نرمال‌سازی کند."""

    class Normalizing(SampleTool):
        """ابزار نرمال‌ساز."""

        name = "normalizing_tool"

        async def before_execute(self, kwargs: dict[str, Any], context: ToolContext | None) -> dict[str, Any]:
            kwargs["value"] = str(kwargs.get("value", "")).strip().lower()
            return kwargs

    result = await Normalizing().run({"value": "  MIXED Case  "})
    assert result.data == "mixed case×1"


def test_validate_input_can_be_extended_by_subclass() -> None:
    """override کردن validate_input در subclass."""

    class Strict(SampleTool):
        """ابزار سخت‌گیر."""

        name = "strict_tool"

        def validate_input(self, **kwargs: Any) -> bool:
            super().validate_input(**kwargs)
            if kwargs.get("size", 1) > 5:
                raise ValidationError("size must be <= 5 for this tool")
            return True

    assert asyncio.run(Strict().run({"value": "a", "size": 2})).success
    denied = asyncio.run(Strict().run({"value": "a", "size": 9}))
    assert not denied.success and "5" in (denied.error or "")


def test_tool_result_helpers() -> None:
    """میان‌برهای ToolResult و تبدیل به متن برای مدل."""
    ok = ToolResult.ok({"a": 1}, tool="t")
    assert ok.success and ok.data == {"a": 1} and ok.error is None
    failed = ToolResult.fail("nope", tool="t", error_code="x")
    assert not failed.success and failed.error_code == "x"
    assert '"error": "nope"' in failed.to_llm_string()
    assert ok.to_llm_string().startswith('{"data"')
    empty = ToolResult(success=True, data="")
    assert empty.to_llm_string() == "(no output)"
    long = ToolResult(success=True, data="q" * 500)
    assert len(long.to_llm_string(max_chars=100)) <= 120 and long.truncated

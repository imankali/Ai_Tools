"""ابزارهای ساختگی برای تست (fakes).

این ماژول جانشین صدا زدن واقعی API است: حلقه‌ی ایجنت را بدون شبکه، بدون کلید
و به‌صورت کاملاً قطعی تست می‌کنیم.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

__all__ = ["FakeCompletions", "FakeOpenAIClient", "FakeTool", "make_chat_response"]


def make_chat_response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    model: str = "gpt-6-astra",
    reasoning: str | None = None,
) -> SimpleNamespace:
    """ساخت پاسخ chat.completions ساختگی.

    Args:
        content: متن پاسخ (می‌تواند None باشد).
        tool_calls: فهرست ``{"id":…, "name":…, "arguments": {…}|str}``.
        usage: dict مصرف توکن (``prompt_tokens`` و…).
        model: نام مدل در پاسخ.
        reasoning: برخی سرویس‌ها به‌جای content، reasoning_content می‌دهند.

    Returns:
        ساختاری مشابه پاسخ کتابخانه‌ی openai (دسترسی attribute-محور).
    """
    calls: list[SimpleNamespace] | None = None
    if tool_calls:
        calls = [
            SimpleNamespace(
                id=str(item.get("id") or f"call_{index}"),
                type="function",
                function=SimpleNamespace(
                    name=str(item["name"]),
                    arguments=(
                        item["arguments"]
                        if isinstance(item.get("arguments"), str)
                        else json.dumps(item.get("arguments") or {}, ensure_ascii=False)
                    ),
                ),
            )
            for index, item in enumerate(tool_calls)
        ]
    message = SimpleNamespace(content=content, tool_calls=calls, role="assistant", reasoning_content=reasoning)
    choice = SimpleNamespace(message=message, index=0, finish_reason="stop" if content else "tool_calls")
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(**usage) if usage else None,
        model=model,
        id="chatcmpl-test",
    )


class FakeCompletions:
    """جای‌نمای ``client.chat.completions`` با ثبت درخواست‌ها."""

    def __init__(
        self,
        responses: list[Any],
        *,
        error: BaseException | None = None,
        error_times: int = 1,
        on_create: Any | None = None,
    ) -> None:
        """
        Args:
            responses: پاسخ‌های ترتیبی (آخرین پاسخ تکرار می‌شود).
            error: استثنای تزریق‌شده برای تست retry/fallback.
            error_times: چند فراخوانی اول باید خطا بدهند.
            on_create: callback ``(kwargs) -> None`` برای بررسی درخواست‌ها.
        """
        self.responses = list(responses)
        self.payloads: list[dict[str, Any]] = []
        self.error = error
        self.error_times = error_times
        self._on_create = on_create

    async def create(self, **kwargs: Any) -> Any:
        """ثبت درخواست، (اختیاراً) raise خطا و برگرداندن پاسخ بعدی."""
        self.payloads.append(dict(kwargs))
        if self._on_create is not None:
            self._on_create(kwargs)
        if self.error is not None and len(self.payloads) <= self.error_times:
            raise self.error
        index = len(self.payloads) - 1
        return self.responses[index] if index < len(self.responses) else self.responses[-1]


class FakeOpenAIClient:
    """client ساختگیِ سازگار با ``AsyncOpenAI`` (فقط chat.completions)."""

    def __init__(
        self,
        responses: list[Any],
        *,
        error: BaseException | None = None,
        error_times: int = 1,
        on_create: Any | None = None,
    ) -> None:
        self.completions = FakeCompletions(responses, error=error, error_times=error_times, on_create=on_create)
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def payloads(self) -> list[dict[str, Any]]:
        """درخواست‌های ارسال‌شده (برای assert روی tools/messages/model)."""
        return self.completions.payloads

    @property
    def call_count(self) -> int:
        """تعداد فراخوانی‌های انجام‌شده."""
        return len(self.payloads)


class FakeTool:
    """ابزار ساختگیِ سازگار با ToolRegistry (برای تست حلقه‌ی ایجنت)."""

    def __init__(
        self, name: str = "fake_tool", *, result: Any = "fake-output", requires_confirmation: bool = False
    ) -> None:
        self.name = name
        self.description = "test double"
        self.category = SimpleNamespace(value="custom")
        self.requires_confirmation = requires_confirmation
        self.calls: list[dict[str, Any]] = []
        self._result = result
        self._context: Any = None

    async def run(self, arguments: dict[str, Any], *, context: Any = None, call_id: str = "") -> Any:
        """ثبت فراخوانی و برگرداندن نتیجه‌ی از پیش تعیین‌شده."""
        from src.models.tool_models import ToolResult

        self.calls.append(dict(arguments))
        self._context = context
        if isinstance(self._result, ToolResult):
            return self._result
        return ToolResult.ok(self._result, tool=self.name, metadata={"call_id": call_id})

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای ساده."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string", "description": "x"}},
                    "required": [],
                },
            },
        }

    def to_info(self) -> Any:
        """اطلاعات نمایشی."""
        from src.models.tool_models import ToolInfo

        return ToolInfo(name=self.name, description=self.description)

    def close(self) -> None:  # pragma: no cover - ساده
        """بستن منابع (برای تست close ایجنت)."""
        self.calls.clear()

"""مدل‌های داده‌ای مرتبط با ایجنت و گفت‌وگو (Agent data models).

این مدل‌ها «حالت» ایجنت را توصیف می‌کنند: پیام‌ها، فراخوانی‌های ابزار،
نتیجه‌ی یک اجرا و رویدادهای قابل مشاهده برای UI/لاگ.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.models.tool_models import ToolResult

__all__ = [
    "AgentEvent",
    "AgentRunResult",
    "AgentState",
    "Message",
    "ToolCallRecord",
    "UsageStats",
]

MessageRole = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """یک پیام در تاریخچه‌ی گفت‌وگو.

    ساختار سازگار با API اتری OpenAI (chat.completions) است تا مستقیماً
    بتوان آن را به صورت dict به سرویس ارسال کرد.
    """

    role: MessageRole
    content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    def to_api_dict(self) -> dict[str, Any]:
        """تبدیل به dict قابل ارسال به API (بدون فیلدهای خالی)."""
        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = self.content
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        return payload


class UsageStats(BaseModel):
    """مصرف توکن در یک اجرا."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: UsageStats | None) -> UsageStats:
        """جمع آماری با نتیجه‌ی یک فراخوانی دیگر."""
        if other is None:
            return self.model_copy()
        return UsageStats(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    @classmethod
    def from_api(cls, usage: Any) -> UsageStats:
        """ساخت آمار از آبجکت ``usage`` کتابخانه‌ی openai (یا dict)."""
        if usage is None:
            return cls()
        if isinstance(usage, dict):
            return cls(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            )
        return cls(
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        )


class ToolCallRecord(BaseModel):
    """رکورد یک فراخوانی ابزار (برای تاریخچه، لاگ و تست)."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: ToolResult | None = None
    started_at: float = Field(default_factory=time.time)
    duration_ms: int = 0
    approved: bool | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """آیا فراخوانی با موفقیت کامل شد؟"""
        return self.result is not None and self.result.success


class AgentRunResult(BaseModel):
    """خروجی کامل یک اجرا از ایجنت.

    Attributes:
        text: پاسخ نهایی مدل.
        iterations: تعداد دورهای گفت‌وگو با مدل.
        tool_calls: فهرست فراخوانی‌های انجام‌شده.
        usage: مجموع مصرف توکن.
        duration_ms: زمان کل اجرا.
        model: مدلی که واقعاً استفاده شد (ممکن است fallback باشد).
        ok: پرچم موفقیت کلی.
        error: خطای سطح‌بالا در صورت بروز مشکل.
    """

    text: str = ""
    iterations: int = 0
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    usage: UsageStats = Field(default_factory=UsageStats)
    duration_ms: int = 0
    model: str = ""
    ok: bool = True
    error: str | None = None
    events: list[AgentEvent] = Field(default_factory=list)

    @property
    def tool_names(self) -> list[str]:
        """نام ابزارهای استفاده‌شده (بدون تکرار، به همان ترتیب)."""
        seen: list[str] = []
        for record in self.tool_calls:
            if record.tool not in seen:
                seen.append(record.tool)
        return seen

    @property
    def failed_tools(self) -> list[str]:
        """ابزارهایی که خطا دادند."""
        return [record.tool for record in self.tool_calls if not record.succeeded]


class AgentState(BaseModel):
    """حالت قابل serialize ایجنت (برای resume یا ذخیره‌ی گفت‌وگو).

    Attributes:
        messages: تاریخچه‌ی پیام‌ها.
        turn_count: تعداد نوبت‌های کاربر.
        active_profile: نام پروفایل جاری.
        confirmations_granted: تعداد تأییدهای داده‌شده در این session.
    """

    messages: list[Message] = Field(default_factory=list)
    turn_count: int = 0
    active_profile: str = "generalist"
    confirmations_granted: int = 0

    def clear(self) -> None:
        """پاک کردن تاریخچه (حالت تازه)."""
        self.messages = []
        self.turn_count = 0
        self.confirmations_granted = 0

    def to_json(self) -> str:
        """سریال‌سازی برای ذخیره روی دیسک."""
        return self.model_dump_json(indent=2)


class AgentEvent(BaseModel):
    """رویداد قابل مشاهده در EventBus (Observer pattern).

    نوع‌های رایج: ``agent.started``, ``agent.completed``, ``tool.requested``,
    ``tool.approved``, ``tool.denied``, ``tool.completed``, ``tool.failed``,
    ``error``.
    """

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)

    @property
    def elapsed_since(self) -> float:
        """ثانیه‌های سپری‌شده از وقوع رویداد."""
        return max(0.0, time.time() - self.timestamp)

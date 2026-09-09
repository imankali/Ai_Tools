"""مدل‌های داده‌ای مربوط به ابزارها (Tool data models).

این ماژول قرارداد مشترک ورودی/خروجی بین «ایجنت» و «ابزارها» را تعریف می‌کند:
هر ابزاری باید یک :class:`ToolResult` برگرداند و اسکیمای OpenAI Function
Calling خود را به‌صورت یک dict استاندارد ارائه دهد.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "RiskLevel",
    "ToolCall",
    "ToolCategory",
    "ToolInfo",
    "ToolResult",
]


class ToolCategory(str, Enum):
    """دسته‌بندی ابزارها (برای فیلتر در CLI و فکتوری)."""

    SYSTEM = "system"
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    BROWSER = "browser"
    DEVELOPER = "developer"
    CUSTOM = "custom"


class RiskLevel(str, Enum):
    """سطح ریسک یک عملیات."""

    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolResult(BaseModel):
    """نتیجه‌ی اجرای یک ابزار.

    Attributes:
        success: آیا ابزار بدون خطا اجرا شد.
        data: خروجی واقعی (رشته، dict، لیست و...).
        error: پیام خطا در صورت شکست.
        error_code: کد خطای ماشین‌خوان (مثل ``blocked``, ``timeout``).
        metadata: اطلاعات جانبی (مدت اجرا، مسیر، تعداد رکورد و...).
        tool: نام ابزاری که نتیجه را تولید کرده.
        duration_ms: زمان اجرا به میلی‌ثانیه.
        truncated: آیا خروجی بریده شده است.
        requires_confirmation: اگر True باشد، نتیجه منتظر تأیید کاربر است.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool = True
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool: str = ""
    duration_ms: int = 0
    truncated: bool = False
    requires_confirmation: bool = False

    @classmethod
    def ok(
        cls,
        data: Any = None,
        *,
        tool: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        """ساخت نتیجه‌ی موفق (میان‌بر)."""
        return cls(success=True, data=data, tool=tool, metadata=metadata or {})

    @classmethod
    def fail(
        cls,
        error: str,
        *,
        tool: str = "",
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        """ساخت نتیجه‌ی ناموفق (میان‌بر)."""
        return cls(
            success=False,
            error=error,
            error_code=error_code,
            tool=tool,
            metadata=metadata or {},
        )

    def to_llm_string(self, max_chars: int = 8000) -> str:
        """متناسب‌سازی خروجی برای تزریق مجدد به مدل زبانی.

        مدل‌های زبانی با رشته بهتر از اشیاء سفارشی کار می‌کنند؛ بنابراین
        نتیجه به JSON فشرده یا رشته تبدیل و در نهایت کوتاه می‌شود.
        """
        from src.utils.helpers import truncate_text  # local import (چرخه‌ی وارد نکردن)

        if not self.success:
            payload = {"error": self.error, "error_code": self.error_code, "metadata": self.metadata}
            text = _safe_json(payload)
        elif isinstance(self.data, str):
            text = self.data
        else:
            text = _safe_json({"data": self.data, "metadata": self.metadata})

        if not text.strip():
            text = "(no output)"
        shortened, was_cut = truncate_text(text, max_chars)
        if was_cut:
            self.truncated = True
        return shortened


class ToolCall(BaseModel):
    """یک درخواست فراخوانی ابزار از سمت مدل زبانی."""

    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""


class ToolInfo(BaseModel):
    """اطلاعات توصیفی یک ابزار ثبت‌شده (برای CLI و docs)."""

    name: str
    description: str
    category: ToolCategory = ToolCategory.CUSTOM
    requires_confirmation: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    parameters: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    enabled: bool = True


def _safe_json(payload: Any) -> str:
    """سریال‌سازی JSON با افتادن به ``str`` برای انواع غیرقابل سریال."""
    import json

    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - محافظتی
        return str(payload)

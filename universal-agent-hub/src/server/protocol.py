"""مدل‌های ورودی/خروجی HTTP سرور (قرارداد رسمی اپ‌ها).

قرارداد کلی:

* همه‌ی مسیرها زیر ``/api`` هستند و JSON می‌گیرند/می‌دهند.
* احراز: هدر ``Authorization: Bearer <SERVER_TOKEN>`` (یا ``?token=`` برای WebSocket).
* شناسه‌ی session با هدر ``X-Agent-Session`` یا فیلد ``session_id`` در body.
* پاسخ خطا همیشه ``{"error": "...", "error_code": "..."}`` با status مناسب است.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "ApiKeyRequest",
    "ApprovalDecision",
    "ErrorResponse",
    "HealthResponse",
    "MemoryNoteRequest",
    "RunRequest",
    "SessionUpdate",
    "ToolInvokeRequest",
    "WsMessage",
]


def _clean_text(value: Any, *, limit: int = 200) -> str:
    """trim + برش ملایم (ورودی اپ هیچ‌وقت بی‌سقف نیست)."""
    return str(value if value is not None else "").strip()[:limit]


class RunRequest(BaseModel):
    """بدنه‌ی ``POST /api/run``."""

    prompt: str
    session_id: str = ""
    profile: str | None = None
    tools: list[str] | None = None
    system_note: str | None = None
    timeout: int | None = Field(default=None, ge=1, le=3600)
    new_session: bool = False

    @field_validator("prompt")
    @classmethod
    def _require_prompt(cls, value: str) -> str:
        """پرامپت خالی مجاز نیست (اپ باید پیام را از قبل trim کند)."""
        text = _clean_text(value, limit=32_000)
        if not text:
            raise ValueError("prompt must not be empty")
        return text


class ApprovalDecision(BaseModel):
    """پاسخ کاربر به یک درخواست تأیید."""

    request_id: str
    approved: bool
    reason: str = ""

    @field_validator("request_id")
    @classmethod
    def _require_id(cls, value: str) -> str:
        """شناسه‌ی درخواست لازم است."""
        text = _clean_text(value, limit=64)
        if not text:
            raise ValueError("request_id must not be empty")
        return text


class SessionUpdate(BaseModel):
    """تغییرات مجاز یک session (پروفایل، ابزارها و چند کلید config)."""

    profile: str | None = None
    tools: list[str] | None = None
    system_note: str | None = None
    model_name: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tool_iterations: int | None = Field(default=None, ge=1, le=40)
    max_output_tokens: int | None = Field(default=None, ge=64, le=32000)
    enable_confirmation: bool | None = None
    parallel_tool_calls: bool | None = None
    max_output_chars: int | None = Field(default=None, ge=500)
    dangerous_command_policy: str | None = None

    def updates(self) -> dict[str, Any]:
        """فقط فیلدهایی که کاربر واقعاً فرستاده است."""
        return dict(self.model_dump(exclude_none=True))


class ToolInvokeRequest(BaseModel):
    """فراخوانی مستقیم یک ابزار (پنل مدیریت/تست)."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str = ""

    @field_validator("tool")
    @classmethod
    def _require_tool(cls, value: str) -> str:
        """نام ابزار لازم است."""
        text = _clean_text(value, limit=64)
        if not text:
            raise ValueError("tool must not be empty")
        return text


class MemoryNoteRequest(BaseModel):
    """ثبت یک یادداشت در حافظه‌ی بلندمدت (``POST /api/memory``).

    پنل مدیریت/اپ از همین مسیر برای «یادداشت دادن به ایجنت» استفاده می‌کند؛ متن
    پیش از ذخیره redact می‌شود، پس فرستادن کلید API بی‌خطر است ولی بی‌فایده.
    """

    content: str = Field(min_length=1, max_length=4000)
    kind: str = "note"
    tags: list[str] = Field(default_factory=list, max_length=12)
    pin: bool = False

    @field_validator("content")
    @classmethod
    def _require_content(cls, value: str) -> str:
        """حداقل یک کلمه‌ی واقعی (نه فاصله/کاراکتر کنترل)."""
        text = _clean_text(value, limit=4000)
        if not text:
            raise ValueError("content must not be empty")
        return text

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: Any) -> str:
        """kind ناشناخته به ``note`` تبدیل می‌شود (مخزن نباید با typo بشکند)."""
        from src.core.memory import MEMORY_KINDS

        text = str(value or "note").strip().lower()
        return text if text in MEMORY_KINDS else "note"

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        """برچسب‌ها کوتاه و بدون فاصله‌ی اضافی."""
        return [str(tag).strip()[:40] for tag in value if str(tag).strip()][:12]


class ApiKeyRequest(BaseModel):
    """ساخت/به‌روزرسانی یک پروفایل کلید API."""

    name: str
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    fallbacks: list[str] | None = None
    profile: str | None = None
    activate: bool = False

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        """نام پروفایل لازم است و کاراکتر کنترلی نداشته باشد."""
        text = _clean_text(value, limit=64)
        if not text or any(char in text for char in "\r\n\t/\\"):
            raise ValueError("name must be a short label without slashes")
        return text


class WsMessage(BaseModel):
    """پیام ورودی WebSocket (kind + بدنه‌ی باز)."""

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _require_kind(cls, value: str) -> str:
        """kind خالی مجاز نیست."""
        text = _clean_text(value, limit=40)
        if not text:
            raise ValueError("kind must not be empty")
        return text


class ErrorResponse(BaseModel):
    """ساختار یکنواخت خطا (اپ فقط همین را parse می‌کند)."""

    error: str
    error_code: str = "error"
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """پاسخ ``GET /healthz`` (بدون احراز)."""

    status: str = "ok"
    version: str = ""
    model: str = ""
    tools: int = 0
    auth_required: bool = True
    profiles: list[str] = Field(default_factory=list)

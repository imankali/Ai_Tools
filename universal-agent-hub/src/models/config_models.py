"""مدل‌های پیکربندی تکمیلی (Supplementary configuration models).

:class:`src.config.Config` تنظیمات اصلی را از محیط می‌خواند؛ این ماژول
ساختارهای ظریف‌تری برای «پروفایل‌های ایجنت» و «تنظیمات ایمنی» ارائه می‌دهد
که می‌توان آن‌ها را در فایل JSON هم ذخیره و بارگذاری کرد.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.models.tool_models import RiskLevel, ToolCategory

__all__ = ["PROFILE_FILE_SUFFIXES", "AgentProfile", "SafetySettings", "load_profile_file"]

#: پسوند‌های مجاز برای فایل پروفایل
PROFILE_FILE_SUFFIXES: tuple[str, ...] = (".json", ".yaml", ".yml")


class SafetySettings(BaseModel):
    """تنظیمات ایمنی قابل ذخیره در فایل.

    Attributes:
        blocked_commands: الگوهای دستوری که همیشه مسدودند.
        confirm_commands: الگوهای دستوری که نیازمند تأیید کاربرند.
        blocked_paths: الگوهای glob برای مسیرهای ممنوع.
        read_only_paths: مسیرهایی که فقط خواندنی‌اند (حذف/نوشتن ممنوع).
        max_input_bytes: حداکثر حجم مجاز برای نوشتن فایل.
    """

    blocked_commands: list[str] = Field(default_factory=list)
    confirm_commands: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)
    read_only_paths: list[str] = Field(default_factory=list)
    max_input_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    network_allowed: bool = True
    max_risk_level: RiskLevel = RiskLevel.HIGH

    @field_validator("max_risk_level", mode="before")
    @classmethod
    def _coerce_risk(cls, value: Any) -> Any:
        """پذیرش مقدار رشته‌ای مثل ``"high"``."""
        if isinstance(value, str):
            return RiskLevel(value.strip().lower())
        return value


class AgentProfile(BaseModel):
    """پروفایل آماده‌ی اجرا (Pre-baked agent configuration).

    پروفایل مشخص می‌کند کدام ابزارها فعال باشند، مدل چه باشد و سطح ایمنی
    چقدر سخت‌گیرانه باشد. فکتوری (:class:`src.core.agent_factory.AgentFactory`)
    از همین ساختار برای ساخت ایجنت استفاده می‌کند.
    """

    name: str = "generalist"
    description: str = "ایجنت عمومی با همه‌ی ابزارها"
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    system_prompt_extra: str = ""
    enabled_tools: list[str] = Field(default_factory=list)
    disabled_tools: list[str] = Field(default_factory=list)
    #: فیلتر دسته‌ای (وقتی نام تک‌تک ابزارها را نمی‌دانید)
    categories: list[ToolCategory] = Field(default_factory=list)
    max_tool_iterations: int | None = Field(default=None, ge=1, le=40)
    auto_confirm_all: bool = False
    safety: SafetySettings = Field(default_factory=SafetySettings)

    @field_validator("enabled_tools", "disabled_tools", mode="before")
    @classmethod
    def _to_list(cls, value: Any) -> Any:
        """پذیرش رشته‌ی جدا‌شده با کاما."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def should_include(self, tool_name: str) -> bool:
        """آیا ابزار ``tool_name`` در این پروفایل فعال است؟"""
        if self.enabled_tools:
            return tool_name in self.enabled_tools
        return tool_name not in self.disabled_tools


def load_profile_file(path: str | Path) -> AgentProfile:
    """بارگذاری پروفایل از فایل JSON (و YAML در صورت نصب بودن ``pyyaml``).

    Args:
        path: مسیر فایل پروفایل.

    Returns:
        نمونه‌ی اعتبارسنجی‌شده‌ی :class:`AgentProfile`.

    Raises:
        FileNotFoundError: فایل وجود ندارد.
        ValueError: پسوند نامعتبر، محتوای نامعتبر یا وابستگی YAML نصب نیست.
    """
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise FileNotFoundError(f"Profile file not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix not in PROFILE_FILE_SUFFIXES:
        raise ValueError(f"Unsupported profile extension '{suffix}' (use {PROFILE_FILE_SUFFIXES})")

    raw = file_path.read_text(encoding="utf-8")
    payload: dict[str, Any]
    if suffix == ".json":
        import json

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - پیام خطای دقیق‌تر
            raise ValueError(f"Invalid JSON in {file_path.name}: {exc}") from exc
    else:  # pragma: no cover - مسیر اختیاری
        try:
            import yaml
        except ImportError as exc:
            raise ValueError("YAML profiles require the optional 'pyyaml' dependency") from exc
        loaded = yaml.safe_load(raw) or {}
        payload = loaded if isinstance(loaded, dict) else {}

    if not isinstance(payload, dict):
        raise ValueError(f"Profile root must be an object/mapping, got {type(payload).__name__}")

    return AgentProfile.model_validate(payload)

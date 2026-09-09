"""قالب ساخت ابزار جدید (Copy this file to add a tool).

راهنمای ۶ مرحله‌ای
------------------
1. این فایل را در ``src/tools/my_tool.py`` کپی کنید و نام کلاس/فایل را عوض کنید.
2. ``name`` یک‌کلمه‌ای و یکتا بگذارید (همان چیزی که مدل صدا می‌زند) و
   ``description`` را دقیق و عملیاتی بنویسید — کیفیت انتخاب ابزار به آن وابسته است.
3. ``required_parameters`` / ``optional_parameters`` را ست کنید؛ اعتبارسنجی اولیه
   خودکار انجام می‌شود (ورودی ناشناخته رد می‌شود).
4. فقط :meth:`execute` را بنویسید و :class:`ToolResult` برگردانید.
   **استثنا نیندازید**؛ خطا را در ``ToolResult.fail`` بپیچید.
5. اگر ابزار تغییر دهنده است: ``requires_confirmation = True`` و/یا
   :meth:`safety_check` را override کنید.
6. :class:`MyTool.get_schema` را به‌روز کنید و در ``tests/test_tools/test_my_tool.py``
   یک تست بنویسید (رجیستری خودش ابزار را پیدا می‌کند؛ ثبت دستی لازم نیست).

نکته: این فایل عمداً ``@register_tool`` ندارد تا اسکیمای نمونه به مدل نرود؛
هنگام کپی کردن، decorator را اضافه کنید.
"""

from __future__ import annotations

from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.safety import SafetyDecision
from src.utils.validators import ValidationError, bounded_int

__all__ = ["NewTool"]


class NewTool(BaseTool):
    """ابزار نمونه: پارامتر اول اجباری، پارامتر دوم اختیاری و محدود."""

    # 1) هویت ابزار -----------------------------------------------------
    name: ClassVar[str] = "new_tool"
    description: ClassVar[str] = "One clear sentence about what this tool does and when to use it."
    category: ClassVar[ToolCategory] = ToolCategory.CUSTOM
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    requires_confirmation: ClassVar[bool] = False

    # 2) قرارداد ورودی --------------------------------------------------
    required_parameters: ClassVar[tuple[str, ...]] = ("param1",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("param2",)
    sensitive_parameters: ClassVar[tuple[str, ...]] = ()

    # 3) اعتبارسنجی (اختیاری) -------------------------------------------
    def validate_input(self, **kwargs: Any) -> bool:
        """ساختار ورودی را چک می‌کند؛ پیام خطا مستقیماً به مدل می‌رود."""
        super().validate_input(**kwargs)
        bounded_int(kwargs.get("param2"), name="param2", minimum=1, maximum=1000, default=10)
        return True

    # 4) ایمنی (اختیاری) ------------------------------------------------
    async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
        """اگر عملیات حساس است، اینجا تصمیم بگیرید (مثلاً بر اساس مسیر یا دامنه)."""
        return SafetyDecision(
            allowed=True, risk=self.risk_level, requires_confirmation=False, reasons=["sample tool is safe"]
        )

    # 5) منطق اصلی ------------------------------------------------------
    async def execute(self, param1: str, param2: int = 10, *, context: ToolContext | None = None) -> ToolResult:  # type: ignore[override]
        """کار واقعی ابزار.

        Args:
            param1: توضیح پارامتر ۱ (اجباری).
            param2: توضیح پارامتر ۲ (اختیاری، ۱ تا ۱۰۰۰).
            context: زمینه‌ی اجرا (config/safety/bus/session) — اختیاری و فقط برای
                ابزارهایی که به آن نیاز دارند.

        Returns:
            نتیجه‌ی اجرا با data قابل خواندن برای مدل.
        """
        try:
            text = str(param1 or "").strip()
            if not text:
                raise ValidationError("param1 must not be empty")
            result = {"echo": text, "length": len(text), "repeat": int(param2)}
            return ToolResult.ok(result, tool=self.name)
        except ValidationError as exc:
            return ToolResult.fail(str(exc), tool=self.name, error_code="invalid_input")
        except Exception as exc:  # noqa: BLE001 - خطای ابزار نباید ایجنت را متوقف کند
            return ToolResult.fail(f"{type(exc).__name__}: {exc}", tool=self.name, error_code="tool_error")

    # 6) اسکیمای OpenAI -------------------------------------------------
    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling؛ description هر پارامتر مهم است."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("param1",),
            properties={
                "param1": {"type": "string", "description": "Explain what to put here (include a short example)."},
                "param2": {
                    "type": "integer",
                    "description": "Optional number between 1 and 1000 (default 10).",
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
        )

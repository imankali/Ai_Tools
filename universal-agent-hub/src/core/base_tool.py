"""کلاس پایه‌ی تمام ابزارها (Base tool) — پیاده‌سازی **Strategy Pattern**.

قرارداد پروژه:

* هر ابزار یک کلاس مستقل است که از :class:`BaseTool` ارث می‌برد.
* منطق واقعی فقط در :meth:`BaseTool.execute` نوشته می‌شود.
* :meth:`BaseTool.run` (که ایجنت آن را صدا می‌زند) کارهای عرضی را انجام می‌دهد:
  اعتبارسنجی ورودی، بررسی ایمنی، تأیید کاربر، زمان‌سنجی، انتشار رویداد و
  تبدیل استثنا به :class:`ToolResult`.

این «wrapper واحد» باعث می‌شود افزودن ابزار جدید سریع و ایمن باشد و هیچ ابزاری
نتواند از مسیر ایمنی/لاگ فرار کند.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from src.models.tool_models import RiskLevel, ToolCategory, ToolInfo, ToolResult
from src.utils.helpers import Timer, format_size, truncate_text
from src.utils.safety import SafetyDecision, SafetyGuard
from src.utils.validators import ValidationError, clean_text

__all__ = ["BaseTool", "ConfirmationDecision", "ConfirmationRequest", "ToolContext", "ToolResult"]

#: امضای callback تأیید: درخواست → نتیجه‌ی تصمیم کاربر
ConfirmationCallback = Callable[["ConfirmationRequest"], "Awaitable[bool] | bool"]


class ConfirmationRequest(BaseModel):
    """درخواست تأیید که ایجنت به کاربر نشان می‌دهد.

    Attributes:
        tool: نام ابزار.
        action: شرح کوتاه عملیات (مثلاً ``terminal.run``).
        summary: خلاصه‌ی یک‌خطیِ کاری که قرار است انجام شود.
        details: داده‌های تکمیلی (دستور، مسیر، URL …).
        risk: سطح ریسک تشخیص‌داده‌شده.
        call_id: شناسه‌ی فراخوانی (برای هم‌ترازی با tool_calls مدل).
    """

    tool: str
    action: str
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.MEDIUM
    call_id: str = ""


class ConfirmationDecision(BaseModel):
    """پاسخ کاربر به یک درخواست تأیید."""

    approved: bool
    reason: str = ""

    @classmethod
    def yes(cls) -> ConfirmationDecision:
        """تأیید مثبت."""
        return cls(approved=True, reason="user-approved")

    @classmethod
    def no(cls, reason: str = "user-declined") -> ConfirmationDecision:
        """رد کردن درخواست."""
        return cls(approved=False, reason=reason)


@dataclass
class ToolContext:
    """زمینه‌ی اجرای ابزار (Dependency Injection).

    ابزارها هیچ‌وقت Config را مستقیم import نمی‌کنند؛ همه‌چیز از طریق این
    object تزریق می‌شود تا تست واحد ساده بماند.

    Attributes:
        config: نمونه‌ی تنظیمات (یا هر آبجکت سازگار با آن: duck typing).
        safety: نگهبان ایمنی.
        bus: ایونت‌باس پروژه.
        confirm: callback تأیید کاربر (ممکن است None باشد).
        session: دیکشنری مشترک برای caching در طول یک اجرا (مثلاً session مرورگر).
    """

    config: Any = None
    safety: SafetyGuard | None = None
    bus: Any = None
    confirm: ConfirmationCallback | None = None
    session: dict[str, Any] = field(default_factory=dict)

    @property
    def max_output_chars(self) -> int:
        """حداکثر کاراکتر خروجی مجاز برای جلوگیری از ترکیدن context مدل."""
        return int(getattr(self.config, "max_output_chars", 12000))

    @property
    def timeout(self) -> int:
        """Timeout پیش‌فرض عملیات‌های I/O (ثانیه)."""
        return int(getattr(self.config, "max_command_timeout", 30))


class BaseTool(ABC):
    """پایه‌ی انتزاعی ابزارها.

    Attributes:
        name: شناسه‌ی یکتای ابزار (همان چیزی که مدل زبانی صدا می‌زند).
        description: توضیحی که به مدل داده می‌شود؛ هرچه دقیق‌تر، انتخاب بهتر.
        requires_confirmation: آیا اجرا همیشه باید تأیید شود.
        category: دسته‌بندی برای فیلتر/نمایش.
        risk_level: ریسک ذاتی ابزار (کمک‌کننده به تصمیم تأیید).
        required_parameters: پارامترهای اجباری برای validate_input.
        optional_parameters: پارامترهای اختیاری (ورودی ناشناخته رد می‌شود).
    """

    name: ClassVar[str] = "base_tool"
    description: ClassVar[str] = "Abstract base tool"
    requires_confirmation: ClassVar[bool] = False
    category: ClassVar[ToolCategory] = ToolCategory.CUSTOM
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    version: ClassVar[str] = "1.0.0"
    required_parameters: ClassVar[Sequence[str]] = ()
    optional_parameters: ClassVar[Sequence[str]] = ()
    #: پارامترهایی که در لاگ/تاریخچه ماسک می‌شوند (شعار، توکن و…)
    sensitive_parameters: ClassVar[Sequence[str]] = ()
    #: نوع بازگشتی برای مدل: "text" یا "structured"
    output_mode: ClassVar[str] = "text"

    def __init__(self, config: Any = None) -> None:
        """ساخت ابزار (اختیاری: تزریق config برای استفاده‌های ساده).

        برای تست می‌توان config را فراموش کرد؛ مقادیر پیش‌فرض ایمن به‌کار
        می‌روند.
        """
        self.config = config
        self._safety_guard = self._build_safety_guard(config)
        # سازگاری با نام قدیمی (برای subclass‌هایی که به `_safety` اشاره می‌کنند)
        self._safety = self._safety_guard

    # ------------------------------------------------------------------
    # هوک‌هایی که subclass می‌تواند override کند
    # ------------------------------------------------------------------
    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """منطق اصلی ابزار — تنها جایی که کار واقعی انجام می‌شود.

        Returns:
            :class:`ToolResult` (هیچ‌وقت استثنایی به بیرون نیندازید؛ تبدیلش کنید).
        """
        raise NotImplementedError

    @abstractmethod
    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI Function Calling برای این ابزار."""
        raise NotImplementedError

    def validate_input(self, **kwargs: Any) -> bool:
        """اعتبارسنجی ورودی پیش از اجرا.

        Returns:
            ``True`` در صورت معتبر بودن.

        Raises:
            ValidationError: ورودی نامعتبر (پیام خطا به مدل برمی‌گردد).
        """
        provided = set(kwargs)
        missing = [key for key in self.required_parameters if key not in provided or kwargs.get(key) in (None, "")]
        if missing:
            raise ValidationError(f"missing required parameter(s): {', '.join(missing)}")
        allowed = set(self.required_parameters) | set(self.optional_parameters)
        if allowed:
            unknown = sorted(provided - allowed)
            if unknown:
                raise ValidationError(f"unknown parameter(s): {', '.join(unknown)}")
        return True

    async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
        """هوک ایمنی ابزار (به‌صورت پیش‌فرض: ریسک ذاتی ابزار).

        ابزارهای حساس (terminal/filesystem) این متد را override می‌کنند تا
        ورودی را دقیق‌تر بررسی کنند.
        """
        decision = SafetyDecision(
            allowed=True,
            requires_confirmation=self.requires_confirmation,
            risk=self.risk_level,
            reasons=["tool declares intrinsic risk '" + self.risk_level.value + "'"],
        )
        return decision

    async def before_execute(self, kwargs: dict[str, Any], context: ToolContext | None) -> dict[str, Any]:
        """نرمال‌سازی/تغییر kwargs پیش از اجرا (hook)."""
        return kwargs

    def to_info(self) -> ToolInfo:
        """تبدیل به مدل اطلاعاتی (برای ``/tools`` و مستندات)."""
        return ToolInfo(
            name=self.name,
            description=clean_text(self.description, max_length=400),
            category=self.category,
            requires_confirmation=self.requires_confirmation,
            risk_level=self.risk_level,
            parameters=list(self.required_parameters) + list(self.optional_parameters),
            version=self.version,
        )

    # ------------------------------------------------------------------
    # اجرای public (template method)
    # ------------------------------------------------------------------
    async def run(
        self,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
        call_id: str = "",
        skip_confirmation: bool = False,
    ) -> ToolResult:
        """اجرای استاندارد ابزار با همه‌ی نگرانی‌های عرضی.

        Args:
            arguments: پارامترهای ابزار.
            context: زمینه‌ی اجرا (config/safety/bus/confirm).
            call_id: شناسه‌ی فراخوانی مدل (برای رویدادها).
            skip_confirmation: رد شدن از تأیید (فقط وقتی سیاست ایمنی اجازه دهد).

        Returns:
            نتیجه‌ی اجرا؛ در خطا ``success=False`` با پیام خوانا.
        """
        ctx = context or ToolContext(config=self.config, safety=self._safety_guard)
        if ctx.safety is None:
            ctx.safety = self._safety_guard
        kwargs = dict(arguments or {})
        started_timer = Timer()
        with started_timer:
            try:
                self.validate_input(**kwargs)
                kwargs = await self.before_execute(kwargs, ctx)
                decision = await self.safety_check(kwargs, ctx)
                await self._emit(
                    ctx,
                    "tool.requested",
                    {"tool": self.name, "arguments": self._redact(kwargs), "risk": decision.risk.value},
                )
                if not decision.allowed:
                    result = ToolResult.fail(
                        f"blocked by safety guard: {decision.reason_text}",
                        tool=self.name,
                        error_code="blocked",
                        metadata={"safety": decision.as_dict()},
                    )
                    await self._emit(ctx, "safety.blocked", {"tool": self.name, "reasons": decision.reasons})
                    return result.model_copy(update={"duration_ms": started_timer.ms})

                needs_confirmation = decision.requires_confirmation and not skip_confirmation
                if needs_confirmation:
                    approved = await self._confirm(ctx, kwargs, decision, call_id=call_id)
                    if not approved:
                        result = ToolResult.fail(
                            "execution declined by the user",
                            tool=self.name,
                            error_code="declined",
                            metadata={"safety": decision.as_dict()},
                        )
                        await self._emit(ctx, "tool.denied", {"tool": self.name, "call_id": call_id})
                        return result.model_copy(update={"duration_ms": started_timer.ms})
                    await self._emit(ctx, "tool.approved", {"tool": self.name, "call_id": call_id})

                result = await self._execute_guarded(ctx, kwargs)
            except ValidationError as exc:
                result = ToolResult.fail(str(exc), tool=self.name, error_code="invalid_input")
                await self._emit(ctx, "tool.failed", {"tool": self.name, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - خطای ابزار نباید ایجنت را متوقف کند
                result = ToolResult.fail(
                    f"{type(exc).__name__}: {exc}",
                    tool=self.name,
                    error_code="tool_error",
                )
                await self._emit(ctx, "tool.failed", {"tool": self.name, "error": str(exc)})
        return result.model_copy(
            update={
                "tool": result.tool or self.name,
                "duration_ms": started_timer.ms,
            }
        )

    async def _execute_guarded(self, context: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
        """اجرای :meth:`execute` و محدودسازی اندازه‌ی خروجی."""
        if (
            self.requires_confirmation
            and context.confirm is None
            and context.config is not None
            and getattr(context.config, "enable_confirmation", False)
        ):
            return ToolResult.fail(
                f"tool '{self.name}' needs confirmation but no confirmation channel is available; "
                "run it through the CLI or pass a confirm callback",
                tool=self.name,
                error_code="confirmation_unavailable",
            )
        result = await self._call_execute(**kwargs)
        return self._normalize_result(result, context)

    async def _call_execute(self, **kwargs: Any) -> ToolResult:
        """فراخوانی execute فقط با پارامترهای مجاز (برای ابزارهای چندعملیاتی)."""
        signature = inspect.signature(self.execute)
        parameters = signature.parameters
        accepts_var_kw = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())
        if not accepts_var_kw:
            allowed = {
                name
                for name, param in parameters.items()
                if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
            }
            kwargs = {key: value for key, value in kwargs.items() if key in allowed}
        raw = self.execute(**kwargs)
        outcome: Any = await raw if inspect.isawaitable(raw) else raw
        if isinstance(outcome, ToolResult):
            return outcome
        return ToolResult(success=True, data=outcome, tool=self.name)  # انعطاف برای برگشت مقدار خام

    def _normalize_result(self, result: ToolResult, context: ToolContext) -> ToolResult:
        """کوتاه‌سازی خروجی و افزودن metadata استاندارد."""
        payload, truncated = self._cap_payload(result.data, context.max_output_chars)
        updates: dict[str, Any] = {
            "data": payload,
            "truncated": result.truncated or truncated,
            "tool": result.tool or self.name,
            "metadata": {
                "output_bytes": format_size(len(str(payload).encode("utf-8"))),
                **result.metadata,
            },
        }
        return result.model_copy(update=updates)

    @staticmethod
    def _cap_payload(payload: Any, max_chars: int) -> tuple[Any, bool]:
        """محدود کردن حجم خروجی ابزار (رشته، dict یا list).

        بدون این کار، یک ``cat`` روی فایل ۱۰ مگابایتی می‌تواند کل context مدل را
        پر کند و هزینه/خطای دنبال‌دار داشته باشد.
        """
        if isinstance(payload, str):
            return truncate_text(payload, max_chars)
        if isinstance(payload, (list, tuple)):
            rendered = "\n".join(str(item) for item in payload)
            if len(rendered) <= max_chars:
                return (list(payload) if isinstance(payload, tuple) else payload), False
            return [truncate_text(str(item), max(80, max_chars // 10))[0] for item in payload[:200]], True
        if isinstance(payload, dict):
            total = sum(len(str(value)) for value in payload.values())
            if total <= max_chars:
                return payload, False
            per_value = max(160, max_chars // max(1, len(payload)))
            capped = {
                key: (truncate_text(value, per_value)[0] if isinstance(value, str) else value)
                for key, value in payload.items()
            }
            capped["_truncated_fields"] = sorted(
                key for key, value in payload.items() if isinstance(value, str) and len(value) > per_value
            )
            return capped, True
        return payload, False

    async def _confirm(
        self,
        context: ToolContext,
        kwargs: dict[str, Any],
        decision: SafetyDecision,
        *,
        call_id: str = "",
    ) -> bool:
        """دریافت تأیید از طریق callback (در نبود آن: سیاست config)."""
        request = ConfirmationRequest(
            tool=self.name,
            action=f"{self.name}({', '.join(sorted(kwargs))})",
            summary=self._confirmation_summary(kwargs),
            details=self._redact(kwargs),
            risk=decision.risk,
            call_id=call_id,
        )
        await self._emit(context, "safety.confirmation_requested", request.model_dump(mode="json"))
        if context.confirm is None:
            # بدون کانال تأیید: فقط وقتی config اجازه‌ی اجرای بی‌تأیید دهد مجازیم
            return not bool(getattr(context.config, "enable_confirmation", True))
        outcome = context.confirm(request)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, ConfirmationDecision):
            return bool(outcome.approved)
        return bool(outcome)

    def _confirmation_summary(self, kwargs: dict[str, Any]) -> str:
        """ساخت جمله‌ی توصیفی برای پنل تأیید (subclass قابل override)."""
        parts = [f"{key}={clean_text(str(value), max_length=160)}" for key, value in kwargs.items()]
        return f"{self.name}: " + ", ".join(parts) if parts else self.name

    def _redact(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """ماسک پارامترهای حساس برای لاگ و تاریخچه."""
        from src.utils.validators import mask_value

        redacted: dict[str, Any] = {}
        for key, value in kwargs.items():
            redacted[key] = mask_value(value) if key in set(self.sensitive_parameters) else value
        return redacted

    async def _emit(self, context: ToolContext | None, kind: str, payload: dict[str, Any]) -> None:
        """انتشار رویداد (fire-and-forget و ایمن در برابر نبود bus/context)."""
        if context is None:
            return
        bus = context.bus
        if bus is None:
            return
        emit = getattr(bus, "emit", None)
        if emit is None:
            return
        try:
            outcome = emit(kind, {**payload, "tool": payload.get("tool", self.name)}, source=self.name)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # noqa: BLE001 - رویداد نباید اجرا را بشکند
            from src.utils.logger import get_logger

            get_logger("core.base_tool").debug("event emission failed for %s: %s", kind, exc)

    def safety_guard(self, context: ToolContext | None = None) -> SafetyGuard | None:
        """نگهبان ایمنی مؤثر برای این اجرا (context اولویت دارد)."""
        if context is not None and context.safety is not None:
            return context.safety
        return self._safety_guard

    def _build_safety_guard(self, config: Any) -> SafetyGuard | None:
        """ساخت guard ایمنی از config (اگر config نبود، None)."""
        if config is None:
            return None
        if isinstance(config, SafetyGuard):
            return config
        try:
            return SafetyGuard.from_config(config)
        except Exception as exc:  # noqa: BLE001 - تنظیمات ناقص نباید import را بشکند
            from src.utils.logger import get_logger

            get_logger("core.base_tool").debug("safety guard construction failed: %s", exc)
            return None

    @staticmethod
    def function_schema(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: Sequence[str] | None = None,
        *,
        strict: bool = False,
    ) -> dict[str, Any]:
        """ساخت اسکیمای استاندارد ``type: function`` برای OpenAI.

        Args:
            name: نام تابع.
            description: توضیح برای مدل.
            properties: نگاشت نام پارامتر → schema.
            required: پارامترهای اجباری.
            strict: فعال‌سازی ``strict`` در structured outputs (اگر مدل پشتیبانی کند).

        Returns:
            dict آماده‌ی درج در ``tools=[...]``.
        """
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": list(required or []),
            "additionalProperties": False,
        }
        payload: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema,
            },
        }
        if strict:
            payload["function"]["strict"] = True
        return payload

    @staticmethod
    def describe_limits(max_bytes: int) -> str:
        """توضیح کوتاه درباره‌ی سقف حجم (برای درج در description ابزار)."""
        return f"output capped at {format_size(max_bytes)}"

    class InputModel(BaseModel):
        """پایه‌ی مدل‌های ورودی ابزارها (برای ابزارهای پیشرفته‌تر).

        subclass کردن از این مدل به شما اجازه می‌دهد با قدرت Pydantic اعتبارسنجی
        کنید و سپس در :meth:`execute` با آبجکت type-safe کار کنید.
        """

        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    def __repr__(self) -> str:
        """نمایش قابل‌فهم در لاگ و تست."""
        return f"<{type(self).__name__} name={self.name!r} category={self.category.value}>"

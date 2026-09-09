"""ایجنت اصلی پروژه: حلقه‌ی «مدل ⇄ ابزار» (agentic loop).

نقشه‌ی جریان در هر :meth:`UniversalAgent.ask`:

1. پیام کاربر به‌همراه system prompt و تاریخچه به مدل فرستاده می‌شود.
2. اگر مدل «فراخوانی ابزار» خواست، ورودی‌اش parse و اعتبارسنجی می‌شود.
3. نگهبان ایمنی تصمیم می‌گیرد: اجرا / تأیید بگیر / مسدود کن.
4. نتیجه‌ی ابزار به‌عنوان پیام ``role="tool"`` به گفت‌وگو اضافه می‌شود.
5. چرخه تا رسیدن به پاسخ نهایی یا رسیدن به ``MAX_TOOL_ITERATIONS`` ادامه می‌یابد.

نکته‌ی عملیاتی درباره‌ی مدل: اگر نام مدل در سرویس مقصد وجود نداشته باشد
(مثلاً ``gpt-6-astra`` روی API رسمی OpenAI)، ایجنت به اولین مدل موجود در
``MODEL_FALLBACKS`` سوییچ می‌کند و یک رویداد ``llm.model_fallback`` منتشر
می‌کند تا کاربر متوجه شود.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from src.config import Config, get_config
from src.core.base_tool import ConfirmationCallback
from src.core.event_bus import EVENTS, EventBus
from src.core.memory import AgentMemory
from src.core.reports import ActivityRecorder, build_report, redact_line
from src.core.tool_registry import ToolRegistry, discover_tools
from src.models.agent_models import (
    AgentRunResult,
    AgentState,
    Message,
    ToolCallRecord,
    UsageStats,
)
from src.models.config_models import AgentProfile
from src.models.tool_models import ToolCall, ToolResult
from src.utils.helpers import safe_json_loads, shorten_middle, truncate_text
from src.utils.logger import get_logger
from src.utils.safety import SafetyGuard
from src.utils.validators import ValidationError

try:  # pragma: no cover - وابستگی اختیاری در محیط‌های مینیمال
    from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError
except ImportError:  # pragma: no cover

    class AsyncOpenAI:  # type: ignore[no-redef]
        """Placeholder که فقط در صورت نصب نبود کتابخانه‌ی openai استفاده می‌شود."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the 'openai' package is not installed: pip install openai")

    APIConnectionError = APIStatusError = APITimeoutError = RateLimitError = RuntimeError  # type: ignore[misc,assignment]

__all__ = ["DEFAULT_SYSTEM_PROMPT", "UniversalAgent"]

#: پیام پیش‌فرض سیستم (قابل بازنویسی با system_prompt یا پروفایل)
DEFAULT_SYSTEM_PROMPT = (
    "You are Universal Agent Hub, an autonomous assistant with real access to the user's machine "
    "through tools. Use tools instead of guessing, report failures honestly, and prefer "
    "non-destructive operations. Keep answers concise and concrete."
)

#: خطاهایی که «مدل یافت نشد» را نشان می‌دهند (برای fallback خودکار)
_MODEL_NOT_FOUND_RE = re.compile(
    r"(model[_ ]not[_ ]found|does not exist|unknown model|invalid model|unsupported model|no such model)",
    re.IGNORECASE,
)

#: خطاهای قابل تلاش‌مجدد
_RETRYABLE_RE = re.compile(
    r"(rate.?limit|timeout|timed out|temporarily|overloaded|connection|reset by peer|502|503|504|429)", re.IGNORECASE
)


class UniversalAgent:
    """ایجنت عمومی با دسترسی کامل (و کنترل‌شده) به سیستم.

    Args:
        config: پیکربندی. اگر None باشد از :func:`src.config.get_config` خوانده می‌شود.
        tools: فهرست نام ابزارهای مجاز برای این ایجنت (None = همه‌ی ابزارهای رجیستری).
        event_bus: باس رویداد (None = باس تازه با observer لاگ).
        safety: نگهبان ایمنی (None = ساخته‌شده از config).
        system_prompt: پیام سیستم.
        profile: پروفایل اختیاری که از کارخانه تزریق می‌شود.
        confirm_callback: تابع تأیید (async یا sync) برای عملیات حساس.
        client: تزریق client سفارشی (برای تست و سرویس‌های سازگار با OpenAI).
        registry: کلاس رجیستری ابزارها.
        keep_history: نگهداری تاریخچه‌ی گفت‌وگو بین چند نوبت.
        max_history_messages: سقف تعداد پیام نگه‌داشته‌شده.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        tools: Iterable[str] | None = None,
        event_bus: EventBus | None = None,
        safety: SafetyGuard | None = None,
        system_prompt: str | None = None,
        profile: AgentProfile | None = None,
        confirm_callback: ConfirmationCallback | None = None,
        client: Any = None,
        registry: type[ToolRegistry] = ToolRegistry,
        keep_history: bool = True,
        max_history_messages: int = 40,
        memory: AgentMemory | None = None,
        use_memory: bool | None = None,
        recorder: ActivityRecorder | None = None,
    ) -> None:
        self.config = config or get_config()
        self.registry = registry
        self.logger = get_logger("agent")
        self.profile = profile
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.event_bus = event_bus or EventBus(persist_path=self.config.event_log_path)
        self.safety = safety or SafetyGuard.from_config(self.config)
        self.confirm_callback: ConfirmationCallback | None = confirm_callback
        self.keep_history = keep_history
        self.max_history_messages = max_history_messages
        self.state = AgentState(active_profile=profile.name if profile else "generalist")
        self.model = self.config.active_model or self.config.model_name
        self._requested_model = self.config.model_name
        self._model_candidates: list[str] = list(self.config.safe_model_candidates) or [self.model]
        self._tool_names: list[str] = list(tools) if tools is not None else []
        self._context = self.registry.build_context(
            config=self.config,
            safety=self.safety,
            bus=self.event_bus,
            confirm=self._confirm_through_callback,
        )
        self._last_usage = UsageStats()
        self._model_downgrade_reason: str | None = None
        self.client = client or self._build_client()
        # حافظه‌ی بلندمدت و گزارش فعالیت: هیچ‌کدام نباید خودِ اجرا را بشکنند
        if use_memory is None:
            use_memory = bool(getattr(self.config, "memory_enabled", False))
        self.memory = memory if memory is not None else (AgentMemory.for_config(self.config) if use_memory else None)
        if use_memory is False and memory is None:
            self.memory = None
        self.recorder = recorder if recorder is not None else ActivityRecorder.for_config(self.config)

    # ------------------------------------------------------------------
    # راه‌اندازی
    # ------------------------------------------------------------------
    def _build_client(self) -> Any:
        """ساخت client ایوای OpenAI (یا سازگار با آن)."""
        kwargs: dict[str, Any] = {
            "api_key": self.config.openai_api_key or "sk-not-configured",
            "timeout": self.config.request_timeout,
            "max_retries": 0,  # retry را خودمان انجام می‌دهیم تا کنترل کامل داشته باشیم
        }
        if self.config.openai_base_url:
            kwargs["base_url"] = self.config.openai_base_url
        return AsyncOpenAI(**kwargs)

    @property
    def tools(self) -> list[Any]:
        """نمونه‌های ابزار فعال برای این ایجنت."""
        if not self._tool_names:
            discover_tools()
            return self.registry.instances(config=self.config)
        return self.registry.instances(config=self.config, only=self._tool_names)

    @property
    def tool_names(self) -> list[str]:
        """نام ابزارهای فعال."""
        return [tool.name for tool in self.tools]

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """اسکیمای OpenAI ابزارهای فعال."""
        if not self._tool_names:
            return self.registry.schemas(config=self.config)
        return self.registry.schemas(config=self.config, only=self._tool_names)

    @property
    def model_info(self) -> dict[str, Any]:
        """وضعیت مدل (نام درخواستی، نام واقعی، دلیل تغییر احتمالی)."""
        return {
            "requested": self._requested_model,
            "active": self.model,
            "candidates": list(self._model_candidates),
            "downgraded": self.model != self._requested_model,
            "reason": self._model_downgrade_reason,
            "base_url": self.config.openai_base_url or "https://api.openai.com/v1",
        }

    def describe_tools(self) -> list[dict[str, Any]]:
        """توضیح ابزارها برای چاپ در CLI."""
        return [tool.to_info().model_dump(mode="json") for tool in self.tools]

    # ------------------------------------------------------------------
    # حلقه‌ی اصلی
    # ------------------------------------------------------------------
    async def ask(
        self, user_input: str, *, system_note: str | None = None, raise_on_error: bool = False
    ) -> AgentRunResult:
        """اجرای یک نوبت گفت‌وگو و بازگرداندن نتیجه‌ی کامل.

        Args:
            user_input: درخواست کاربر.
            system_note: یادداشت سیستمی اضافی برای همین نوبت (مثلاً context پوشه‌ی کاری).
            raise_on_error: اگر True باشد خطای مدل به‌صورت استثنا raised می‌شود.

        Returns:
            :class:`AgentRunResult` با متن، فراخوانی‌های ابزار، مصرف توکن و زمان.

        Raises:
            ValueError: ورودی خالی باشد (و raise_on_error=True).
            RuntimeError: خطای مدل در حالت raise_on_error.
        """
        prompt = str(user_input or "").strip()
        if not prompt:
            if raise_on_error:
                raise ValueError("user input must not be empty")
            return AgentRunResult(ok=False, error="empty input", model=self.model)

        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.event_bus.emit(EVENTS.AGENT_STARTED, {"input": shorten_middle(prompt, 400), "model": self.model})

        messages = self._build_messages(prompt, system_note=system_note)
        result = AgentRunResult(model=self.model)
        baseline = self._last_usage
        try:
            while result.iterations < self.config.max_tool_iterations:
                result.iterations += 1
                message = await self._chat_with_retry(messages)
                if message is None:
                    result.ok = False
                    result.error = result.error or "the model returned no response"
                    break

                tool_calls = self._extract_tool_calls(message)
                text = self._extract_text(message)
                if tool_calls:
                    messages.append(self._assistant_tool_message(message, text))
                    results = await self._run_tool_calls(tool_calls, messages, result)
                    if all(r is None for r in results) and results:
                        # همه‌ی ابزارها رد/مسدود شدند؛ به مدل اطلاع می‌دهیم تا ادامه دهد
                        continue
                    continue
                result.text = text
                messages.append(Message(role="assistant", content=text).to_api_dict())
                self._record_history(Message(role="assistant", content=text))
                break
            else:
                result.text = (
                    result.text
                    or "I stopped after reaching the configured tool iteration limit "
                    f"({self.config.max_tool_iterations}). Ask me to continue, or raise MAX_TOOL_ITERATIONS."
                )
                result.ok = True
        except Exception as exc:  # noqa: BLE001 - تصمیم نهایی در CLI/layer صدا‌زننده
            self.logger.error("agent run failed: %s", exc, exc_info=True)
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            await self.event_bus.emit(EVENTS.AGENT_FAILED, {"error": str(exc)})
            if raise_on_error:
                raise RuntimeError(result.error) from exc

        result.duration_ms = int((loop.time() - started) * 1000)
        result.usage = UsageStats(
            prompt_tokens=max(0, self._last_usage.prompt_tokens - baseline.prompt_tokens),
            completion_tokens=max(0, self._last_usage.completion_tokens - baseline.completion_tokens),
            total_tokens=max(0, self._last_usage.total_tokens - baseline.total_tokens),
        )
        result.model = self.model
        if not self.keep_history:
            self.state.messages = []
        await self.event_bus.emit(
            EVENTS.AGENT_COMPLETED,
            {
                "ok": result.ok,
                "iterations": result.iterations,
                "tools": result.tool_names,
                "duration_ms": result.duration_ms,
                "tokens": result.usage.total_tokens,
            },
        )
        self._capture_run(result, prompt)
        self._record_activity(result, prompt)
        return result

    async def run(self, user_input: str, *, system_note: str | None = None) -> str:
        """سازگاری با API ساده: فقط متن پاسخ نهایی را برمی‌گرداند."""
        result = await self.ask(user_input, system_note=system_note)
        if not result.ok and not result.text:
            return f"[error] {result.error}"
        return result.text

    # ------------------------------------------------------------------
    # مکالمه / پیام‌ها
    # ------------------------------------------------------------------
    def _build_messages(self, prompt: str, *, system_note: str | None = None) -> list[dict[str, Any]]:
        """ساخت لیست پیام برای یک فراخوانی جدید (system + تاریخچه + ورودی)."""
        system = self.system_prompt
        extra = (self.profile.system_prompt_extra if self.profile else "") or ""
        if extra:
            system = f"{system}\n{extra}"
        memory_block = self.memory_block(prompt)
        if memory_block:
            system = f"{system}\n\n{memory_block}"
        if system_note:
            system = f"{system}\nContext: {system_note}"
        messages: list[dict[str, Any]] = [Message(role="system", content=system).to_api_dict()]
        if self.keep_history:
            messages.extend(message.to_api_dict() for message in self.state.messages)
        messages.append(Message(role="user", content=prompt).to_api_dict())
        self._record_history(Message(role="user", content=prompt))
        self.state.turn_count += 1
        return messages

    def _record_history(self, message: Message) -> None:
        """افزودن پیام به تاریخچه با محدودسازی طول (windowing)."""
        if not self.keep_history:
            return
        self.state.messages.append(message)
        limit = max(4, self.max_history_messages)
        if len(self.state.messages) > limit:
            self.state.messages = self.state.messages[-limit:]

    def reset_history(self) -> None:
        """پاک کردن تاریخچه‌ی گفت‌وگو (``/clear`` در CLI)."""
        self.state.clear()
        self._last_usage = UsageStats()

    @staticmethod
    def _extract_text(message: Any) -> str:
        """استخراج متن از پیام مدل (سازگار با content رشته‌ای یا لیست‌بخشی‌شده)."""
        content = getattr(message, "content", None)
        if content is None:
            reasoning = getattr(message, "reasoning_content", None)
            return str(reasoning).strip() if reasoning else ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(getattr(item, "text", "") or ""))
            return "\n".join(part for part in parts if part).strip()
        return str(content).strip()

    @staticmethod
    def _extract_tool_calls(message: Any) -> list[ToolCall]:
        """تبدیل tool_calls مدل به :class:`ToolCall` (با JSON ایمن)."""
        raw_calls = getattr(message, "tool_calls", None) or []
        calls: list[ToolCall] = []
        for item in raw_calls:
            function = getattr(item, "function", None) or (item.get("function") if isinstance(item, dict) else None)
            if function is None:
                continue
            name = getattr(function, "name", None) or (function.get("name") if isinstance(function, dict) else "")
            arguments_raw = getattr(function, "arguments", None)
            if arguments_raw is None and isinstance(function, dict):
                arguments_raw = function.get("arguments")
            arguments = safe_json_loads(arguments_raw)
            call_id = getattr(item, "id", "") or (item.get("id", "") if isinstance(item, dict) else "")
            calls.append(
                ToolCall(
                    id=str(call_id or f"call_{len(calls)}"),
                    name=str(name or ""),
                    arguments=arguments,
                    raw_arguments=truncate_text(str(arguments_raw or ""), 2000)[0],
                )
            )
        return calls

    def _assistant_tool_message(self, message: Any, text: str) -> dict[str, Any]:
        """ساخت پیام assistant که tool_calls دارد (باید عیناً به API برگردد)."""
        payload: dict[str, Any] = {"role": "assistant", "content": text or None}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            payload["tool_calls"] = [
                {
                    "id": getattr(call, "id", None) or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(call, "function", None), "name", ""),
                        "arguments": getattr(getattr(call, "function", None), "arguments", "") or "{}",
                    },
                }
                for index, call in enumerate(tool_calls)
            ]
        return payload

    # ------------------------------------------------------------------
    # اجرای ابزارها
    # ------------------------------------------------------------------
    async def _run_tool_calls(
        self,
        calls: Sequence[ToolCall],
        messages: list[dict[str, Any]],
        result: AgentRunResult,
    ) -> list[ToolResult | None]:
        """اجرای (ترجیحاً موازی) ابزارهای درخواستی و ثبت نتایج در گفت‌وگو.

        ترتیب پیام‌های ``tool`` همیشه ترتیب درخواست مدل است، حتی وقتی اجراها موازی‌اند:
        هر فراخوانی در سینه‌ی خودش می‌نویسد و این‌جا به ترتیب ادغام می‌شود. بدون این کار،
        تاریخچه‌ی مدل وابسته به زمان اتمام هر ابزار می‌شد (غیرقابل‌تکرار و در تست‌ها flaky).
        """
        parallel = len(calls) > 1 and bool(getattr(self.config, "parallel_tool_calls", True))
        if not parallel:
            return [await self._execute_call(call, messages, result) for call in calls]
        message_slots: list[list[dict[str, Any]]] = [[] for _ in calls]
        record_slots: list[list[ToolCallRecord]] = [[] for _ in calls]
        outcomes = list(
            await asyncio.gather(
                *(
                    self._execute_call(call, message_slots[index], result, records=record_slots[index])
                    for index, call in enumerate(calls)
                )
            )
        )
        for slot_messages, slot_records in zip(message_slots, record_slots, strict=True):
            messages.extend(slot_messages)
            result.tool_calls.extend(slot_records)
        return outcomes

    async def _execute_call(
        self,
        call: ToolCall,
        messages: list[dict[str, Any]],
        result: AgentRunResult,
        *,
        records: list[ToolCallRecord] | None = None,
    ) -> ToolResult | None:
        """اجرای یک فراخوانی: یافتن ابزار، اجرا، ثبت رکورد و پیام tool.

        Args:
            call: فراخوانی مدل.
            messages: مقصد پیام‌های ``tool`` (در حالت موازی سینه‌ی اختصاصی هر فراخوانی).
            result: نتیجه‌ی کلی اجرا (برای شمارنده‌ها).
            records: مقصد رکوردها؛ پیش‌فرض ``result.tool_calls``.
        """
        sink = records if records is not None else result.tool_calls
        record = ToolCallRecord(tool=call.name, arguments=dict(call.arguments))
        tool = self.registry.get(call.name, config=self.config)
        if tool is None:
            failure = ToolResult.fail(
                f"unknown tool '{call.name}'. Available tools: {', '.join(self.tool_names) or 'none'}",
                tool=call.name,
                error_code="no_such_tool",
            )
            record.result = failure
            record.error = failure.error
            sink.append(record)
            messages.append(self._tool_message(call, failure))
            await self.event_bus.emit(EVENTS.TOOL_FAILED, {"tool": call.name, "error": failure.error})
            return failure

        tool_result = await tool.run(call.arguments, context=self._context, call_id=call.id)
        record.result = tool_result
        record.duration_ms = tool_result.duration_ms
        record.approved = not tool_result.requires_confirmation
        record.error = tool_result.error
        sink.append(record)
        messages.append(self._tool_message(call, tool_result))
        await self.event_bus.emit(
            EVENTS.TOOL_COMPLETED if tool_result.success else EVENTS.TOOL_FAILED,
            {
                "tool": call.name,
                "duration_ms": tool_result.duration_ms,
                "error": tool_result.error,
                "success": tool_result.success,
            },
        )
        return tool_result

    def _tool_message(self, call: ToolCall, tool_result: ToolResult) -> dict[str, Any]:
        """ساخت پیام ``role=tool`` برای تزریق نتیجه به مدل."""
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": tool_result.to_llm_string(max_chars=self.config.max_output_chars),
        }

    # ------------------------------------------------------------------
    # گفت‌وگو با مدل (retry + fallback)
    # ------------------------------------------------------------------
    async def _chat_with_retry(self, messages: Sequence[dict[str, Any]]) -> Any:
        """فراخوانی chat.completions با تلاش مجدد و جایگزینی مدل در صورت لزوم."""
        payload = self._request_payload(messages)
        attempt = 0
        candidate_index = self._model_candidates.index(self.model) if self.model in self._model_candidates else 0
        last_error: Exception | None = None
        while True:
            try:
                await self.event_bus.emit(
                    EVENTS.LLM_REQUESTED, {"iteration": attempt + 1, "model": self.model, "messages": len(messages)}
                )
                before = self._last_usage
                response = await self.client.chat.completions.create(**payload)
                self._track_usage(response)
                delta = self._last_usage.add(
                    UsageStats(
                        prompt_tokens=-before.prompt_tokens,
                        completion_tokens=-before.completion_tokens,
                        total_tokens=-before.total_tokens,
                    )
                )
                await self.event_bus.emit(EVENTS.LLM_RESPONDED, {"model": self.model, "usage": delta.model_dump()})
                return self._first_choice(response)
            except Exception as exc:  # noqa: BLE001 - دسته‌بندی خطا در ادامه انجام می‌شود
                last_error = exc
                if self._is_model_not_found(exc) and candidate_index + 1 < len(self._model_candidates):
                    previous = self.model
                    self.model = self._model_candidates[candidate_index + 1]
                    candidate_index += 1
                    self._model_downgrade_reason = f"'{previous}' is not available on this endpoint"
                    payload["model"] = self.model
                    self.logger.warning(
                        "model '%s' unavailable → falling back to '%s' (set MODEL_NAME in .env to change this)",
                        previous,
                        self.model,
                    )
                    await self.event_bus.emit(
                        "llm.model_fallback",
                        {"from": previous, "to": self.model, "reason": self._model_downgrade_reason},
                    )
                    continue
                if attempt >= self.config.max_retries or not self._is_retryable(exc):
                    break
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)  # noqa: S311 - jitter ساده
                attempt += 1
                self.logger.warning(
                    "transient LLM error (%s); retry %d/%d in %.1fs", exc, attempt, self.config.max_retries, delay
                )
                await self.event_bus.emit(
                    EVENTS.LLM_FAILED, {"error": str(exc), "retry_in": round(delay, 2), "attempt": attempt}
                )
                await asyncio.sleep(delay)
        await self.event_bus.emit(EVENTS.LLM_FAILED, {"error": str(last_error), "fatal": True})
        raise self._describe_llm_error(last_error)

    def _request_payload(self, messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """ساخت بدنه‌ی درخواست chat.completions متناسب با قابلیت‌های سرویس."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        schemas = self.schemas
        if schemas:
            payload["tools"] = schemas
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _first_choice(response: Any) -> Any:
        """بازگرداندن message اولین choice (با بررسی ساختار پاسخ)."""
        choices = getattr(response, "choices", None) or (
            response.get("choices") if isinstance(response, dict) else None
        )
        if not choices:
            raise RuntimeError("the model returned a response without choices")
        first = choices[0]
        message = getattr(first, "message", None) or (first.get("message") if isinstance(first, dict) else None)
        if message is None:
            raise RuntimeError("the model returned a choice without a message payload")
        return message

    def _track_usage(self, response: Any) -> None:
        """ثبت مصرف توکن پاسخ در مجموع."""
        usage = getattr(response, "usage", None) or (response.get("usage") if isinstance(response, dict) else None)
        try:
            self._last_usage = self._last_usage.add(UsageStats.from_api(usage))
        except (TypeError, ValueError):  # pragma: no cover - سرویس‌های غیراستاندارد
            self.logger.debug("could not parse usage payload: %r", usage)

    @staticmethod
    def _is_model_not_found(exc: BaseException) -> bool:
        """تشخیص خطای «مدل وجود ندارد» از روی status code و متن پیام."""
        status = getattr(exc, "status_code", None)
        text = str(exc)
        if status in {400, 404} and _MODEL_NOT_FOUND_RE.search(text):
            return True
        return bool(_MODEL_NOT_FOUND_RE.search(text))

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """آیا خطا ارزش تلاش مجدد دارد؟"""
        if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError)):
            return True
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        return bool(_RETRYABLE_RE.search(str(exc)))

    def _describe_llm_error(self, exc: BaseException | None) -> RuntimeError:
        """تبدیل خطای خام API به پیام راهنما برای کاربر."""
        if exc is None:  # pragma: no cover - محافظتی
            return RuntimeError("the model call failed without an error message")
        text = str(exc)
        if isinstance(exc, APIStatusError) or _MODEL_NOT_FOUND_RE.search(text):
            known = ", ".join(f"`{name}`" for name in sorted(self._model_candidates))
            return RuntimeError(
                f"model '{self.model}' could not be used by the endpoint: {truncate_text(text, 400)[0]}\n"
                f"Try one of the configured models ({known}) by setting MODEL_NAME in .env. "
                "Note: 'gpt-6-astra' is only usable if your provider (OPENAI_BASE_URL) actually exposes such a model."
            )
        if not self.config.is_api_key_set:
            return RuntimeError(f"OPENAI_API_KEY is not configured. Original error: {truncate_text(text, 300)[0]}")
        return RuntimeError(truncate_text(f"{type(exc).__name__}: {text}", 800)[0])

    # ------------------------------------------------------------------
    # تأیید
    # ------------------------------------------------------------------
    def set_confirmation_handler(self, handler: ConfirmationCallback | None) -> None:
        """تزریق callback تأیید (بعداً هم قابل تنظیم است؛ مثلاً از CLI)."""
        self.confirm_callback = handler
        self._context.confirm = self._confirm_through_callback if handler is not None else None

    async def _confirm_through_callback(self, request: Any) -> bool:
        """پل بین ابزار و callback تأیید کاربر (نسخه‌ی sync هم مجاز است)."""
        handler = self.confirm_callback
        if handler is None:
            return not bool(getattr(self.config, "enable_confirmation", True))
        outcome = handler(request)
        if asyncio.iscoroutine(outcome) or asyncio.isfuture(outcome):
            outcome = await outcome
        approved = bool(getattr(outcome, "approved", outcome))
        if approved:
            self.state.confirmations_granted += 1
        await self.event_bus.emit(
            EVENTS.TOOL_APPROVED if approved else EVENTS.TOOL_DENIED,
            {"tool": getattr(request, "tool", "?"), "call_id": getattr(request, "call_id", "")},
        )
        return bool(approved)

    # ------------------------------------------------------------------
    # ابزارهای کمکی برای لایه‌های بالاتر
    # ------------------------------------------------------------------
    def memory_block(self, query: str = "") -> str:
        """بلوک حافظه‌ی بلندمدت برای system prompt (``""`` اگر حافظه خاموش باشد)."""
        if self.memory is None:
            return ""
        try:
            return self.memory.context_block(
                max_chars=int(getattr(self.config, "memory_context_chars", 4000)), query=query
            )
        except Exception as exc:  # noqa: BLE001 - حافظه نباید اجرا را شکست
            self.logger.debug("memory block unavailable: %s", exc)
            return ""

    def _capture_run(self, result: AgentRunResult, prompt: str) -> None:
        """یک یادداشت خودکار از همین اجرا در حافظه ثبت می‌کند (اگر فعال باشد)."""
        if self.memory is None or not bool(getattr(self.config, "memory_auto_capture", True)):
            return
        try:
            self.memory.capture_run(result, prompt=prompt, profile=self.state.active_profile)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("memory capture failed: %s", exc)

    def _record_activity(self, result: AgentRunResult, prompt: str) -> None:
        """یک رکورد خلاصه‌ی اجرا در فایل فعالیت می‌نویسد (منبع پنل گزارش‌ها)."""
        if self.recorder is None:
            return
        calls = [
            {
                "tool": item.tool,
                "ok": bool(item.result.success) if item.result is not None else False,
                "code": str(item.result.error_code or "") if item.result is not None else "",
                "duration_ms": int(item.duration_ms or 0),
            }
            for item in result.tool_calls
        ]
        try:
            self.recorder.record(
                {
                    "kind": "run",
                    "ok": bool(result.ok),
                    "iterations": int(result.iterations),
                    "tools": [item.tool for item in result.tool_calls],
                    "calls": calls,
                    "duration_ms": int(result.duration_ms),
                    "tokens": int(result.usage.total_tokens),
                    "model": str(result.model or self.model),
                    "profile": str(self.state.active_profile or ""),
                    "prompt": redact_line(prompt),
                    "error": redact_line(result.error or ""),
                }
            )
        except Exception as exc:  # noqa: BLE001 - گزارش‌دهی نباید اجرا را بشکند
            self.logger.debug("activity record failed: %s", exc)

    def memory_summary(self) -> dict[str, Any]:
        """آمار حافظه‌ی بلندمدت (برای ``/memory``، ``--doctor`` و پنل گزارش)."""
        if self.memory is None:
            return {"enabled": False, "records": 0}
        return self.memory.stats()

    def activity_report(self, *, days: float = 7.0) -> dict[str, Any]:
        """گزارش فعالیت: اجراها + ابزارها + تصمیم‌های ایمنی + برنامه‌های باز."""
        return build_report(self.config, days=days, memory=self.memory, recorder=self.recorder)

    def safety_summary(self) -> dict[str, Any]:
        """خلاصه‌ی سیاست ایمنی جاری (برای ``/config``)."""
        summary = self.safety.summary() if isinstance(self.safety, SafetyGuard) else {}
        summary["confirmation_enabled"] = bool(getattr(self.config, "enable_confirmation", True))
        summary["has_confirmation_channel"] = self.confirm_callback is not None
        return summary

    def validate_tool_arguments(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """اعتبارسنجی خشک ورودی یک ابزار (بدون اجرا) — مفید برای UI/تست.

        Returns:
            پیام خطا یا ``None`` در صورت معتبر بودن.
        """
        tool = self.registry.get(tool_name, config=self.config)
        if tool is None:
            return f"unknown tool '{tool_name}'"
        try:
            tool.validate_input(**arguments)
        except ValidationError as exc:
            return str(exc)
        return None

    def export_state(self) -> str:
        """JSON تاریخچه/حالت ایجنت (برای ذخیره و resume)."""
        return self.state.to_json()

    def load_state(self, payload: dict[str, Any]) -> None:
        """بازیابی حالت از dict (خروجی :meth:`export_state`)."""
        self.state = AgentState.model_validate(payload)

    def add_tool(self, tool: Any) -> None:
        """افزودن ابزار به رجیستری و فعال‌سازی آن برای همین ایجنت."""
        self.registry.register(type(tool) if not isinstance(tool, type) else tool)
        if tool.name not in self._tool_names:
            self._tool_names.append(tool.name)
            self._tool_names.sort()

    async def close(self) -> None:
        """آزادسازی منابع (مثلاً session مرورگر)."""
        for tool in self.tools:
            closer: Callable[[], Any] | None = getattr(tool, "close", None)
            if callable(closer):
                try:
                    outcome = closer()
                    if asyncio.iscoroutine(outcome):
                        await outcome
                except Exception as exc:  # noqa: BLE001 - بستن منابع نباید خطا بدهد
                    self.logger.debug("tool %s close failed: %s", tool.name, exc)
        session = self._context.session
        session.clear()

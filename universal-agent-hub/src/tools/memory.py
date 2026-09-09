"""ابزارهای حافظه‌ی بلندمدت (memory tools).

ایجنت با این چهار ابزار «دفتر یادداشت پایدار» خودش را مدیریت می‌کند:

``memory_write``   ثبت ترجیح/رویه/تصمیم/برنامه (dedupe خودکار، redaction خودکار)
``memory_read``    خواندن تازه‌ترین‌ها یا یک رکورد با id
``memory_search``  جست‌وجوی رتبه‌بندی‌شده
``memory_forget``  حذف یک رکورد (رکوردهای pinned فقط با ``force``)

چرا ابزار و نه فقط زیرسیستم: مدل باید *خودش* تصمیم بگیرد چه چیزی ارزش ماندن دارد و
این فراخوانی‌ها روی EventBus ثبت می‌شوند، پس در پنل گزارش مشخص است ایجنت چه یادداشتی
نوشته. بلوک خودکارِ حافظه در system prompt هم جدا از این‌هاست (``AgentMemory.context_block``).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.core.memory import MEMORY_KINDS, AgentMemory, MemoryRecord
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.validators import ValidationError, bounded_int, clean_text

__all__ = [
    "MemoryForgetTool",
    "MemoryReadTool",
    "MemorySearchTool",
    "MemoryToolBase",
    "MemoryWriteTool",
]


def _resolve_memory(fallback_config: Any, context: ToolContext | None = None) -> AgentMemory | None:
    """store مشترکِ همین config.

    قرارداد پروژه این است که config به *نمونه‌ی ابزار* تزریق می‌شود
    (``ToolRegistry.instances(config=...)``) و :class:`ToolContext` فقط به hook‌های
    ایمنی می‌رسد؛ پس اول ``self.config`` و در نبودش ``context.config``.
    ``None`` یعنی حافظه خاموش است یا configی در کار نبوده.
    """
    config = fallback_config or getattr(context, "config", None)
    if config is None:
        return None
    return AgentMemory.for_config(config)


class MemoryToolBase(BaseTool):
    """مشترکات ابزارهای حافظه (اسم‌ها، ریسک و یافتن store)."""

    category: ClassVar[ToolCategory] = ToolCategory.CUSTOM
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW

    def _store(self, context: ToolContext | None = None) -> tuple[AgentMemory | None, ToolResult | None]:
        """store را برمی‌گرداند؛ یا یک ToolResult خطا اگر حافظه خاموش/ناآماده بود."""
        memory = _resolve_memory(getattr(self, "config", None), context)
        if memory is None:
            return None, ToolResult.fail(
                "long-term memory is disabled (set MEMORY_ENABLED=true)",
                tool=getattr(self, "name", "memory"),
                error_code="disabled",
            )
        return memory, None


@register_tool
class MemoryWriteTool(MemoryToolBase):
    """ذخیره‌ی یک یادداشت پایدار در حافظه‌ی بلندمدت ایجنت."""

    name: ClassVar[str] = "memory_write"
    description: ClassVar[str] = (
        "Persist a durable note for future sessions: user preferences, learned procedures, "
        "decisions, or open plans. Use it when something is worth remembering after this run ends; "
        "do not store secrets, credentials, or one-off observations."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("content",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("kind", "tags", "confidence", "pin")
    sensitive_parameters: ClassVar[tuple[str, ...]] = ()

    async def execute(  # type: ignore[override]
        self,
        content: str = "",
        kind: str = "note",
        tags: list[str] | None = None,
        confidence: float = 0.8,
        pin: bool = False,
        *,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """ثبت (یا تقویت) یک رکورد حافظه.

        Args:
            content: متن یادداشت؛ concrete و خودکفا بنویسید (بدون ارجاب به «این» و «آن»).
            kind: یکی از ``fact`` ``preference`` ``procedure`` ``decision`` ``plan`` ``note``.
            tags: برچسب‌های کوتاه مثل ``project:webshop`` یا ``area:payments``.
            confidence: اطمینان ۰..۱ (روی رتبه‌بندی جست‌وجو اثر دارد).
            pin: چسباندن تا هرگز حذف نشود (ترجیحات اصلی کاربر).
        """
        memory, failure = self._store(context)
        if memory is None:
            return failure or ToolResult.fail(
                "long-term memory is unavailable", tool=self.name, error_code="disabled"
            )
        text = clean_text(content, max_length=4000)
        if not text:
            return ToolResult.fail("content must not be empty", tool=self.name, error_code="invalid_input")
        wanted = str(kind or "note").strip().lower()
        if wanted not in MEMORY_KINDS:
            return ToolResult.fail(
                f"kind must be one of: {', '.join(MEMORY_KINDS)}", tool=self.name, error_code="invalid_input"
            )
        try:
            score = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValidationError("confidence must be a number between 0 and 1") from exc
        if not 0.0 <= score <= 1.0:
            return ToolResult.fail("confidence must be between 0 and 1", tool=self.name, error_code="invalid_input")
        cleaned_tags = [str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()]
        record = memory.add(
            text,
            kind=wanted,
            tags=cleaned_tags,
            source=f"tool:{self.name}",
            confidence=score,
            pin=bool(pin),
        )
        if record is None:  # pragma: no cover - فقط اگر store بی‌صدا رد کند
            return ToolResult.fail("memory refused the write", tool=self.name, error_code="tool_error")
        return ToolResult.ok(
            {
                "id": record.id,
                "kind": record.kind,
                "saved": True,
                "tags": record.tags,
                "records": memory.stats()["records"],
            },
            tool=self.name,
            metadata={"id": record.id},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("content",),
            properties={
                "content": {
                    "type": "string",
                    "description": "The note itself. Write it self-contained, e.g. 'Deploy target for webshop is ssh host `prod1`, run scripts/deploy.sh'.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(MEMORY_KINDS),
                    "description": "preference = how the user likes things; procedure = how to do it; decision = what was chosen and why; plan = what to do next.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short keys like project:webshop, area:payments (max 12).",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "How sure you are (default 0.8).",
                },
                "pin": {"type": "boolean", "description": "Never prune this note (use for core user preferences)."},
            },
        )


@register_tool
class MemoryReadTool(MemoryToolBase):
    """خواندن حافظه: تازه‌ترین‌ها، یک نوع خاص، یا یک رکورد با id."""

    name: ClassVar[str] = "memory_read"
    description: ClassVar[str] = (
        "Read long-term memory: recent notes, notes of one kind, or a single record by id. "
        "Use before repeating questions or before changing something the user already told you."
    )
    optional_parameters: ClassVar[tuple[str, ...]] = ("id", "kind", "limit")

    async def execute(  # type: ignore[override]
        self,
        id: str = "",  # noqa: A002 - نام قراردادی با API سرور یکی باشد
        kind: str = "",
        limit: int = 10,
        *,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """بازگرداندن رکوردها (کوتاه‌شده و قابل‌خواندن برای مدل)."""
        memory, failure = self._store(context)
        if memory is None:
            return failure or ToolResult.fail(
                "long-term memory is unavailable", tool=self.name, error_code="disabled"
            )
        capped = bounded_int(limit, name="limit", minimum=1, maximum=50, default=10)
        if id:
            record = memory.find(str(id).strip())
            if record is None:
                return ToolResult.fail(f"no memory record with id '{id}'", tool=self.name, error_code="not_found")
            return ToolResult.ok(record.as_dict(), tool=self.name)
        kinds = (str(kind).strip(),) if str(kind).strip() in MEMORY_KINDS else None
        records = memory.recent(capped, kinds=kinds)
        memory.touch(records)
        return ToolResult.ok(
            {
                "count": len(records),
                "records": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "content": item.content,
                        "tags": item.tags,
                        "pinned": item.pinned,
                        "age_days": round(max(0.0, time.time() - item.created_at) / 86400.0, 1),
                    }
                    for item in records
                ],
                "stats": memory.stats(),
            },
            tool=self.name,
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            properties={
                "id": {"type": "string", "description": "Exact record id (or unique prefix) to fetch one record."},
                "kind": {"type": "string", "enum": list(MEMORY_KINDS), "description": "Only this kind of record."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "How many records (default 10).",
                },
            },
        )


@register_tool
class MemorySearchTool(MemoryToolBase):
    """جست‌وجو در حافظه با رتبه‌بندی (واژگان + تازگی + pin)."""

    name: ClassVar[str] = "memory_search"
    description: ClassVar[str] = (
        "Search long-term memory by keywords (also matches tags). Use it to recover decisions, "
        "procedures, and user preferences from earlier sessions before asking the user again."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("query",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("kind", "limit")

    async def execute(  # type: ignore[override]
        self,
        query: str = "",
        kind: str = "",
        limit: int = 8,
        *,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """جست‌وجو و برگرداندن رکوردهای مرتبط."""
        memory, failure = self._store(context)
        if memory is None:
            return failure or ToolResult.fail(
                "long-term memory is unavailable", tool=self.name, error_code="disabled"
            )
        text = clean_text(query, max_length=400)
        if not text:
            return ToolResult.fail("query must not be empty", tool=self.name, error_code="invalid_input")
        capped = bounded_int(limit, name="limit", minimum=1, maximum=30, default=8)
        kinds = (str(kind).strip(),) if str(kind).strip() in MEMORY_KINDS else None
        records = memory.search(text, kinds=kinds, limit=capped)
        memory.touch(records)
        return ToolResult.ok(
            {
                "query": text,
                "count": len(records),
                "records": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "content": item.content,
                        "tags": item.tags,
                        "confidence": round(item.confidence, 2),
                    }
                    for item in records
                ],
            },
            tool=self.name,
            metadata={"matches": [item.id for item in records]},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("query",),
            properties={
                "query": {
                    "type": "string",
                    "description": "Keywords, e.g. 'deploy staging' or 'payment gateway documents'.",
                },
                "kind": {"type": "string", "enum": list(MEMORY_KINDS), "description": "Restrict to one kind."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Max results (default 8)."},
            },
        )


@register_tool
class MemoryForgetTool(MemoryToolBase):
    """حذف یک رکورد از حافظه (مثلاً وقتی یک ترجیح عوض شده)."""

    name: ClassVar[str] = "memory_forget"
    description: ClassVar[str] = (
        "Delete one memory record by id. Prefer writing the corrected note first; pinned records " "need force=true."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("id",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("force",)
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM

    async def execute(  # type: ignore[override]
        self,
        id: str = "",  # noqa: A002 - قرارداد API
        force: bool = False,
        *,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """حذف رکورد؛ اگر pinned بود و force نبود، رد می‌شود."""
        memory, failure = self._store(context)
        if memory is None:
            return failure or ToolResult.fail(
                "long-term memory is unavailable", tool=self.name, error_code="disabled"
            )
        record: MemoryRecord | None = memory.find(str(id).strip())
        if record is None:
            return ToolResult.fail(f"no memory record with id '{id}'", tool=self.name, error_code="not_found")
        if record.pinned and not force:
            return ToolResult.fail(
                "record is pinned; pass force=true if the user really wants it gone",
                tool=self.name,
                error_code="invalid_input",
            )
        removed = memory.forget(record.id)
        return ToolResult.ok(
            {"deleted": record.id, "kind": record.kind, "records": memory.stats()["records"] if removed else None},
            tool=self.name,
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("id",),
            properties={
                "id": {"type": "string", "description": "Record id from memory_search/memory_read."},
                "force": {"type": "boolean", "description": "Allow deleting a pinned record (default false)."},
            },
        )

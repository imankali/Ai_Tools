"""هسته‌ی سیستم (Core): قرارداد ابزارها، رجیستری، رویدادها و کارخانه‌ی ایجنت.

اجزای اصلی:

* :class:`src.core.base_tool.BaseTool` — کلاس پایه‌ی ابزارها (Strategy)
* :class:`src.core.tool_registry.ToolRegistry` — کاتالوگ ابزارها + discovery
* :class:`src.core.event_bus.EventBus` — ناشر/مشترک رویدادها (Observer)
* :class:`src.core.agent_factory.AgentFactory` — ساخت ایجنت با پروفایل (Factory)
"""

from __future__ import annotations

from src.core.base_tool import (
    BaseTool,
    ConfirmationDecision,
    ConfirmationRequest,
    ToolContext,
    ToolResult,
)
from src.core.event_bus import EVENTS, Event, EventBus
from src.core.tool_registry import ToolRegistry, register_tool, unregister_tool

__all__ = [
    "EVENTS",
    "BaseTool",
    "ConfirmationDecision",
    "ConfirmationRequest",
    "Event",
    "EventBus",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "register_tool",
    "unregister_tool",
]

"""Universal Agent Hub — ایجنت ماژولار هوش مصنوعی با دسترسی کنترل‌شده به سیستم.

این پکیج (``src``) نقطه‌ی ورود پروژه است و API عمومی را باز‌ارسال می‌کند تا
استفاده‌ی متداول یک خط باشد::

    import asyncio
    from src import AgentFactory, Config

    agent = AgentFactory.create("generalist")
    print(asyncio.run(agent.ask("Summarize what is in this folder")).text)

ساختار:

* :mod:`src.core` — هسته (پایه‌ی ابزار، رجیستری، کارخانه، ایونت‌باس)
* :mod:`src.tools` — ابزارها (هر ابزار یک فایل مستقل)
* :mod:`src.models` — مدل‌های داده (pydantic)
* :mod:`src.utils` — لاگ، ایمنی، اعتبارسنجی، کمکی‌ها
* :mod:`src.agent` / :mod:`src.cli` — ایجنت و رابط خط فرمان
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = [
    "AgentFactory",
    "BaseTool",
    "Config",
    "EventBus",
    "SafetyGuard",
    "ToolRegistry",
    "ToolResult",
    "UniversalAgent",
    "__version__",
    "get_config",
    "load_dotenv_if_available",
    "register_tool",
]


def load_dotenv_if_available(override: bool = False) -> bool:
    """بارگذاری ``.env`` در صورت نصب بودن python-dotenv (بی‌صدا اگر نبود).

    Args:
        override: متغیرهای محیطی موجود بازنویسی شوند.

    Returns:
        ``True`` اگر فایل بارگذاری شد.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(load_dotenv(override=override))


def __getattr__(name: str) -> object:
    """lazy re-export: از import سنگین وقت‌گیر در سطح package پرهیز می‌کند."""
    mapping = {
        "Config": ("src.config", "Config"),
        "get_config": ("src.config", "get_config"),
        "UniversalAgent": ("src.agent", "UniversalAgent"),
        "AgentFactory": ("src.core.agent_factory", "AgentFactory"),
        "ToolRegistry": ("src.core.tool_registry", "ToolRegistry"),
        "register_tool": ("src.core.tool_registry", "register_tool"),
        "BaseTool": ("src.core.base_tool", "BaseTool"),
        "ToolResult": ("src.models.tool_models", "ToolResult"),
        "EventBus": ("src.core.event_bus", "EventBus"),
        "SafetyGuard": ("src.utils.safety", "SafetyGuard"),
    }
    if name in mapping:  # pragma: no cover - مسیر راحتی
        module_name, attribute = mapping[name]
        import importlib

        value = getattr(importlib.import_module(module_name), attribute)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'src' has no attribute {name!r}")

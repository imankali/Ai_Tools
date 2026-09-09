"""fixtures مشترک تست‌ها.

استراتژی: هیچ تستی نباید به شبکه، کلید واقعی یا فایل‌سیستم بیرونی دست بزند.
بنابراین:

* :func:`workspace` — یک پوشه‌ی موقت که به‌عنوان تنها دایرکتوری مجاز تعریف می‌شود
* :func:`config` — تنظیمات ایزوله (بدون لاگ فایل، جست‌وجوی offline، تأیید روشن)
* :func:`context` — :class:`ToolContext` مشترک برای تست ابزارها
* :func:`fake_client` — client ساختگی سازگار با OpenAI برای تست حلقه‌ی ایجنت
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover - مسیر استاندارد
    sys.path.insert(0, str(ROOT))

from src.config import Config  # noqa: E402
from src.core.base_tool import ToolContext  # noqa: E402
from src.core.event_bus import EventBus  # noqa: E402
from src.core.tool_registry import ToolRegistry, discover_tools  # noqa: E402
from src.utils.safety import SafetyGuard  # noqa: E402
from tests.fakes import FakeOpenAIClient, make_chat_response  # noqa: E402

__all__ = ["FakeOpenAIClient", "make_chat_response"]


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """پوشه‌ی موقتِ ایزوله (و تنها دایرکتوری مجاز) برای تست فایل و ترمینال."""
    previous = Path.cwd()
    (tmp_path / "sample.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.py").write_text("VALUE = 42\n", encoding="utf-8")
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(previous)


@pytest.fixture
def config(workspace: Path) -> Config:
    """تنظیمات تست (بدون .env، بدون شبکه، فقط workspace مجاز)."""
    return Config(
        openai_api_key="sk-test-0000000000",
        model_name="gpt-6-astra",
        model_fallbacks=["gpt-4o-mini"],
        allowed_directories=[str(workspace)],
        project_root=workspace,
        log_file=None,
        log_level="WARNING",
        rich_console=False,
        enable_confirmation=True,
        search_backend="offline",
        max_command_timeout=10,
        events_log_file=None,
        parallel_tool_calls=True,
        # حافظه/گزارش روی پوشه‌ی موقت همان تست (تا home کاربر دست‌نخورده بماند)
        memory_enabled=True,
        memory_dir=str(workspace / "memory"),
        activity_log_file=str(workspace / "activity.jsonl"),
        reports_enabled=True,
    )


@pytest.fixture(autouse=True)
def _isolate_stateful_stores() -> Iterator[None]:
    """کش‌های AgentMemory/ActivityRecorder بین تست‌ها پاک شوند (shared state ممنوع)."""
    from src.core.memory import reset_memory_cache
    from src.core.reports import reset_recorder_cache

    reset_memory_cache()
    reset_recorder_cache()
    yield
    reset_memory_cache()
    reset_recorder_cache()


@pytest.fixture
def guard(config: Config) -> SafetyGuard:
    """نگهبان ایمنی ساخته‌شده از تنظیمات تست."""
    return SafetyGuard.from_config(config)


@pytest.fixture
def bus() -> EventBus:
    """ایونت‌باس تازه با تاریخچه‌ی کوتاه."""
    return EventBus(history_size=50)


@pytest.fixture
def context(config: Config, guard: SafetyGuard, bus: EventBus) -> ToolContext:
    """زمینه‌ی اجرای ابزار برای تست (تأیید همیشه مثبت، با ضبط درخواست‌ها)."""
    approved: list[str] = []

    def _confirm(request: Any) -> bool:
        approved.append(getattr(request, "tool", "?"))
        return True

    ctx = ToolContext(config=config, safety=guard, bus=bus, confirm=_confirm)
    ctx.approvals = approved  # type: ignore[attr-defined]
    return ctx


@pytest.fixture
def registered(config: Config) -> dict[str, Any]:
    """رجیستری ابزارها را پر می‌کند و ابزارها را با config تست می‌سازد."""
    ToolRegistry.set_default_config(config)
    discover_tools(force=True)
    return {"registry": ToolRegistry, "names": ToolRegistry.names()}


@pytest.fixture
def tool_factory(registered: dict[str, Any], config: Config) -> Callable[[str], Any]:
    """ساخت نمونه‌ی ابزار طبق نام با config تست."""

    def _factory(name: str) -> Any:
        tool = ToolRegistry.get(name, config=config)
        assert tool is not None, f"tool '{name}' is not registered"
        return tool

    return _factory


@pytest.fixture
def fake_client() -> Callable[..., FakeOpenAIClient]:
    """کارخانه‌ی client ساختگی."""
    return lambda responses, **kwargs: FakeOpenAIClient(responses, **kwargs)


@pytest.fixture
def response() -> Callable[..., Any]:
    """کارخانه‌ی پاسخ ساختگی."""
    return make_chat_response

"""تست سیستم لاگ‌گیری (:mod:`src.utils.logger`).

نکته: لاگر پروژه سراسری است، پس هر تست با یک fixture وضعیت را برمی‌گرداند تا
تست‌های دیگر هندلرها/سطح لاگ آلوده تحویل نگیرند.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.core.event_bus import EventBus
from src.utils import logger as lg
from src.utils.logger import (
    DATE_FORMAT,
    FILE_LOG_FORMAT,
    LOG_FORMAT,
    SecretRedactingFilter,
    _compact,
    _resolve_level,
    _supports_rich,
    attach_event_bus,
    get_logger,
    setup_logging,
)

ROOT = "universal_agent_hub"


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """قبل و بعد هر تست، هندلرها و فلگ «پیکربندی‌شده» را بازیابی کن."""
    root = logging.getLogger(ROOT)
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_propagate = root.propagate
    saved_configured = set(lg._configured)
    lg._configured.clear()  # تا هر تست از صفر پیکربندی شود (تست‌های CLI حالت سراسری را تغییر می‌دهند)
    noisy = {name: logging.getLogger(name).level for name in ("httpx", "openai", "urllib3", "asyncio", "playwright")}
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    root.propagate = saved_propagate
    lg._configured.clear()
    lg._configured.update(saved_configured)
    for name, level in noisy.items():
        logging.getLogger(name).setLevel(level)


class CapturingHandler(logging.Handler):
    """هندلری که رکوردها را برای assert نگه می‌دارد."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        """پیام نهایی (فرمت‌شده) همه‌ی رکوردها."""
        return [self.format(record) for record in self.records]


def capture(logger: logging.Logger, *, level: int = logging.DEBUG) -> CapturingHandler:
    """وصل کردن هندلگیر به یک لاگر."""
    handler = CapturingHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(levelname)s|%(name)s|%(message)s"))
    logger.addHandler(handler)
    return handler


# ---------------------------------------------------------------------------
# نام‌گذاری و سطح‌ها
# ---------------------------------------------------------------------------
def test_get_logger_prefixes_project_root() -> None:
    """لاگرهای ماژول‌ها زیر ریشه‌ی پروژه گروه می‌شوند."""
    assert get_logger().name == ROOT
    assert get_logger("tools.terminal").name == f"{ROOT}.tools.terminal"
    assert get_logger(ROOT).name == ROOT
    assert get_logger(f"{ROOT}.agent").name == f"{ROOT}.agent"
    assert get_logger("tools.terminal") is get_logger("tools.terminal")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("debug", logging.DEBUG),
        ("WARNING", logging.WARNING),
        (" info ", logging.INFO),
        (40, 40),
        ("not-a-level", logging.INFO),
        ("", logging.INFO),
    ],
)
def test_resolve_level(raw: Any, expected: int) -> None:
    """تبدیل سطح متنی/عددی با مقدار امن پیش‌فرض."""
    assert _resolve_level(raw) == expected


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------
def test_setup_logging_builds_console_handler(tmp_path: Path) -> None:
    """یک handler کنسولی، بدون فایل، با سطح درست."""
    logger = setup_logging("DEBUG", None, rich_console=False)
    assert logger.name == ROOT and logger.propagate is False
    assert logger.level == logging.DEBUG
    assert len(logger.handlers) == 1
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
    assert any(isinstance(item, SecretRedactingFilter) for item in handler.filters)
    assert _supports_rich() is False  # در pytest استدرم ترمینال نیست


def test_setup_logging_creates_rotating_file(tmp_path: Path) -> None:
    """فایل لاگ در پوشه‌ی خودش ساخته و با قالب فایل نوشته می‌شود."""
    target = tmp_path / "logs" / "agent.log"
    logger = setup_logging("INFO", target, rich_console=False)
    file_handlers = [item for item in logger.handlers if isinstance(item, logging.FileHandler)]
    assert len(file_handlers) == 1
    assert target.parent.is_dir() and target.exists()
    assert file_handlers[0].formatter is not None
    assert file_handlers[0].maxBytes == 2 * 1024 * 1024 and file_handlers[0].backupCount == 3
    get_logger("tools.demo").info("hello from test")
    content = target.read_text(encoding="utf-8")
    assert "hello from test" in content and "tools.demo" in content


def test_setup_logging_redacts_secrets_in_file(tmp_path: Path) -> None:
    """کلید API هیچ‌وقت در فایل لاگ خام نوشته نمی‌شود."""
    target = tmp_path / "agent.log"
    setup_logging("INFO", target, rich_console=False)
    get_logger("agent").info("using key sk-proletariate1234567890abcdefgh for auth")
    content = target.read_text(encoding="utf-8")
    assert "1234567890abcdefgh" not in content
    assert "[redacted]" in content and "sk-pro" in content


def test_setup_logging_respects_custom_rotation(tmp_path: Path) -> None:
    """سقف حجم و تعداد بکاپ از پارامترها گرفته می‌شود."""
    target = tmp_path / "agent.log"
    logger = setup_logging("INFO", target, rich_console=False, max_bytes=1234, backup_count=7)
    handler = next(item for item in logger.handlers if isinstance(item, logging.FileHandler))
    assert (handler.maxBytes, handler.backupCount) == (1234, 7)


def test_setup_logging_is_idempotent_without_force(tmp_path: Path) -> None:
    """فراخوانی دوم هندلر تکراری اضافه نمی‌کند (ولی سطح را به‌روز می‌کند)."""
    target = tmp_path / "agent.log"
    first = setup_logging("INFO", target, rich_console=False)
    handlers = list(first.handlers)
    second = setup_logging("ERROR", target, rich_console=False)
    assert second is first and first.handlers == handlers
    assert first.level == logging.ERROR


def test_setup_logging_force_rebuilds_handlers(tmp_path: Path) -> None:
    """force=True هندلرها را از نو می‌سازد."""
    target = tmp_path / "agent.log"
    logger = setup_logging("INFO", target, rich_console=False)
    before = list(logger.handlers)
    rebuilt = setup_logging("INFO", target, rich_console=False, force=True)
    assert rebuilt.handlers and rebuilt.handlers != before
    assert len(rebuilt.handlers) == 2  # کنسول + فایل


def test_setup_logging_survives_unwritable_file(tmp_path: Path) -> None:
    """مسیر غیرقابل‌write لاگ را خاموش می‌کند، نه اینکه اجرا را بشکند."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    logger = setup_logging("INFO", blocker / "sub" / "agent.log", rich_console=False)
    assert not any(isinstance(item, logging.FileHandler) for item in logger.handlers)
    assert len(logger.handlers) == 1


def test_setup_logging_uses_rich_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """در ترمینال رنگی، RichHandler جای StreamHandler می‌نشیند."""
    pytest.importorskip("rich")
    monkeypatch.setattr(lg, "_supports_rich", lambda: True)
    logger = setup_logging("INFO", None, rich_console=True, force=True)
    handler = logger.handlers[0]
    assert type(handler).__module__.startswith("rich")


def test_rich_failure_falls_back_to_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """اگر import rich خطا بدهد، به StreamHandler ساده برمی‌گردد."""
    monkeypatch.setattr(lg, "_supports_rich", lambda: True)
    monkeypatch.setitem(sys.modules, "rich.logging", None)  # import → ImportError
    logger = setup_logging("INFO", None, rich_console=True, force=True)
    assert isinstance(logger.handlers[0], logging.StreamHandler)
    assert not type(logger.handlers[0]).__module__.startswith("rich")


def test_noisy_third_party_loggers_are_quieted() -> None:
    """سکوت کتابخانه‌های شخص ثالث در سطح INFO."""
    setup_logging("INFO", None, rich_console=False, force=True)
    for name in ("httpx", "openai", "urllib3", "asyncio", "playwright"):
        assert logging.getLogger(name).level >= logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING


def test_debug_level_still_silences_third_parties() -> None:
    """حتی در DEBUG کتابخانه‌های شخص ثالث زیر WARNING نمی‌آیند (سقف عمداً WARNING است)."""
    setup_logging("DEBUG", None, rich_console=False, force=True)
    assert logging.getLogger("openai").level == logging.WARNING
    assert logging.getLogger(ROOT).level == logging.DEBUG


# ---------------------------------------------------------------------------
# فیلتر ماسک‌سازی
# ---------------------------------------------------------------------------
def test_secret_filter_masks_and_keeps_record() -> None:
    """فیلتر پیام را عوض می‌کند ولی رکورد را دور نمی‌اندازد."""
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "token=ghp_abcdef1234567890abcdef1234567890ab", (), None
    )
    assert SecretRedactingFilter().filter(record) is True
    assert "abcdef1234567890" not in record.getMessage() and "[redacted]" in record.getMessage()
    clean = logging.LogRecord("x", logging.INFO, __file__, 1, "nothing to hide", (), None)
    assert SecretRedactingFilter().filter(clean) is True and clean.getMessage() == "nothing to hide"


def test_secret_filter_tolerates_broken_record() -> None:
    """رکوردی که getMessage آن خطا می‌دهد نباید لاگ را down کند."""

    class Broken(logging.LogRecord):
        def getMessage(self) -> str:  # noqa: N802 - امضای stdlib
            raise RuntimeError("boom")

    assert SecretRedactingFilter().filter(Broken("x", logging.INFO, __file__, 1, "m", (), None)) is True


def test_filter_masks_args_records() -> None:
    """پیام‌های %-format هم ماسک می‌شوند (record.args پاک می‌شود)."""
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "key %s failed", ("sk-secret0123456789abcdefghij",), None
    )
    SecretRedactingFilter().filter(record)
    assert record.args == () and "0123456789abcdefghij" not in record.getMessage()


# ---------------------------------------------------------------------------
# اتصال به EventBus
# ---------------------------------------------------------------------------
def test_attach_event_bus_logs_events() -> None:
    """هر رویداد باس به یک خط لاگ تبدیل می‌شود."""
    logger = setup_logging("INFO", None, rich_console=False, force=True)
    handler = capture(logger)
    bus = EventBus(history_size=10)
    sub_id = attach_event_bus(bus, logger)
    assert sub_id
    bus.publish_nowait("tool.completed", {"tool": "terminal_run", "duration_ms": 12})
    bus.publish_nowait("agent.started", {})
    text = "\n".join(handler.messages)
    assert "tool.completed" in text and "terminal_run" in text
    assert "agent.started" in text


def test_attach_event_bus_uses_error_level_for_failures() -> None:
    """رویدادهای .failed/.error با سطح ERROR ثبت می‌شوند."""
    logger = setup_logging("INFO", None, rich_console=False, force=True)
    handler = capture(logger)
    bus = EventBus()
    attach_event_bus(bus, logger)
    bus.publish_nowait("tool.failed", {"error": "boom"})
    bus.publish_nowait("error", {"detail": "oops"})
    bus.publish_nowait("tool.progress", {"step": 1})
    levels = [record.levelno for record in handler.records]
    assert levels.count(logging.ERROR) == 2 and levels.count(logging.INFO) == 1


def test_attach_event_bus_redacts_payload(tmp_path: Path) -> None:
    """مقادیر حساس در payload هم در لاگ ماسک می‌شوند."""
    target = tmp_path / "events.log"
    setup_logging("INFO", target, rich_console=False, force=True)
    bus = EventBus()
    attach_event_bus(bus, logging.getLogger(f"{ROOT}.events"))
    bus.publish_nowait("llm.requested", {"api_key": "sk-verysecret1234567890abcdef", "model": "gpt-6-astra"})
    content = target.read_text(encoding="utf-8")
    assert "1234567890abcdef" not in content and "gpt-6-astra" in content


def test_unsubscribed_bus_stops_logging() -> None:
    """پس از unsubscribe لاگ نوشته نمی‌شود."""
    logger = setup_logging("INFO", None, rich_console=False, force=True)
    handler = capture(logger)
    bus = EventBus()
    sub_id = attach_event_bus(bus, logger)
    assert bus.unsubscribe("#", sub_id) is True
    handler.records.clear()
    bus.publish_nowait("tool.completed", {"tool": "x"})
    assert handler.records == []
    assert bus.unsubscribe("#", sub_id) is False


class BusStub:
    """باس مینیمال که هندلر را برای فراخوانی مستقیم نگه می‌دارد."""

    def __init__(self) -> None:
        self.handler: Any = None

    def subscribe(self, pattern: str, handler: Any, *, name: str | None = None) -> str:
        self.handler = handler
        return "sub-stub"


def test_observer_tolerates_objects_without_event_shape() -> None:
    """رویداد بدون kind/payload نباید لاگ‌گیر را بشکند."""
    logger = setup_logging("INFO", None, rich_console=False, force=True)
    handler = capture(logger)
    bus = BusStub()
    assert attach_event_bus(bus, logging.getLogger(f"{ROOT}.events")) == "sub-stub"

    class _Raw:
        def __str__(self) -> str:
            return "raw-event"

    bus.handler({"kind": "dict.event", "payload": {"x": 1}})
    bus.handler(_Raw())
    bus.handler(None)
    text = "\n".join(record.getMessage() for record in handler.records)
    assert "dict.event" in text and "raw-event" in text


# ---------------------------------------------------------------------------
# خلاصه‌سازی payload
# ---------------------------------------------------------------------------
def test_compact_skips_none_and_caps_length() -> None:
    """None حذف، هر مقدار تا ۱۲۰ کاراکتر و کل رشته تا limit."""
    assert _compact({"a": 1, "b": None, "c": "x"}) == "a=1 c=x"
    assert _compact({}) == ""
    long_value = "y" * 5000
    text = _compact({"data": long_value})
    assert text.startswith("data=yyy") and "truncated" in text and len(text) < 600
    many = {f"key_{index}": "v" * 50 for index in range(30)}
    capped = _compact(many, limit=200)
    assert len(capped) <= 205


def test_compact_represents_non_strings() -> None:
    """انواع غیررشته با repr نوشته می‌شوند."""
    assert _compact({"n": 5, "t": (1, 2), "d": {"k": "v"}}) == "n=5 t=(1, 2) d={'k': 'v'}"


def test_log_formats_are_used() -> None:
    """قالب‌های اعلام‌شده در هندلرها اعمال می‌شوند."""
    logger = setup_logging("INFO", None, rich_console=False, force=True)
    console = logger.handlers[0]
    assert isinstance(console.formatter, logging.Formatter)
    assert console.formatter._fmt == LOG_FORMAT and console.formatter.datefmt == DATE_FORMAT
    assert "%(msecs)03d" in FILE_LOG_FORMAT

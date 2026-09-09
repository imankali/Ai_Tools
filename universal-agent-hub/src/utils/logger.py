"""سیستم لاگ‌گیری (Logging) با پشتیبانی Rich و فایل چرخشی.

طراحی بر پایه‌ی **Observer pattern**:

* :func:`get_logger` یک ``logging.Logger`` استاندارد برمی‌گرداند (Console/Rich +
  یک ``RotatingFileHandler`` که فقط **یک بار** ساخته می‌شود).
* :func:`attach_event_bus` مشاهده‌گر لاگ را به EventBus وصل می‌کند تا رویدادهای
  ایجنت و ابزارها هم در لاگ ثبت شوند.
* :func:`redact_secrets` قبل از نوشتن، کلیدهای API را ماسک می‌کند.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.utils.helpers import redact_secrets

if TYPE_CHECKING:  # pragma: no cover - فقط برای type checking
    from src.core.event_bus import EventBus

__all__ = ["LOG_FORMAT", "SecretRedactingFilter", "attach_event_bus", "get_logger", "setup_logging"]

#: قالب استاندارد لاگ
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
#: قالب لاگ فایل (ثانیه‌های بیشتر برای عیب‌یابی)
FILE_LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-28s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_ROOT_NAME = "universal_agent_hub"
_configured: set[str] = set()


class SecretRedactingFilter(logging.Filter):
    """فیلتری که کلیدهای API را از پیام لاگ حذف می‌کند."""

    def filter(self, record: logging.LogRecord) -> bool:
        """ماسک کردن پیام لاگ (همیشه True برمی‌گرداند تا لاگ حذف نشود)."""
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - لاگ خراب نباید برنامه را از پا درآورد
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _supports_rich() -> bool:
    """آیا Rich در این ترمینال قابل استفاده است؟"""
    if not sys.stderr.isatty():
        return False
    try:
        import rich  # noqa: F401
    except ImportError:  # pragma: no cover - Rich اختیاری است
        return False
    return True


def setup_logging(
    level: str | int = "INFO",
    log_file: Path | None = None,
    *,
    rich_console: bool = True,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
    force: bool = False,
) -> logging.Logger:
    """پیکربندی logger ریشه‌ی پروژه.

    Args:
        level: سطح لاگ (نام یا عدد).
        log_file: مسیر فایل لاگ؛ ``None`` به معنای بدون فایل.
        rich_console: استفاده از ``RichHandler`` برای خروجی رنگی.
        max_bytes: حداکثر حجم فایل لاگ پیش از چرخش.
        backup_count: تعداد فایل‌های پشتیبان.
        force: اگر True باشد، هندلرهای قبلی حذف و دوباره ساخته می‌شوند.

    Returns:
        logger ریشه‌ی پروژه (``universal_agent_hub``).
    """
    logger = logging.getLogger(_ROOT_NAME)
    if _ROOT_NAME in _configured and not force:
        logger.setLevel(_resolve_level(level))
        return logger

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    if rich_console and _supports_rich():
        try:  # pragma: no cover - وابسته به محیط اجرا
            from rich.logging import RichHandler

            console_handler: logging.Handler = RichHandler(
                rich_tracebacks=True,
                show_path=False,
                omit_repeated_times=False,
                log_time_format=DATE_FORMAT,
            )
            console_handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
        except Exception:  # noqa: BLE001 - افت به handler ساده
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    else:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    console_handler.addFilter(SecretRedactingFilter())
    logger.addHandler(console_handler)

    if log_file is not None:
        try:
            from logging.handlers import RotatingFileHandler

            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT, DATE_FORMAT))
            file_handler.addFilter(SecretRedactingFilter())
            logger.addHandler(file_handler)
        except OSError as exc:
            logger.warning("File logging disabled (%s)", exc)

    _configured.add(_ROOT_NAME)
    # کاهش سر و صدای کتابخانه‌های شخص ثالث
    for noisy in ("httpx", "openai", "urllib3", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, _resolve_level(level)))
    return logger


def _resolve_level(level: str | int) -> int:
    """تبدیل سطح لاگ متنی به عدد (با مقدار امن پیش‌فرض)."""
    if isinstance(level, int):
        return level
    numeric = logging.getLevelName(str(level).strip().upper())
    return numeric if isinstance(numeric, int) else logging.INFO


def get_logger(name: str = _ROOT_NAME) -> logging.Logger:
    """دریافت logger با prefix پروژه.

    Args:
        name: معمولاً ``__name__`` ماژول صدا‌زننده.

    Returns:
        نمونه‌ی ``logging.Logger`` زیر ریشه‌ی پروژه.
    """
    if name == _ROOT_NAME or name.startswith(f"{_ROOT_NAME}."):
        return logging.getLogger(name)
    # ماژول‌های داخل src را زیر ریشه‌ی پروژه گروه می‌کنیم
    return logging.getLogger(f"{_ROOT_NAME}.{name}")


def attach_event_bus(bus: EventBus, logger: logging.Logger | None = None) -> str:
    """وصل کردن یک observer که رویدادها را در لاگ ثبت می‌کند.

    Args:
        bus: نمونه‌ی EventBus پروژه.
        logger: logger مقصد (پیش‌فرض: logger ماژول agent).

    Returns:
        شناسه‌ی subscription (برای لغو اشتراک).
    """
    log = logger or get_logger("events")

    def _on_event(event: Any) -> None:
        """مشاهده‌گر: هر رویداد باس را به یک خط لاگ تبدیل می‌کند."""
        kind = getattr(event, "kind", str(event))
        payload = getattr(event, "payload", event if isinstance(event, dict) else {})
        level = logging.ERROR if kind.endswith((".failed", ".error")) or kind == "error" else logging.INFO
        if log.isEnabledFor(level):
            log.log(level, "%s %s", kind, redact_secrets(_compact(dict(payload or {}))))

    return bus.subscribe("#", _on_event)


def _compact(payload: dict[str, Any], limit: int = 400) -> str:
    """خلاصه‌سازی payload رویداد برای یک خط لاگ."""
    from src.utils.helpers import truncate_text

    parts: list[str] = []
    for key, value in payload.items():
        if value is None:
            continue
        text = value if isinstance(value, str) else repr(value)
        parts.append(f"{key}={truncate_text(text, 120)[0]}")
    return truncate_text(" ".join(parts), limit)[0]

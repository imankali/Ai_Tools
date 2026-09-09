"""توابع کمکی عمومی (Generic helpers).

تابع‌های این ماژول عمداً سبک، خالص (pure) و بدون وابستگی به سایر بخش‌های
پروژه هستند تا بتوان در هر جا – از ابزارها تا تست‌ها – از آن‌ها استفاده کرد.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import html
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

__all__ = [
    "Timer",
    "chunked",
    "ensure_dir",
    "format_duration",
    "format_size",
    "now_iso",
    "redact_secrets",
    "run_in_thread",
    "safe_json_loads",
    "shorten_middle",
    "strip_html",
    "truncate_text",
]

T = TypeVar("T")

#: الگوی ساده برای شناسایی کلیدهای API در لاگ‌ها
_SECRET_RE = re.compile(
    r"(?i)\b("
    r"sk-[A-Za-z0-9_\-]{8,}"  # OpenAI / many OpenAI-compatible gateways
    r"|gsk_[A-Za-z0-9_\-]{8,}"  # Groq
    r"|tvly-[A-Za-z0-9_\-]{8,}"  # Tavily
    r"|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"  # GitHub
    r"|xox[baprs]-[A-Za-z0-9_\-]{8,}"  # Slack
    r"|AIza[0-9A-Za-z_\-]{8,}"  # Google
    r")\b"
)


def truncate_text(text: str, max_chars: int = 8000, *, suffix: str = "\n…[truncated]") -> tuple[str, bool]:
    """کوتاه کردن متن با حفظ ابتدا و انتها.

    Args:
        text: متن ورودی.
        max_chars: حداکثر طول خروجی ( شامل پسوند برش).
        suffix: برچسبی که انتهای متن بریده‌شده اضافه می‌شود.

    Returns:
        دوگانه‌ی ``(متن, آیا_بریده_شد)``.
    """
    if max_chars <= 0 or not isinstance(text, str):
        return (str(text), False)
    if len(text) <= max_chars:
        return text, False
    keep = max(0, max_chars - len(suffix))
    head = keep // 2
    tail = keep - head
    cut = text[:head] + suffix + (text[-tail:] if tail else "")
    return cut, True


def shorten_middle(text: str, max_chars: int = 120) -> str:
    """نمایش کوتاه‌شده‌ی یک رشته برای لاگ (مثلاً ``rm -rf … /tmp``)."""
    if len(text) <= max_chars or max_chars < 4:
        return text
    ellipsis = "…"
    budget = max_chars - len(ellipsis)
    head = budget - budget // 2
    tail = budget // 2
    return f"{text[:head]}{ellipsis}{text[-tail:] if tail else ''}"


def format_size(num_bytes: float) -> str:
    """تبدیل بایت به رشته‌ی خوانا (``1.4 KiB``)."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "n/a"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(size) < 1024.0 or unit == "PiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} PiB"  # pragma: no cover


def format_duration(seconds: float) -> str:
    """تبدیل ثانیه به رشته‌ی ``1h 2m 3s`` (یا میلی‌ثانیه برای مقادیر کوچک)."""
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    value = int(seconds)
    parts: list[str] = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if value >= size:
            parts.append(f"{value // size}{unit}")
            value %= size
    return " ".join(parts) or "0s"


def now_iso() -> str:
    """زمان فعلی به‌صورت ISO-8601 با منطقه‌ی زمانی UTC."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_json_loads(payload: Any) -> dict[str, Any]:
    """تبدیل امن ورودی مدل زبانی به dict.

    مدل‌ها گاهی JSON نامعتبر یا رشته‌ی خالی برمی‌گردانند. این تابع همیشه یک
    dict برمی‌گرداند و هیچ استثنایی raise نمی‌کند.

    Args:
        payload: رشته، dict یا None.

    Returns:
        dict قابل استفاده به‌عنوان kwargs ابزار.
    """
    if isinstance(payload, dict):
        return dict(payload)
    if payload is None:
        return {}
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    text = str(payload).strip()
    if not text or text in {"null", "None", "{}"}:
        return {}
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"input": parsed}
    # تلاش دوم: حذف بلوک‌های کد markdown
    fenced = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        parsed = json.loads(fenced)
        return parsed if isinstance(parsed, dict) else {"input": parsed}
    return {"_parse_error": True, "_raw": text}


def strip_html(markup: str, *, keep_links: bool = False) -> str:
    """تبدیل HTML به متن ساده (بدون وابستگی به BeautifulSoup).

    Args:
        markup: متن HTML.
        keep_links: اگر True باشد لینک‌ها به شکل ``[عنوان](url)`` حفظ می‌شوند.

    Returns:
        متن ساده با خطوط فشرده.
    """
    if not markup:
        return ""
    text = re.sub(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>", " ", markup)
    if keep_links:
        text = re.sub(r'(?is)<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r"[\2](\1)", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<li[^>]*>", "- ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank = False
    for line in lines:
        if not line:
            blank = True
            continue
        cleaned.append(("" if not cleaned and blank else ("\n" if blank else "")) + line)
        blank = False
    return "\n".join(cleaned).strip()


async def run_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """اجرای یک تابع blocking در نخ جدا (برای آزاد نگه‌داشتن event loop).

    ابزارهایی مثل ``requests`` یا ``psutil`` نسخه‌ی async ندارند؛ صدا زدن
    مستقیم آن‌ها در یک coroutine باعث قفل شدن کل ایجنت می‌شود، پس همه را با
    این wrapper در executor اجرا می‌کنیم.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


class Timer:
    """کانتکست‌منژر اندازه‌گیری زمان (میلی‌ثانیه).

    مثال:
        >>> with Timer() as timer:
        ...     pass
        >>> timer.ms >= 0
        True
    """

    def __init__(self) -> None:
        self._start: float = 0.0
        self.ms: int = 0
        self.elapsed: float = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.elapsed = time.perf_counter() - self._start
        self.ms = int(self.elapsed * 1000)


def ensure_dir(path: str | Path) -> Path:
    """ساخت دایرکتوری در صورت نیاز و بازگرداندن آن."""
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def chunked(items: Sequence[T], size: int) -> Iterable[list[T]]:
    """تکه‌تکه کردن یک فهرست به گروه‌های هم‌اندازه."""
    if size <= 0:
        raise ValueError("size must be positive")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def redact_secrets(text: str) -> str:
    """ماسک کردن کلیدهای API پیش از نوشتن در لاگ."""
    if not text:
        return text
    return _SECRET_RE.sub(lambda match: f"{match.group(0)[:6]}…[redacted]", text)


async def gather_or_timeout(awaitables: Sequence[Awaitable[Any]], timeout: float | None) -> list[Any]:
    """اجرای موازی با timeout اختیاری و خطای امن در هر مورد."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    if not tasks:
        return []
    if timeout:
        try:
            return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)
        except asyncio.TimeoutError:  # pragma: no cover - وابسته به زمان
            for task in tasks:
                task.cancel()
            return [RuntimeError(f"operation timed out after {timeout}s")] * len(tasks)
    return await asyncio.gather(*tasks, return_exceptions=True)


def env_flag(name: str, default: bool = False) -> bool:
    """خواندن یک فلگ بولی از محیط (``1/true/yes/on``)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def first_existing_dir(candidates: Iterable[str | Path]) -> Path | None:
    """اولین دایرکتوری موجود از فهرست پیشنهادی."""
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return None

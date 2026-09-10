"""اعتبارسنجی ورودی‌ها (Input validation).

تابع‌های این ماژول هیچ اثر جانبویی ندارند و فقط «صحت/رد» بودن ورودی را
بررسی می‌کنند. ابزارها پیش از اجرای منطق خود از این‌ها استفاده می‌کنند تا
پیام خطای واضح (بدون stack trace) به مدل زبانی برگردانند.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import socket
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "ValidationError",
    "bounded_int",
    "clean_text",
    "coerce_bool",
    "is_private_host",
    "is_safe_selector",
    "is_valid_cron",
    "is_valid_date",
    "is_valid_path",
    "is_valid_url",
    "is_valid_url_strict",
    "is_within",
    "mask_value",
    "safe_host_for_url",
    "validate_shell_tokens",
]

_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}
_URL_RE = re.compile(r"^https?://[^\s<>\"'`]+[^\s<>\"'`.,;:)]$", re.IGNORECASE)
_SELECTOR_RE = re.compile(r"^[#.]?[A-Za-z0-9_\-\[\]=\"'^*~> :.()|]+$")


class ValidationError(ValueError):
    """خطای اعتبارسنجی با پیام خوانا برای کاربر و مدل."""


def is_valid_url(url: Any, *, allowed_schemes: Sequence[str] = ("http", "https")) -> bool:
    """آیا ``url`` یک آدرس معتبر و ساده (بدون تزریق) است؟

    Args:
        url: مقدار ارسالی (معمولاً رشته).
        allowed_schemes: طرح‌های مجاز.

    Returns:
        ``True`` در صورت معتبر بودن.
    """
    try:
        return bool(is_valid_url_strict(str(url), allowed_schemes=allowed_schemes))
    except ValidationError:
        return False


def is_valid_url_strict(url: str, *, allowed_schemes: Sequence[str] = ("http", "https")) -> str:
    """بررسی دقیق URL و بازگرداندن همان URL نرمال‌شده.

    Raises:
        ValidationError: URL خالی، بدون scheme، دارای credentials یا با کاراکتر مشکوک.
    """
    cleaned = clean_text(url, max_length=2048)
    if not cleaned:
        raise ValidationError("URL is empty")
    if any(char in cleaned for char in ("`", "$(", "${", "\\", "\n", "|", ";", "&")):
        raise ValidationError("URL contains characters that are not allowed")
    if not _URL_RE.match(cleaned):
        raise ValidationError(f"URL does not look like a valid http(s) address: {cleaned[:80]!r}")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in allowed_schemes:
        raise ValidationError(f"URL scheme '{parsed.scheme}' is not allowed (use one of {list(allowed_schemes)})")
    if not parsed.netloc:
        raise ValidationError("URL is missing a host")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValidationError("Credentials embedded in URL are not allowed")
    return parsed.geturl()


def safe_host_for_url(url: str) -> str:
    """استخراج host از URL (برای تشخیص آدرس‌های داخلی/لوکال).

    Returns:
        نام host به‌صورت lowercase بدون نقطه‌ی انتهایی.

    Raises:
        ValidationError: اگر host قابل استخراج نباشد.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ValidationError("URL has no host component")
    return host


def is_private_host(url_or_host: str) -> bool:
    """آیا host مربوطه loopback/private/link-local است؟ (محافظت در برابر SSRF)"""
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        return True
    if host.endswith((".local", ".internal", ".lan", ".localdomain")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or (address.version == 6 and address.is_site_local)
    )


def is_valid_path(path: Any, *, must_exist: bool = False, max_length: int = 2048) -> Path:
    """اعتبارسنجی یک مسیر فایل‌سیستم.

    Args:
        path: مسیر خام.
        must_exist: اگر True باشد وجود مسیر هم بررسی می‌شود.
        max_length: حداکثر طول مجاز رشته‌ی مسیر.

    Returns:
        شیء :class:`pathlib.Path`.

    Raises:
        ValidationError: مسیر خالی/توخی/بلند.
    """
    text = str(path or "").strip()
    if not text:
        raise ValidationError("Path must not be empty")
    if len(text) > max_length:
        raise ValidationError(f"Path is too long ({len(text)} > {max_length})")
    if "\x00" in text:
        raise ValidationError("Path contains a NUL byte")
    candidate = Path(text).expanduser()
    resolved = candidate.resolve(strict=False)
    if must_exist and not resolved.exists():
        raise ValidationError(f"Path does not exist: {candidate}")
    return resolved


def is_within(child: Path, parents: Sequence[Path], *, follow_symlinks: bool = True) -> bool:
    """آیا ``child`` داخل یکی از ``parents`` قرار دارد؟

    Args:
        child: مسیر مورد بررسی (نسبی یا مطلق).
        parents: دایرکتوری‌های مجاز.
        follow_symlinks: برای مقصد symlink هم بررسی انجام شود.

    Returns:
        ``True`` اگر زیرمجموعه‌ی یکی از والدین باشد (خود والد هم مجاز است).
    """
    if not parents:
        return False
    try:
        target = child.resolve(strict=follow_symlinks) if follow_symlinks else child.resolve(strict=False)
    except (OSError, RuntimeError):  # pragma: no cover - مسیرهای شکسته
        target = Path(os.path.abspath(str(child)))  # noqa: PTH100 - عمداً symlink ها را resolve نمی‌کنیم
    for parent in parents:
        try:
            base = parent.resolve(strict=False)
        except (OSError, RuntimeError):  # pragma: no cover
            continue
        if target == base:
            return True
        try:
            target.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def bounded_int(value: Any, *, name: str, minimum: int, maximum: int, default: int | None = None) -> int:
    """تبدیل امن به عدد صحیح در یک بازه.

    Args:
        value: ورودی خام.
        name: نام پارامتر (برای پیام خطا).
        minimum: حداقل مجاز.
        maximum: حداکثر مجاز.
        default: مقدار پیش‌فرض اگر ورودی None باشد.

    Raises:
        ValidationError: ورودی عددی قابل تبدیل نیست یا خارج از بازه است.
    """
    if value is None or value == "":
        if default is None:
            raise ValidationError(f"Parameter '{name}' is required")
        value = default
    try:
        number = int(float(value)) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Parameter '{name}' must be an integer, got {value!r}") from exc
    if number < minimum or number > maximum:
        raise ValidationError(f"Parameter '{name}' must be between {minimum} and {maximum}, got {number}")
    return number


def is_valid_cron(expression: str, *, fields: int = 5) -> bool:
    """بررسی ساختاری ساده‌ی عبارت cron (تعداد فیلد و کاراکترهای مجاز).

    This is a deliberately lightweight check: it validates the shape of the
    expression without pretending to be a full cron parser.
    """
    if not expression or not isinstance(expression, str):
        return False
    parts = expression.split()
    if len(parts) != fields:
        return False
    allowed = set("*/,-0123456789 jan feb mar apr may jun jul aug sep oct nov dec sun mon tue wed thu fri sat")
    return all(all(char in allowed for char in part.lower()) for part in parts)


def is_safe_selector(selector: Any, *, max_length: int = 512) -> bool:
    """آیا selector CSS برای تزریق به JS ایمن است؟"""
    text = str(selector or "").strip()
    if not text or len(text) > max_length:
        return False
    if any(char in text for char in ("`", "$", "\\", "\n", "\r", "<", ">")):
        return False
    return bool(_SELECTOR_RE.match(text))


def validate_shell_tokens(command: str, *, max_length: int = 4000) -> list[str]:
    """تجزیه‌ی ایمن یک دستور shell به توکن‌ها (بدون اجرا).

    Args:
        command: دستور خام.
        max_length: حداکثر طول مجاز.

    Returns:
        فهرست توکن‌ها.

    Raises:
        ValidationError: دستور خالی، بلند، دارای newline یا quote بسته‌نشده.
    """
    text = str(command or "").strip()
    if not text:
        raise ValidationError("Command must not be empty")
    if len(text) > max_length:
        raise ValidationError(f"Command is too long ({len(text)} > {max_length})")
    if "\n" in text or "\r" in text:
        raise ValidationError("Multi-line commands are not allowed; pass a single command")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ValidationError(f"Unbalanced quotes in command: {exc}") from exc
    if not tokens:
        raise ValidationError("Command parsed to zero tokens")
    return tokens


def coerce_bool(value: Any, *, default: bool = False) -> bool:
    """تبدیل مقادیر گوناگون (str/int/bool) به بولین."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "بله", "ok"}:
        return True
    if text in {"0", "false", "no", "n", "off", "خیر"}:
        return False
    return default


def is_valid_date(value: Any, *, formats: Sequence[str] = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y")) -> date | None:
    """تبدیل رشته به :class:`datetime.date` یا ``None`` اگر نامعتبر باشد."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def clean_text(value: Any, *, max_length: int = 4096, strip_control: bool = True) -> str:
    """نرمال‌سازی متن: حذف کاراکترهای کنترلی، فاصله‌های اضافی و برش طول.

    Args:
        value: ورودی خام (هر نوع).
        max_length: حداکثر طول خروجی.
        strip_control: حذف کاراکترهای کنترلی غیر از newline/tab.

    Returns:
        رشته‌ی تمیز (همیشه بدون None).
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if strip_control:
        # فقط کاراکترهای کنترلی (زیر ۳۲ به‌جز newline/tab و DEL) حذف می‌شوند؛
        # backslash عمداً حفظ می‌شود تا مسیرهای ویندوزی خراب نشوند.
        text = "".join(char for char in text if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()[:max_length]


def mask_value(value: Any, *, keep: int = 4) -> str:
    """ماسک کردن مقدار حساس برای نمایش در گزارش‌ها."""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{'*' * 6}…{text[-keep:]}"

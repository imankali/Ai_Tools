"""احراز هویت و محافظت نرخ برای سرور اپ‌ها.

مدل امنیتی ساده و قابل توضیح:

* اگر ``SERVER_TOKEN`` تنظیم شده باشد، هر درخواست ``/api/*`` باید همان توکن را
  داشته باشد (هدر ``Authorization: Bearer``، ``X-Agent-Token`` یا ``?token=``).
* اگر توکن تنظیم **نشده** باشد، سرور فقط روی loopback کار می‌کند؛ باز شدن روی
  شبکه بدون توکن هنگام راه‌اندازی رد می‌شود (fail closed).
* توکن با :func:`hmac.compare_digest` مقایسه می‌شود تا زمان‌مقایسه‌ای نشت نکند.
"""

from __future__ import annotations

import hmac
import ipaddress
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RateLimiter", "TokenAuthorizer", "is_loopback_address", "unauthorized"]

from aiohttp import web


def is_loopback_address(value: str | None) -> bool:
    """آیا IP درخواست‌کننده روی همین ماشین است؟ (``::ffff:127.0.0.1`` هم بله)"""
    text = (value or "").strip()
    if text in {"", "localhost"}:
        return True
    if text.startswith("::ffff:") and len(text) > 7:
        text = text[7:]
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def unauthorized(message: str = "missing or invalid token") -> web.Response:
    """پاسخ ۴۰۱ یکدست (بدون افشای جزئیات توکن)."""
    return web.json_response({"error": message, "error_code": "unauthorized"}, status=401)


class TokenAuthorizer:
    """بررسی توکن دسترسی.

    Args:
        token: توکن مورد انتظار (خالی = فقط loopback مجاز است).
        allow_loopback_without_token: در حالت توسعه، درخواست‌های روی همین ماشین
            بدون توکن هم پذیرفته می‌شوند.
    """

    def __init__(self, token: str = "", *, allow_loopback_without_token: bool = True) -> None:
        self._token = str(token or "")
        self.allow_loopback_without_token = allow_loopback_without_token

    @classmethod
    def from_config(cls, config: Any) -> TokenAuthorizer:
        """ساخت از :class:`src.config.Config`."""
        return cls(str(getattr(config, "server_token", "") or ""))

    @property
    def enabled(self) -> bool:
        """آیا اصلاً توکنی لازم است؟"""
        return bool(self._token)

    @property
    def token(self) -> str:
        """خودِ توکن (فقط برای مقایسه؛ هیچ‌وقت در پاسخ برگردانده نمی‌شود)."""
        return self._token

    def token_from_request(self, request: web.Request) -> str:
        """استخراج توکن از هدرها یا پارامتر ``token`` (برای WebSocket/APK)."""
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        if header.lower().startswith("token "):
            return header[6:].strip()
        for name in ("X-Agent-Token", "X-Api-Key"):
            value = request.headers.get(name, "")
            if value:
                return value.strip()
        query = request.query.get("token", "")
        return str(query).strip()

    def is_allowed(self, request: web.Request) -> bool:
        """آیا این درخواست مجاز به ادامه است؟"""
        remote = request.remote
        if not self._token:
            return self.allow_loopback_without_token and is_loopback_address(remote)
        provided = self.token_from_request(request)
        if not provided:
            return False
        return hmac.compare_digest(provided, self._token)

    def describe(self) -> dict[str, Any]:
        """وضعیت احراز (برای ``/api/status`` — بدون افشای خودِ توکن)."""
        return {
            "token_required": self.enabled,
            "loopback_without_token": self.allow_loopback_without_token and not self.enabled,
            "token_length": len(self._token),
        }


@dataclass
class RateLimiter:
    """سقف درخواست در دقیقه، به ازای هر کلید (معمولاً IP).

    Args:
        per_minute: حداکثر تعداد مجاز در هر ۶۰ ثانیه.
        window_seconds: طول پنجره‌ی زمان.
    """

    per_minute: int = 30
    window_seconds: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_config(cls, config: Any) -> RateLimiter:
        """ساخت از config (سرعت پیش‌فرض ۳۰ اجرا در دقیقه)."""
        return cls(per_minute=int(getattr(config, "server_rate_limit_per_minute", 30) or 30))

    def allow(self, key: str) -> bool:
        """ثبت یک درخواست و تصمیم برای پذیرفتن یا رد آن."""
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= max(1, self.per_minute):
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> int:
        """چند ثانیه دیگر ظرفیت آزاد می‌شود؟ (برای هدر ``Retry-After``)."""
        bucket = self._hits.get(key)
        if not bucket:
            return 1
        now = time.monotonic()
        return max(1, int(self.window_seconds - (now - bucket[0])) + 1)

    def reset(self, key: str | None = None) -> None:
        """پاک کردن شمارنده‌ها (تست/دیباگ)."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)

    def snapshot(self) -> dict[str, int]:
        """تعداد درخواست‌های جاری هر کلید."""
        return {key: len(bucket) for key, bucket in self._hits.items()}

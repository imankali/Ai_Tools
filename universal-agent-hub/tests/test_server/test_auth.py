"""تست احراز توکن و محدودساز نرخ."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from multidict import CIMultiDict

from src.server.auth import RateLimiter, TokenAuthorizer, is_loopback_address, unauthorized


def make_request(headers: dict[str, str] | None = None, *, remote: str = "127.0.0.1", query: str = "") -> Any:
    """شبیه‌سازی سبک :class:`web.Request` (فقط چیزی که auth می‌خواند)."""
    pairs = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
    return SimpleNamespace(
        headers=CIMultiDict(headers or {}),
        query=SimpleNamespace(get=pairs.get),
        remote=remote,
        path="/api/status",
        method="GET",
    )


class TestLoopback:
    """تشخیص آدرس حلقه‌ی داخلی."""

    @pytest.mark.parametrize("value", ["127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1", "localhost", None, ""])
    def test_loopback_values(self, value: str | None) -> None:
        """loopback و مقادیر نامعتبر (بی‌خطر)."""
        assert is_loopback_address(value) is True

    @pytest.mark.parametrize("value", ["10.0.0.5", "192.168.1.20", "8.8.8.8", "::", "not-an-ip"])
    def test_non_loopback(self, value: str) -> None:
        """آدرس‌های شبکه loopback نیستند."""
        assert is_loopback_address(value) is False


class TestTokenAuthorizer:
    """منطق پذیرش/رد درخواست."""

    def test_disabled_allows_loopback_only(self) -> None:
        """بدون توکن: loopback آزاد، شبکه رد."""
        authorizer = TokenAuthorizer("")
        assert authorizer.enabled is False
        assert authorizer.is_allowed(make_request(remote="127.0.0.1")) is True
        assert authorizer.is_allowed(make_request(remote="10.0.0.9")) is False

    def test_disabled_can_deny_loopback(self) -> None:
        """حالت سخت‌گیر (بدون استثنا برای loopback)."""
        authorizer = TokenAuthorizer("", allow_loopback_without_token=False)
        assert authorizer.is_allowed(make_request(remote="127.0.0.1")) is False

    def test_bearer_and_header_variants(self) -> None:
        """Bearer / Token / X-Agent-Token / X-Api-Key همه پذیرفته می‌شوند."""
        authorizer = TokenAuthorizer("s3cr3t")
        for headers in (
            {"Authorization": "Bearer s3cr3t"},
            {"authorization": "bearer s3cr3t"},
            {"Authorization": "Token s3cr3t"},
            {"X-Agent-Token": "s3cr3t"},
            {"X-Api-Key": "s3cr3t"},
        ):
            assert authorizer.is_allowed(make_request(headers=headers, remote="10.1.1.1")) is True, headers

    def test_wrong_or_missing_token_denied(self) -> None:
        """توکن اشتباه یا غایب → رد (حتی از loopback)."""
        authorizer = TokenAuthorizer("s3cr3t")
        assert authorizer.is_allowed(make_request(headers={"Authorization": "Bearer nope"})) is False
        assert authorizer.is_allowed(make_request()) is False

    def test_query_token_extracted(self) -> None:
        """پارامتر ``?token=`` هم خوانده می‌شود (WebSocket نمی‌تواند هدر بفرستد)."""
        authorizer = TokenAuthorizer("abc")
        request = make_request(remote="10.0.0.2", query="token=abc")
        assert authorizer.token_from_request(request) == "abc"
        assert authorizer.is_allowed(request) is True

    def test_from_config_and_describe(self, app_config: Any) -> None:
        """ساخت از config و describe بدون افشای توکن."""
        authorizer = TokenAuthorizer.from_config(app_config.model_copy(update={"server_token": "topsecret"}))
        assert authorizer.enabled is True
        described = authorizer.describe()
        assert described["token_required"] is True
        assert described["token_length"] == 9
        assert "topsecret" not in repr(described)


def test_unauthorized_response_shape() -> None:
    """ساختار پاسخ ۴۰."""
    response = unauthorized("nope")
    assert response.status == 401
    assert b"unauthorized" in response.body
    assert b"nope" in response.body


class TestRateLimiter:
    """محدودساز نرخ."""

    def test_allows_up_to_limit(self) -> None:
        """تا سقف مجاز اجازه می‌دهد و بعد رد می‌کند."""
        limiter = RateLimiter(per_minute=3)
        assert [limiter.allow("1.2.3.4") for _ in range(4)] == [True, True, True, False]

    def test_keys_are_independent(self) -> None:
        """هر IP شمارش جدا دارد."""
        limiter = RateLimiter(per_minute=1)
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False
        assert limiter.allow("b") is True

    def test_reset_and_snapshot(self) -> None:
        """ریست یک کلید یا همه، و snapshot."""
        limiter = RateLimiter(per_minute=1)
        limiter.allow("a")
        limiter.allow("b")
        assert limiter.snapshot()["a"] == 1
        limiter.reset("a")
        assert limiter.allow("a") is True
        limiter.reset()
        assert limiter.snapshot() == {}

    def test_retry_after_counts_down(self) -> None:
        """retry_after یک عدد ثانیه‌ای مثبت می‌دهد."""
        limiter = RateLimiter(per_minute=1, window_seconds=60.0)
        limiter.allow("a")
        assert limiter.allow("a") is False
        assert 0 < limiter.retry_after("a") <= 60

    def test_window_expiry(self) -> None:
        """پس از پنجره‌ی زمانی، ظرفیت برمی‌گردد."""
        limiter = RateLimiter(per_minute=1, window_seconds=0.01)
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False
        import time

        time.sleep(0.02)
        assert limiter.allow("a") is True

    def test_from_config(self, app_config: Any) -> None:
        """ساخت از config."""
        limiter = RateLimiter.from_config(app_config.model_copy(update={"server_rate_limit_per_minute": 7}))
        assert limiter.per_minute == 7

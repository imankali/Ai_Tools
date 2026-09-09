"""تست ابزارهای اعتبارسنجی (:mod:`src.utils.validators`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.validators import (
    ValidationError,
    bounded_int,
    clean_text,
    coerce_bool,
    is_private_host,
    is_safe_selector,
    is_valid_cron,
    is_valid_date,
    is_valid_path,
    is_valid_url,
    is_valid_url_strict,
    is_within,
    mask_value,
    safe_host_for_url,
    validate_shell_tokens,
)


class TestUrls:
    """اعتبارسنجی URL ها."""

    @pytest.mark.parametrize(
        "url",
        ["https://example.com", "http://localhost:8000/x?a=1", "https://docs.python.org/3/library/asyncio.html"],
    )
    def test_accepts_common_urls(self, url: str) -> None:
        """URL‌ های معمول باید معتبر باشند."""
        assert is_valid_url(url)
        assert is_valid_url_strict(url) == url

    @pytest.mark.parametrize(
        "url",
        ["", "example.com", "ftp://example.com", "https://exa mple.com", "javascript:alert(1)"],
    )
    def test_rejects_invalid_urls(self, url: str) -> None:
        """URL‌های ناقص/ناامن باید رد شوند."""
        assert not is_valid_url(url)

    def test_rejects_injection_markers(self) -> None:
        """کاراکترهای پوسته‌ای در URL مجاز نیستند."""
        with pytest.raises(ValidationError, match="characters that are not allowed"):
            is_valid_url_strict("https://example.com/`whoami`")
        with pytest.raises(ValidationError, match="characters that are not allowed"):
            is_valid_url_strict("https://example.com/$(whoami)")  # marker های پوسته رد می‌شوند
        with pytest.raises(ValidationError, match="valid http"):
            is_valid_url_strict("https://exa mple.com")  # فاصله وسط آدرس
        with pytest.raises(ValidationError, match="Credentials"):
            is_valid_url_strict("https://user:pass@example.com")

    def test_host_extraction(self) -> None:
        """استخراج host و تشخیص آدرس داخلی."""
        assert safe_host_for_url("https://Docs.Python.org/x") == "docs.python.org"
        assert is_private_host("http://127.0.0.1:8080/admin")
        assert is_private_host("http://localhost")
        assert not is_private_host("https://example.com")


class TestPaths:
    """اعتبارسنجی مسیرها."""

    def test_valid_relative_path(self, workspace: Path) -> None:
        """مسیر نسبی داخل پوشه‌ی جاری معتبر است."""
        resolved = is_valid_path("sample.txt")
        assert resolved.name == "sample.txt"

    def test_rejects_empty_and_nul(self) -> None:
        """مسیر خالی یا دارای NUL رد می‌شود."""
        with pytest.raises(ValidationError, match="must not be empty"):
            is_valid_path("   ")
        with pytest.raises(ValidationError, match="NUL"):
            is_valid_path("/tmp/a\x00b")

    def test_rejects_too_long(self) -> None:
        """مسیر غیرمتعارف رد می‌شود."""
        with pytest.raises(ValidationError, match="too long"):
            is_valid_path("a" * 5000)

    def test_missing_when_required(self) -> None:
        """must_exist=True وجود فایل را هم بررسی می‌کند."""
        with pytest.raises(ValidationError, match="does not exist"):
            is_valid_path("/definitely/not/here.txt", must_exist=True)

    def test_is_within(self, workspace: Path) -> None:
        """بررسی زیرمجموعه بودن مسیر (شامل خود والد)."""
        inside = workspace / "nested" / "deep.py"
        assert is_within(inside, [workspace])
        assert is_within(workspace, [workspace])
        assert not is_within(Path("/etc/passwd"), [workspace])
        assert not is_within(Path("/etc/passwd"), [])


class TestNumbersAndText:
    """اعداد، بولین، تاریخ و متن."""

    def test_bounded_int_accepts_range(self) -> None:
        """مقادیر داخل بازه پذیرفته می‌شوند."""
        assert bounded_int("12", name="x", minimum=1, maximum=20) == 12
        assert bounded_int(None, name="x", minimum=1, maximum=20, default=5) == 5

    @pytest.mark.parametrize("value", [0, 21, "abc"])
    def test_bounded_int_rejects_bad_values(self, value: object) -> None:
        """خارج از بازه یا غیرعددی خطا می‌دهد."""
        with pytest.raises(ValidationError):
            bounded_int(value, name="x", minimum=1, maximum=20)

    def test_coerce_bool_variants(self) -> None:
        """تبدیل رشته‌های رایج به بولین."""
        assert coerce_bool("yes") and coerce_bool(True) and coerce_bool("1")
        assert not coerce_bool("off") and not coerce_bool("خیر")
        assert coerce_bool("maybe", default=True)

    def test_is_valid_date(self) -> None:
        """پارس چند قالب تاریخی."""
        assert is_valid_date("2026-01-31").isoformat() == "2026-01-31"  # type: ignore[union-attr]
        assert is_valid_date("31-01-2026") is not None
        assert is_valid_date("not-a-date") is None
        assert is_valid_date("") is None

    def test_clean_text_strips_control_chars(self) -> None:
        """کاراکترهای کنترلی حذف و طول محدود می‌شود."""
        assert clean_text("a\x00b\x1bc") == "abc"  # NUL و ESC حذف، کاراکتر چاپی می‌ماند
        assert len(clean_text("x" * 100, max_length=10)) == 10
        assert clean_text(None) == ""

    def test_mask_value(self) -> None:
        """ماسک مقادیر حساس."""
        assert mask_value("sk-1234567890abcdef") == "sk-1…******…cdef"
        assert mask_value("abc") == "***"
        assert mask_value("") == ""


class TestSelectorsAndShell:
    """Selector و تجزیه‌ی دستور."""

    def test_selector_rules(self) -> None:
        """selectorهای مجاز و مخرب."""
        assert is_safe_selector("#login")
        assert is_safe_selector('input[name="user"]')
        assert not is_safe_selector("a`whoami`")
        assert not is_safe_selector("span'); alert(1); //")
        assert not is_safe_selector("")

    def test_cron_shape(self) -> None:
        """بررسی ساختار cron."""
        assert is_valid_cron("*/5 * * * *")
        assert is_valid_cron("0 3 * * mon")
        assert not is_valid_cron("* * *")
        assert not is_valid_cron("; rm -rf / * * * *")
        assert not is_valid_cron("")

    def test_validate_shell_tokens(self) -> None:
        """تجزیه‌ی دستور به توکن‌ها."""
        assert validate_shell_tokens("git commit -m 'fix bug'") == ["git", "commit", "-m", "fix bug"]
        with pytest.raises(ValidationError, match="empty"):
            validate_shell_tokens("")
        with pytest.raises(ValidationError, match="Multi-line"):
            validate_shell_tokens("echo a\necho b")
        with pytest.raises(ValidationError, match="Unbalanced quotes"):
            validate_shell_tokens("echo 'oops")

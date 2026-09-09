"""تست توابع کمکی (:mod:`src.utils.helpers`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.utils.helpers import (
    Timer,
    chunked,
    ensure_dir,
    env_flag,
    first_existing_dir,
    format_duration,
    format_size,
    gather_or_timeout,
    now_iso,
    redact_secrets,
    run_in_thread,
    safe_json_loads,
    shorten_middle,
    strip_html,
    truncate_text,
)


def test_truncate_text_keeps_both_ends() -> None:
    """برش متن، ابتدا و انتها را نگه می‌دارد."""
    text = "A" * 100
    result, cut = truncate_text(text, 40)
    assert cut and len(result) <= 40 and result.startswith("AAA") and result.endswith("AAA")
    assert truncate_text("short", 40) == ("short", False)
    assert truncate_text("x", 0) == ("x", False)


def test_shorten_middle() -> None:
    """نمایش کوتاه وسط‌بریده."""
    assert shorten_middle("abcdefghijklm", 8) == "abcd…klm"
    assert shorten_middle("abcdefg", 5) == "ab…fg"
    assert shorten_middle("ab", 8) == "ab"
    assert shorten_middle("abc", 10) == "abc"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (1024**3, "1.0 GiB")],
)
def test_format_size(value: float, expected: str) -> None:
    """تبدیل بایت به واحد خوانا."""
    assert format_size(value) == expected


def test_format_size_invalid() -> None:
    """ورودی نامعتبر باعث خطا نمی‌شود."""
    assert format_size("nonsense") == "n/a"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.012, "12 ms"), (30, "30s"), (90, "1m 30s"), (7325, "2h 2m 5s")],
)
def test_format_duration(seconds: float, expected: str) -> None:
    """قالب‌بندی مدت زمان."""
    assert format_duration(seconds) == expected


def test_now_iso_is_utc() -> None:
    """زمان ISO با منطقه‌ی UTC."""
    value = now_iso()
    assert "+00:00" in value and len(value) == 25


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 2}\n```', {"a": 2}),
        ("", {}),
        ("null", {}),
        ("[]", {"input": []}),
        ("not json", {"_parse_error": True, "_raw": "not json"}),
        ({"already": "dict"}, {"already": "dict"}),
        (None, {}),
    ],
)
def test_safe_json_loads(payload: object, expected: dict[str, object]) -> None:
    """تجزیه‌ی امن JSON ورودی مدل."""
    assert safe_json_loads(payload) == expected


def test_strip_html_removes_noise() -> None:
    """تبدیل HTML به متن ساده."""
    markup = """
    <html><head><title>T</title><script>var x=1;</script><style>a{}</style></head>
    <body><h1>Header</h1><p>First line.</p><ul><li>one</li><li>two</li></ul>
    <a href="https://example.com">link</a>&amp;more</body></html>
    """
    text = strip_html(markup)
    assert "Header" in text and "First line." in text and "var x=1" not in text
    assert "- one" in text and "&more" in text and "<" not in text
    assert strip_html("") == ""


def test_strip_html_keep_links() -> None:
    """حفظ لینک‌ها به شکل markdown."""
    text = strip_html('<p>see <a href="https://x.io">docs</a></p>', keep_links=True)
    assert "[docs](https://x.io)" in text


async def test_run_in_thread_does_not_block_loop() -> None:
    """اجرای تابع blocking در نخ جدا."""
    counter = {"value": 0}

    def blocking() -> str:
        counter["value"] = 1
        return "done"

    async def ticker(stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(0.005)
            counter["value"] += 10

    stop = asyncio.Event()
    task = asyncio.create_task(ticker(stop))
    result = await run_in_thread(blocking)
    stop.set()
    await task
    assert result == "done"
    assert counter["value"] >= 11  # ticker هم اجرا شده است


def test_timer_measures_monotonic_time() -> None:
    """Timer زمان را به میلی‌ثانیه می‌شمارد."""
    with Timer() as timer:
        total = sum(range(1000))
    assert total == 499500
    assert timer.ms >= 0 and timer.elapsed > 0


def test_ensure_dir_and_first_existing(tmp_path: Path) -> None:
    """ساخت پوشه و یافتن اولین مسیر موجود."""
    created = ensure_dir(tmp_path / "a" / "b")
    assert created.is_dir()
    assert first_existing_dir([tmp_path / "nope", tmp_path]) == tmp_path
    assert first_existing_dir([tmp_path / "missing"]) is None


def test_chunked_and_errors() -> None:
    """تکه‌تکه‌کردن و خطای اندازه."""
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError, match="positive"):
        list(chunked([1], 0))


def test_redact_secrets() -> None:
    """ماسک کلیدها در متن لاگ."""
    message = "using key sk-abcdef123456 and tvly-0192837465"
    result = redact_secrets(message)
    assert "sk-abcdef123456" not in result and "tvly-0192837465" not in result
    assert "[redacted]" in result


def test_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """خواندن فلگ بولی از محیط."""
    monkeypatch.setenv("MY_FLAG", "yes")
    assert env_flag("MY_FLAG")
    monkeypatch.setenv("MY_FLAG", "0")
    assert not env_flag("MY_FLAG", default=True)
    monkeypatch.delenv("MY_FLAG", raising=False)
    assert env_flag("MY_FLAG", default=True)


async def test_gather_or_timeout_collects_errors() -> None:
    """خطاها به‌جای raise، در نتیجه جمع می‌شوند."""

    async def boom() -> None:
        raise RuntimeError("nope")

    async def fine() -> int:
        return 1

    results = await gather_or_timeout([fine(), boom()], timeout=None)
    assert results[0] == 1
    assert isinstance(results[1], RuntimeError)
    assert await gather_or_timeout([], timeout=1) == []

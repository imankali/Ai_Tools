"""تست ابزارهای مرور وب (:mod:`src.tools.browser`).

Playwright در محیط تست نصب نیست، بنابراین:

* مسیرهای نیازمند مرورگر با fake page/session (بدون مرورگر واقعی) سنجیده می‌شوند؛
* رفتار «Playwright نصب نیست» و پیام راهنمای آن هم بررسی می‌شود؛
* fallback سبک HTTP با یک سرور aiohttp محلی (تست integration) تست می‌شود.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.config import Config
from src.core.base_tool import ToolContext
from src.tools import browser as br
from src.tools.browser import (
    PLAYWRIGHT_HINT,
    SESSION_KEY,
    BrowserClickTool,
    BrowserExtractTextTool,
    BrowserFillFormTool,
    BrowserScreenshotTool,
    BrowserSession,
    BrowserTool,
    fetch_url,
    playwright_available,
)

ALL_TOOLS = (BrowserTool, BrowserScreenshotTool, BrowserExtractTextTool, BrowserClickTool, BrowserFillFormTool)
#: ورودی حداقلِ مجاز هر ابزار (تا validate_input روی ابزار دیگر اثر نگذارد)
MINIMAL_INPUT: dict[str, dict[str, Any]] = {
    "browser_browse": {"url": "https://example.com"},
    "browser_screenshot": {"url": "https://example.com"},
    "browser_extract_text": {"url": "https://example.com"},
    "browser_click": {"url": "https://example.com", "selector": "#a"},
    "browser_fill_form": {"url": "https://example.com", "data": {"#a": "b"}},
}


# ---------------------------------------------------------------------------
# دابل‌های تست
# ---------------------------------------------------------------------------
class FakePage:
    """جای‌نمای ``playwright`` Page (فقط متدهایی که ابزارها صدا می‌زنند)."""

    def __init__(
        self, *, text: str = "Hello page", title: str = "Fake Title", error: BaseException | None = None
    ) -> None:
        self.text = text
        self._title = title
        self.error = error
        self.url = "https://site.test/page"
        self.goto_urls: list[str] = []
        self.clicks: list[str] = []
        self.fills: dict[str, str] = {}
        self.waits: list[int] = []
        self.viewports: list[dict[str, int]] = []
        self.screenshot_paths: list[str] = []
        self.closed = False

    async def goto(self, url: str, *, wait_until: str = "") -> None:
        if self.error:
            raise self.error
        self.goto_urls.append(url)
        self.url = url

    async def title(self) -> str:
        return self._title

    async def click(self, selector: str) -> None:
        self.clicks.append(selector)

    async def fill(self, selector: str, value: str) -> None:
        self.fills[selector] = value

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(int(ms))

    async def wait_for_selector(self, selector: str) -> None:
        self.clicks.append(f"wait:{selector}")

    async def set_viewport_size(self, size: dict[str, int]) -> None:
        self.viewports.append(dict(size))

    async def evaluate(self, script: str) -> str:
        return self.text

    async def inner_text(self) -> str:
        return self.text

    def locator(self, selector: str) -> FakePage:
        return self

    async def expect_navigation(self, *, timeout: int = 0) -> Any:
        """context manager ساختگی برای ``async with page.expect_navigation()``."""

        class _Ctx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *args: Any) -> None:
                return None

        return _Ctx()

    async def screenshot(self, *, path: str | None = None, full_page: bool = False) -> bytes:
        data = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
        if path:
            Path(path).write_bytes(data)
        self.screenshot_paths.append(str(path or "<bytes>"))
        return data

    def is_closed(self) -> bool:
        return self.closed


class FakeSession:
    """جای‌نمای :class:`BrowserSession`."""

    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.actions: list[dict[str, Any]] = []
        self.closed = False

    @property
    def age_seconds(self) -> float:
        return 1.5

    @property
    def started(self) -> bool:
        return True

    async def get_page(self) -> FakePage:
        return self.page

    async def record(self, action: str, url: str = "", **extra: Any) -> None:
        self.actions.append({"action": action, "url": url, **extra})

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def page() -> FakePage:
    """صفحه‌ی ساختگی پیش‌فرض."""
    return FakePage()


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch, page: FakePage) -> FakeSession:
    """تزریق session ساختگی و در دسترس نشان دادن Playwright."""
    fake = FakeSession(page)

    async def fake_acquire(cls: Any, context: ToolContext | None, config: Any) -> FakeSession:
        if context is not None:
            context.session[SESSION_KEY] = fake
        return fake

    monkeypatch.setattr(br, "playwright_available", lambda: True)
    monkeypatch.setattr(BrowserSession, "acquire", classmethod(fake_acquire))
    return fake


def browse(**kwargs: Any) -> dict[str, Any]:
    """ساخت ورودی ``browser_browse``."""
    return {"url": "https://site.test/page", **kwargs}


def confirm_ctx(config: Config, *, approved: bool = True, guard: Any = None) -> ToolContext:
    """ساخت context با/بدون نگهبان و با پاسخ تأیید مشخص."""
    return ToolContext(config=config, safety=guard, confirm=lambda request: approved)


# ---------------------------------------------------------------------------
# نبود Playwright
# ---------------------------------------------------------------------------
@pytest.mark.skipif(playwright_available(), reason="Playwright نصب است؛ این تست برای محیط بدون آن است")
@pytest.mark.parametrize(
    "tool_class", [value for value in ALL_TOOLS if value.needs_playwright], ids=lambda value: value.name
)
async def test_actions_needing_playwright_explain_how_to_install(config: Config, guard: Any, tool_class: Any) -> None:
    """به‌جای خطای مبهم، دستور نصب دقیق برگردانده می‌شود."""
    # ابزارهایی که تأیید می‌خواهند (fill_form) با context تأییدکننده تست می‌شوند
    ctx = confirm_ctx(config, approved=True, guard=guard)
    result = await tool_class(config).run(MINIMAL_INPUT[tool_class.name], context=ctx)
    assert not result.success
    assert result.error_code == "playwright_missing"
    assert "pip install playwright" in (result.error or "")
    assert result.metadata["install"].startswith("pip install playwright")


async def test_extract_text_works_without_playwright(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """متن‌خوانی با HTTP fallback (بدون مرورگر) انجام می‌شود."""

    async def fake_fetch(url: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "<title>T</title><h1>Hi</h1>", {
            "status": 200,
            "content_type": "text/html",
            "bytes": 26,
            "final_url": url,
        }

    monkeypatch.setattr(br, "fetch_url", fake_fetch)
    result = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/x"}, context=ToolContext(config=config)
    )
    assert result.success and result.data["mode"] == "http"
    assert "Hi" in result.data["text"] and result.data["title"] == "T"
    assert BrowserExtractTextTool(config).needs_playwright is False


# ---------------------------------------------------------------------------
# browser_browse با صفحه‌ی ساختگی
# ---------------------------------------------------------------------------
async def test_browse_navigate(session: FakeSession, page: FakePage, config: Config) -> None:
    """ناوبری ساده: عنوان + URL + ثبت در تاریخچه‌ی session."""
    result = await BrowserTool(config).run(browse(), context=ToolContext(config=config))
    assert result.success
    assert result.data["title"] == "Fake Title"
    assert result.data["url"] == "https://site.test/page"
    assert result.data["session_age_seconds"] == 1.5
    assert result.metadata["action"] == "navigate" and result.metadata["url"] == "https://site.test/page"
    assert page.goto_urls == ["https://site.test/page"]
    assert session.actions[0]["action"] == "navigate"


async def test_browse_unknown_action_is_invalid(config: Config) -> None:
    """action ناشناخته → لیست مجازها."""
    result = await BrowserTool(config).run(browse(action="teleport"), context=ToolContext(config=config))
    assert not result.success and result.error_code == "invalid_input"
    assert "extract_text" in (result.error or "") and "navigate" in (result.error or "")


async def test_browse_extract_text_action(session: FakeSession, config: Config) -> None:
    """استخراج متن با سقف کاراکتر و پرچم برش."""
    session.page.text = "\n".join(f"row {index} of documentation" for index in range(400))
    full = await BrowserTool(config).run(
        browse(action="extract_text", max_chars=20_000), context=ToolContext(config=config)
    )
    assert full.success and "row 399 of documentation" in full.data["text"]
    assert full.data["truncated"] is False
    cut = await BrowserTool(config).run(
        browse(action="extract_text", max_chars=500), context=ToolContext(config=config)
    )
    assert cut.success and cut.data["truncated"] is True and len(cut.data["text"]) <= 500


async def test_browse_click_requires_selector(session: FakeSession, page: FakePage, config: Config) -> None:
    """کلیک بدون selector رد می‌شود؛ با selector انجام می‌شود."""
    missing = await BrowserTool(config).run(browse(action="click"), context=ToolContext(config=config))
    assert not missing.success and "requires a CSS selector" in (missing.error or "")
    ok = await BrowserTool(config).run(
        browse(action="click", selector="button.submit"), context=ToolContext(config=config)
    )
    assert ok.success and page.clicks == ["button.submit"]
    assert session.actions[-1]["selector"] == "button.submit" and session.actions[-1]["action"] == "click"


async def test_browse_fill_requires_data(session: FakeSession, page: FakePage, config: Config, guard: Any) -> None:
    """پر کردن فرم: data لازم است، تأیید می‌خواهد و selector نامعتبر رد می‌شود."""
    empty = await BrowserTool(config).run(browse(action="fill"), context=ToolContext(config=config))
    assert not empty.success and "requires a data object" in (empty.error or "")
    approved = confirm_ctx(config, guard=guard)
    ok = await BrowserTool(config).run(
        browse(action="fill", data={"input[name=q]": "agents", "input[name=z]": None}), context=approved
    )
    assert ok.success, ok.error
    assert page.fills == {"input[name=q]": "agents", "input[name=z]": ""}
    declined = await BrowserTool(config).run(
        browse(action="fill", data={"input[name=q]": "x"}), context=confirm_ctx(config, approved=False, guard=guard)
    )
    assert not declined.success and declined.error_code == "declined"
    unsafe = await BrowserTool(config).run(browse(action="fill", data={"`evil`": "x"}), context=approved)
    assert not unsafe.success and "unsafe selector" in (unsafe.error or "")


async def test_browse_wait_and_screenshot_actions(session: FakeSession, page: FakePage, config: Config) -> None:
    """wait (سقف ۱۰ ثانیه) و screenshot (بدون بایت خام در پاسخ)."""
    waited = await BrowserTool(config).run(browse(action="wait", wait_ms=999_999), context=ToolContext(config=config))
    assert waited.success and page.waits == [10_000]
    shot = await BrowserTool(config).run(browse(action="screenshot"), context=ToolContext(config=config))
    assert shot.success and shot.data["screenshot_bytes"] > 0
    assert "browser_screenshot" in shot.data["note"] and page.screenshot_paths == ["<bytes>"]
    by_selector = await BrowserTool(config).run(
        browse(action="wait", selector="#late"), context=ToolContext(config=config)
    )
    assert by_selector.success and page.clicks == ["wait:#late"]


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_text"),
    [
        (RuntimeError("page crashed: net::ERR_ABORTED"), "browser_error", "net::ERR_ABORTED"),
        (
            RuntimeError("Executable doesn't exist at /ms-playwright/chromium"),
            "browser_not_installed",
            "pip install playwright",
        ),
    ],
    ids=["generic", "missing-browser"],
)
async def test_browse_maps_page_errors(
    session: FakeSession, page: FakePage, config: Config, error: BaseException, expected_code: str, expected_text: str
) -> None:
    """خطای مرورگر به error_code خوانا تبدیل می‌شود."""
    page.error = error
    result = await BrowserTool(config).run(browse(), context=ToolContext(config=config))
    assert not result.success and result.error_code == expected_code
    assert expected_text in (result.error or "")


async def test_browse_reports_playwright_start_failure(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """اگر Playwright «در دسترس» باشد ولی بوت مرورگر خطا بدهد، خطا بلوکه نمی‌شود."""
    monkeypatch.setattr(br, "playwright_available", lambda: True)
    result = await BrowserTool(config).run(browse(), context=ToolContext(config=config))
    assert not result.success and "playwright" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# اعتبارسنجی و ایمنی
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["", "not a url", "ftp://example.com/pub", "javascript:alert(1)", "example.com/page"])
@pytest.mark.parametrize("tool_class", ALL_TOOLS, ids=lambda value: value.name)
async def test_invalid_urls_rejected_before_any_request(config: Config, tool_class: Any, url: str) -> None:
    """URL نامعتبر: هیچ درخواستی زده نمی‌شود و پیام خطای یکسان است."""
    payload = {**MINIMAL_INPUT[tool_class.name], "url": url}
    result = await tool_class(config).run(payload, context=ToolContext(config=config))
    assert not result.success, f"{tool_class.name} / {url!r}"
    assert result.error_code == "invalid_input", f"{tool_class.name} / {url!r}"


async def test_loopback_url_is_blocked_by_guard(config: Config, guard: Any) -> None:
    """محافظت SSRF: آدرس داخلی پیش از اجرا مسدود می‌شود."""
    result = await BrowserExtractTextTool(config).run(
        {"url": "http://127.0.0.1:8123/internal"}, context=confirm_ctx(config, guard=guard)
    )
    assert not result.success and result.error_code == "blocked"
    assert "ssrf" in (result.error or "").lower() or "loopback" in (result.error or "").lower()
    decision = await BrowserClickTool(config).safety_check(
        {"url": "http://169.254.169.254/latest/meta-data"}, confirm_ctx(config, guard=guard)
    )
    assert decision.allowed is False and decision.risk.value == "high"


async def test_unsafe_selector_rejected_in_validation(config: Config) -> None:
    """selector با کاراکترهای خطرناک در validate_input رد می‌شود."""
    result = await BrowserClickTool(config).run(
        {"url": "https://site.test", "selector": "button`onclick=x`"}, context=ToolContext(config=config)
    )
    assert not result.success and "not allowed" in (result.error or "")


async def test_fill_form_requires_confirmation(config: Config, guard: Any, context: ToolContext) -> None:
    """فیلد فرم همیشه تأیید می‌خواهد و با «رد» اجرا نمی‌شود."""
    tool = BrowserFillFormTool(config)
    assert tool.requires_confirmation
    approved_ctx = ToolContext(config=config, safety=guard, confirm=lambda request: True)
    approved = await tool.run({"url": "https://site.test/form", "data": {"#name": "x"}}, context=approved_ctx)
    # تأیید گرفته شد؛ اجرا تا لایه‌ی مرورگر پیش می‌رود و آن‌جا playwright_missing می‌شود
    assert not approved.success and approved.error_code == "playwright_missing"
    declined = await tool.run(
        {"url": "https://site.test/form", "data": {"#name": "x"}},
        context=confirm_ctx(config, approved=False, guard=guard),
    )
    assert not declined.success and declined.error_code == "declined"
    assert context.approvals == []  # fixture تأیید را ضبط می‌کند؛ اینجا فراخوانی نشده


async def test_fill_form_approved_runs_with_fake_page(
    session: FakeSession, page: FakePage, config: Config, guard: Any, context: ToolContext
) -> None:
    """پس از تأیید، فیلدها پر و submit می‌شوند."""
    context.approvals.clear()
    result = await BrowserFillFormTool(config).run(
        {
            "url": "https://site.test/form",
            "data": {"input[name=q]": "docs"},
            "submit_selector": "button[type=submit]",
            "screenshot": True,
        },
        context=context,
    )
    assert result.success, result.error
    assert result.data["filled_fields"] == ["input[name=q]"] and result.data["submitted"] is True
    assert page.fills == {"input[name=q]": "docs"} and page.clicks == ["button[type=submit]"]
    assert result.data["screenshot"].endswith("form-result.png")
    assert result.metadata["fields"] == 1
    assert context.approvals == ["browser_fill_form"]
    assert PLAYWRIGHT_HINT


async def test_empty_form_data_is_invalid(session: FakeSession, config: Config, guard: Any) -> None:
    """data تهی → خطای ورودی (نه خطای مرورگر)."""
    result = await BrowserFillFormTool(config).run(
        {"url": "https://site.test/form", "data": {}}, context=confirm_ctx(config, guard=guard)
    )
    assert not result.success and "at least one" in (result.error or "")


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------
async def test_screenshot_saves_inside_allowed_root(
    session: FakeSession, page: FakePage, config: Config, guard: Any
) -> None:
    """ذخیره در پروژه: مسیر، اندازه و viewport اعمال‌شده."""
    result = await BrowserScreenshotTool(config).run(
        {"url": "https://site.test/x", "save_path": "shots/a.png", "width": 800, "height": 600},
        context=confirm_ctx(config, guard=guard),
    )
    assert result.success, result.error
    target = Path(result.data["path"])
    assert target.is_absolute() and target.exists() and target.read_bytes().startswith(b"\x89PNG")
    assert target.parent == workspace_shot_dir(config)
    assert page.viewports == [{"width": 800, "height": 600}]
    assert result.data["size"] == "24 B" and result.data["full_page"] is False
    assert result.metadata["saved"] is True


def workspace_shot_dir(config: Config) -> Path:
    """پوشه‌ی مورد انتظار برای اسکرین‌شات‌های نسبی."""
    return config.project_root / "shots"


async def test_screenshot_outside_allowed_root_is_blocked(session: FakeSession, config: Config, guard: Any) -> None:
    """خروج از sandbox هنگام نوشتن فایل → blocked."""
    result = await BrowserScreenshotTool(config).run(
        {"url": "https://site.test/x", "save_path": "/tmp/evil.png"}, context=confirm_ctx(config, guard=guard)
    )
    assert not result.success and result.error_code == "blocked"
    assert "cannot save screenshot" in (result.error or "")


async def test_screenshot_rejects_bad_viewport(config: Config) -> None:
    """اندازه‌ی viewport خارج از بازه."""
    result = await BrowserScreenshotTool(config).run(
        {"url": "https://site.test/x", "width": 10}, context=ToolContext(config=config)
    )
    assert not result.success and "width" in (result.error or "") and "between 320 and 4096" in (result.error or "")


# ---------------------------------------------------------------------------
# extract_text با HTTP fallback
# ---------------------------------------------------------------------------
PatchFetch = Callable[..., None]


@pytest.fixture
def patch_fetch(monkeypatch: pytest.MonkeyPatch) -> PatchFetch:
    """جاگذاری fetch_url با بدنه‌ی دلخواه."""

    def _apply(body: str, *, info: dict[str, Any] | None = None, error: BaseException | None = None) -> None:
        async def fake_fetch(url: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            if error:
                raise error
            meta = {"status": 200, "content_type": "text/html; charset=utf-8", "bytes": len(body), "final_url": url}
            return body, {**meta, **(info or {})}

        monkeypatch.setattr(br, "fetch_url", fake_fetch)

    return _apply


async def test_extract_text_strips_scripts_and_title(config: Config, patch_fetch: PatchFetch) -> None:
    """اسکریپت/استایل حذف و عنوان استخراج می‌شود."""
    patch_fetch(
        "<html><head><title>Docs &amp; more</title><style>p{color:red}</style></head>"
        "<body><script>alert(1)</script><h1>Asyncio</h1><p>Run tasks <b>concurrently</b>.</p></body></html>"
    )
    result = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/docs"}, context=ToolContext(config=config)
    )
    text = result.data["text"]
    assert result.success and "Run tasks concurrently" in text and "Asyncio" in text
    assert text.count("Docs & more") == 0  # عنوان فقط در فیلد title می‌آید، نه در متن body
    assert "alert(1)" not in text and "color:red" not in text
    assert (
        result.data["title"] == "Docs & more" and result.data["status"] == 200 and result.data["truncated"] is False
    )


async def test_extract_text_keeps_links_on_request(config: Config, patch_fetch: PatchFetch) -> None:
    """keep_links=True لینک‌ها را به شکل markdown نگه می‌دارد."""
    patch_fetch('<p>see <a href="https://x.test/a">the docs</a></p>')
    plain = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/p"}, context=ToolContext(config=config)
    )
    assert "the docs" in plain.data["text"] and "https://x.test/a" not in plain.data["text"]
    with_links = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/p", "keep_links": True}, context=ToolContext(config=config)
    )
    assert "https://x.test/a" in with_links.data["text"] and "the docs" in with_links.data["text"]


async def test_extract_text_plain_body_for_non_html(config: Config, patch_fetch: PatchFetch) -> None:
    """محتوای غیر HTML بدون تبدیل، خام برگردانده می‌شود."""
    patch_fetch('{"ok": true}', info={"content_type": "application/json"})
    result = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/api"}, context=ToolContext(config=config)
    )
    assert result.success and result.data["content_type"] == "application/json"
    assert result.data["text"] == '{"ok": true}' and "title" not in result.data


async def test_extract_text_reports_fetch_failure(config: Config, patch_fetch: PatchFetch) -> None:
    """خطای شبکه با راهنمای عیب‌یابی (مثلاً 404)."""
    patch_fetch("", error=RuntimeError("HTTP 404 while fetching https://site.test/missing"))
    result = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/missing"}, context=ToolContext(config=config)
    )
    assert not result.success and result.error_code == "fetch_failed"
    assert "HTTP 404" in (result.error or "") and "CA certificates" in (result.error or "")


async def test_extract_text_bounds(config: Config, patch_fetch: PatchFetch) -> None:
    """سقف‌های max_chars."""
    patch_fetch("<p>" + "x" * 5000 + "</p>")
    too_small = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/p", "max_chars": 10}, context=ToolContext(config=config)
    )
    assert not too_small.success and "between 200 and 200000" in (too_small.error or "")
    cut = await BrowserExtractTextTool(config).run(
        {"url": "https://site.test/p", "max_chars": 200}, context=ToolContext(config=config)
    )
    assert cut.success and cut.data["truncated"] is True and len(cut.data["text"]) <= 200


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------
async def test_session_is_shared_per_context_and_expires(config: Config) -> None:
    """session در context مشترک است و با TTL تمام‌شده بازسازی می‌شود."""
    ctx = ToolContext(config=config)
    first = await BrowserSession.acquire(ctx, config)
    second = await BrowserSession.acquire(ctx, config)
    assert first is second and ctx.session[SESSION_KEY] is first
    assert first.headless is True and first.timeout_ms == int(config.browser_timeout_ms)
    expired = await BrowserSession.acquire(ctx, config.model_copy(update={"browser_session_ttl": 10}))
    assert expired is first  # ttl حداقل ۱۰ ثانیه است و session تازه هنوز منقضی نشده
    fresh = ToolContext(config=config)
    assert await BrowserSession.acquire(fresh, config) is not first


async def test_session_without_context_is_standalone() -> None:
    """context=None هم session می‌سازد (برای استفاده‌ی مستقیم کتابخانه‌ای)."""
    session = await BrowserSession.acquire(None, Config(openai_api_key="x", log_file=None))
    assert isinstance(session, BrowserSession) and not session.started
    assert session.age_seconds >= 0.0
    await session.close()


async def test_session_close_is_idempotent_without_playwright() -> None:
    """close() بدون playwright و دوباره صدا زده‌شدن، خطا نمی‌دهد."""
    session = BrowserSession(headless=False, timeout_ms=1234, user_agent="UA/test", locale="fa-IR")
    assert (session.headless, session.timeout_ms, session.user_agent, session.locale) == (
        False,
        1234,
        "UA/test",
        "fa-IR",
    )
    await session.close()
    await session.close()
    assert session.started is False and session._page is None


async def test_release_all_closes_and_clears(config: Config) -> None:
    """release_all همه‌ی session ها را می‌بندد و store را خالی می‌کند."""
    ctx = ToolContext(config=config)
    session = BrowserSession()
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    session.close = fake_close  # type: ignore[method-assign]
    ctx.session[SESSION_KEY] = session
    ctx.session["other"] = 1
    await BrowserSession.release_all(ctx)
    assert closed == [True] and SESSION_KEY not in ctx.session and ctx.session["other"] == 1
    await BrowserSession.release_all(None)  # بی‌خطر


# ---------------------------------------------------------------------------
# fetch_url (سرور محلی)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_fetch_url_against_local_server() -> None:
    """دریافت واقعی از سرور محلی: 200، 404 و سقف حجم."""
    from aiohttp import web

    async def html_page(request: web.Request) -> web.Response:
        return web.Response(text="<title>Local</title><body>hello</body>", content_type="text/html")

    async def missing(request: web.Request) -> web.Response:
        return web.Response(status=404, text="nope")

    async def big(request: web.Request) -> web.Response:
        return web.Response(text="y" * 5000)

    app = web.Application()
    app.router.add_get("/", html_page)
    app.router.add_get("/missing", missing)
    app.router.add_get("/big", big)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = int(runner.addresses[0][1])
    try:
        text, info = await fetch_url(f"http://127.0.0.1:{port}/", timeout=10.0)
        assert info["status"] == 200 and "hello" in text and info["bytes"] > 0
        with pytest.raises(RuntimeError, match="HTTP 404"):
            await fetch_url(f"http://127.0.0.1:{port}/missing", timeout=10.0)
        partial, capped = await fetch_url(f"http://127.0.0.1:{port}/big", timeout=10.0, max_bytes=100)
        assert capped["bytes"] == 100 and len(partial) == 100
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# اسکیمای ابزارها
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool_class", ALL_TOOLS, ids=lambda value: value.name)
def test_schemas_are_valid_function_definitions(config: Config, tool_class: Any) -> None:
    """همه‌ی ابزارها اسکیمای سازگار با OpenAI تولید می‌کنند."""
    schema = tool_class(config).get_schema()
    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == tool_class.name and function["description"]
    parameters = function["parameters"]
    assert parameters["type"] == "object" and parameters["additionalProperties"] is False
    assert "url" in parameters["required"]
    for name, spec in parameters["properties"].items():
        assert spec.get("description"), f"{tool_class.name}.{name} بدون توضیح"


def test_schema_details(config: Config) -> None:
    """جزئیات مهم اسکیمای هر ابزار."""
    browse_props = BrowserTool(config).get_schema()["function"]["parameters"]["properties"]
    assert set(browse_props["action"]["enum"]) == {"navigate", "extract_text", "click", "fill", "wait", "screenshot"}
    assert browse_props["data"]["type"] == "object"
    click = BrowserClickTool(config).get_schema()["function"]["parameters"]
    assert click["required"] == ["url", "selector"]
    fill = BrowserFillFormTool(config).get_schema()["function"]["parameters"]
    assert fill["required"] == ["url", "data"]
    extract = BrowserExtractTextTool(config).get_schema()["function"]["parameters"]["properties"]
    assert extract["use_browser"]["default"] is False and extract["max_chars"]["minimum"] == 200


def test_categories_and_risk_levels(config: Config) -> None:
    """دسته‌ی همه‌ی ابزارها browser و ریسک fill_form بالا است."""
    for tool_class in ALL_TOOLS:
        assert tool_class(config).category.value == "browser"
    assert BrowserFillFormTool(config).risk_level.value == "high"
    assert BrowserExtractTextTool(config).risk_level.value == "low"
    assert BrowserTool(config).risk_level.value == "medium"
    assert PLAYWRIGHT_HINT and SESSION_KEY == "browser.session"


def test_registered_in_tool_registry() -> None:
    """نام‌های ثبت‌شده در رجیستری."""
    from src.core.tool_registry import ToolRegistry, discover_tools

    discover_tools(force=True)
    for name in (
        "browser_browse",
        "browser_screenshot",
        "browser_extract_text",
        "browser_click",
        "browser_fill_form",
    ):
        assert ToolRegistry.get(name) is not None, name


async def test_direct_execute_without_context(session: FakeSession, config: Config) -> None:
    """استفاده‌ی کتابخانه‌ای: execute مستقیم (بدون pipeline تأیید) هم کار می‌کند."""
    result = await BrowserTool(config).execute("https://site.test/p", action="extract_text", max_chars=5000)
    assert result.success and result.data["title"] == "Fake Title" and result.tool == "browser_browse"

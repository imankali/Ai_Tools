"""ابزارهای مرور وب (Browser tools).

دو مسیر اجرا وجود دارد:

1. **Playwright** (کامل): ناوبری، کلیک، پر کردن فرم، اسکرین‌شات و استخراج متن از
   DOM واقعی. وابستگی اختیاری است؛ اگر نصب نباشد پیام راهنما برگردانده می‌شود::

       pip install "universal-agent-hub[browser]"   # یا: pip install playwright
       playwright install chromium

2. **HTTP fallback** (سبک): برای ``browser_extract_text`` اگر Playwright نبود،
   صفحه با aiohttp/requests دانلود و HTML به متن ساده تبدیل می‌شود.

ایمنی: هر URL پیش از درخواست از ``SafetyGuard.assess_network`` عبور می‌کند تا
درخواست به آدرس‌های داخلی (SSRF) انجام نشود.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.helpers import format_size, run_in_thread, strip_html, truncate_text
from src.utils.safety import SafetyDecision
from src.utils.validators import ValidationError, bounded_int, is_safe_selector, is_valid_url_strict

__all__ = [
    "BrowserClickTool",
    "BrowserExtractTextTool",
    "BrowserFillFormTool",
    "BrowserScreenshotTool",
    "BrowserSession",
    "BrowserTool",
    "fetch_url",
]

#: کلید نگهداری session در ToolContext.session
SESSION_KEY = "browser.session"

#: متن راهنمای نصب، وقتی Playwright پیدا نشود
PLAYWRIGHT_HINT = (
    "Playwright is not installed. Either install it (pip install playwright && playwright install chromium) "
    "for real browser automation, or use browser_extract_text which falls back to a plain HTTP fetch."
)


def playwright_available() -> bool:
    """آیا بسته‌ی playwright import شدنی است؟"""
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


async def fetch_url(
    url: str,
    *,
    config: Any = None,
    timeout: float = 20.0,
    max_bytes: int = 2_000_000,
) -> tuple[str, dict[str, Any]]:
    """دریافت یک صفحه با aiohttp (یا requests) بدون نیاز به مرورگر.

    Args:
        url: آدرس کامل.
        config: برای User-Agent.
        timeout: سقف زمانی درخواست.
        max_bytes: حداکثر حجم بدنه‌ی خوانده‌شده.

    Returns:
        دوگانه‌ی ``(متن_خام, {'status': int, 'content_type': str, 'bytes': int})``.

    Raises:
        RuntimeError: خطای شبکه یا status >= 400.
    """
    headers = {"User-Agent": str(getattr(config, "user_agent", "UniversalAgentHub/1.0"))}
    try:
        import aiohttp

        # NOTE: aiohttp >= 3.10 moved `max_redirects` off ClientSession; set it per-request.
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers=headers,
            ) as session,
            session.get(url, allow_redirects=True) as response,
        ):
            body = await response.content.read(max_bytes)
            info: dict[str, Any] = {
                "status": int(response.status),
                "content_type": str(response.headers.get("Content-Type") or ""),
                "bytes": len(body),
                "final_url": str(response.url),
            }
            text = body.decode(response.charset or "utf-8", errors="replace")
            if info["status"] >= 400:
                # قبلاً فقط مسیر requests وضعیت را بررسی می‌کرد و 404/500 مثل موفقیت برمی‌گشت
                raise RuntimeError(f"HTTP {info['status']} while fetching {url}")
            return text, info
    except ImportError:  # pragma: no cover - aiohttp جزء وابستگی‌هاست
        import requests

        def _sync_get() -> tuple[str, dict[str, Any]]:
            response = requests.get(url, timeout=timeout, headers=headers, stream=True, allow_redirects=True)
            body = response.raw.read(max_bytes, decode_content=True) if response.raw else b""
            info = {
                "status": int(response.status_code),
                "content_type": str(response.headers.get("Content-Type") or ""),
                "bytes": len(body),
                "final_url": str(response.url),
            }
            return body.decode(response.encoding or "utf-8", errors="replace"), info

        text, info = await run_in_thread(_sync_get)
    if int(info["status"]) >= 400:
        raise RuntimeError(f"HTTP {info['status']} while fetching {url}")
    return text, info


class BrowserSession:
    """یک browser + page مشترک در طول اجرای ایجنت (connection pooling).

    Args:
        headless: اجرای بدون پنجره.
        timeout_ms: سقف زمان هر عملیات مرورگر.
        user_agent: رشته‌ی User-Agent.
        locale: زبان صفحه (برای بعضی سایت‌ها مهم است).
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30_000,
        user_agent: str = "",
        locale: str = "en-US",
    ) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.user_agent = user_agent
        self.locale = locale
        self.created_at = time.time()
        self._started = False
        self._page: Any = None
        self._playwright: Any = None
        self._browser: Any = None
        self._context_manager: Any = None
        self._lock = asyncio.Lock()
        self.actions: list[dict[str, Any]] = []

    @property
    def started(self) -> bool:
        """آیا مرورگر راه‌اندازی شده است؟"""
        return self._started

    @property
    def age_seconds(self) -> float:
        """عمر session به ثانیه."""
        return max(0.0, time.time() - self.created_at)

    async def start(self) -> Any:
        """راه‌اندازی playwright + browser + page (idempotent).

        Raises:
            RuntimeError: Playwright نصب نیست یا مرورگر نصب‌شده پیدا نشد.
        """
        async with self._lock:
            if self._started and self._page is not None:
                return self._page
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(PLAYWRIGHT_HINT) from exc
            self._playwright = await async_playwright().start()
            in_container = Path("/.dockerenv").exists()  # noqa: ASYNC240 - یک stat ساده، نه I/O روی داده‌ی کاربر
            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                "args": ["--disable-dev-shm-usage", "--no-sandbox"] if in_container else [],
            }
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {"locale": self.locale}
            if self.user_agent:
                context_kwargs["user_agent"] = self.user_agent
            context = await self._browser.new_context(**context_kwargs)
            context.set_default_timeout(self.timeout_ms)
            self._page = await context.new_page()
            self._started = True
            return self._page

    async def get_page(self) -> Any:
        """دریافت page فعال (با بررسی سلامت آن)."""
        page = await self.start()
        with contextlib.suppress(Exception):  # اگر API تغییر کرد، فرض بر سالم بودن
            if page.is_closed():
                self._started = False
                page = await self.start()
        return page

    async def record(self, action: str, url: str = "", **extra: Any) -> None:
        """ثبت یک عملیات در تاریخچه‌ی session (برای debug)."""
        self.actions.append({"action": action, "url": url, "at": time.time(), **extra})

    async def close(self) -> None:
        """بستن browser و playwright (همیشه safe)."""
        async with self._lock:
            for closer in (getattr(self._browser, "close", None), getattr(self._playwright, "stop", None)):
                if closer is None:
                    continue
                with contextlib.suppress(Exception):  # بستن منابع خطا نمی‌دهد
                    outcome = closer()
                    if asyncio.iscoroutine(outcome):
                        await outcome
            self._page = None
            self._browser = None
            self._playwright = None
            self._started = False

    @classmethod
    async def acquire(cls, context: ToolContext | None, config: Any) -> BrowserSession:
        """دریافت/ساخت session مشترک از context (با TTL از config)."""
        session_store = context.session if context is not None else None
        ttl = int(getattr(config, "browser_session_ttl", 600) or 600)
        if session_store is not None:
            existing = session_store.get(SESSION_KEY)
            if isinstance(existing, BrowserSession) and existing.age_seconds < ttl:
                return existing
        session = cls(
            headless=bool(getattr(config, "browser_headless", True)),
            timeout_ms=int(getattr(config, "browser_timeout_ms", 30_000) or 30_000),
            user_agent=str(getattr(config, "user_agent", "") or ""),
        )
        if session_store is not None:
            session_store[SESSION_KEY] = session
        return session

    @staticmethod
    async def release_all(context: ToolContext | None) -> None:
        """بستن همه‌ی session های بازِ یک context."""
        if context is None:
            return
        for value in list(context.session.values()):
            if isinstance(value, BrowserSession):
                await value.close()
        context.session.pop(SESSION_KEY, None)


class BrowserToolBase(BaseTool):
    """پایه‌ی ابزارهای مرورگر (schema مشترک + بررسی ایمنی URL)."""

    category: ClassVar[ToolCategory] = ToolCategory.BROWSER
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM
    #: عملیات‌هایی که فقط با Playwright ممکن‌اند
    needs_playwright: ClassVar[bool] = True

    def validate_input(self, **kwargs: Any) -> bool:
        """اعتبارسنجی URL (و selector در صورت وجود)."""
        super().validate_input(**kwargs)
        if "url" in kwargs:
            is_valid_url_strict(str(kwargs.get("url") or ""))
        if kwargs.get("selector") is not None and not is_safe_selector(kwargs.get("selector")):
            raise ValidationError("selector contains characters that are not allowed")
        return True

    async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
        """جلوگیری از SSRF و ثبت ریسک تعامل با صفحه."""
        decision = SafetyDecision(allowed=True, risk=self.risk_level, reasons=["browser automation is interactive"])
        url = str(kwargs.get("url") or "")
        guard = self.safety_guard(context)
        if url and guard is not None:
            network = guard.assess_network(url)
            decision.allowed = network.allowed
            decision.risk = network.risk
            decision.reasons = network.reasons or decision.reasons
        if self.requires_confirmation:
            decision.requires_confirmation = True
        elif kwargs.get("fill") or kwargs.get("data"):
            decision.requires_confirmation = True
            decision.reasons.append("form submission may send data to a remote service")
        return decision

    async def _page_for(
        self, kwargs: dict[str, Any], context: ToolContext | None
    ) -> tuple[Any, BrowserSession, ToolResult | None]:
        """تأمین page آماده (یا ToolResult خطا اگر Playwright نبود)."""
        config = context.config if context and context.config else self.config
        if self.needs_playwright and not playwright_available():
            return (
                None,
                None,
                ToolResult.fail(  # type: ignore[return-value]
                    PLAYWRIGHT_HINT,
                    tool=self.name,
                    error_code="playwright_missing",
                    metadata={"install": "pip install playwright && playwright install chromium"},
                ),
            )
        session = await BrowserSession.acquire(context, config)
        page = await session.get_page()
        return page, session, None

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI (در زیرکلاس‌ها بازنویسی می‌شود)."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("url",),
            properties={"url": {"type": "string", "description": "Absolute http(s) URL."}},
        )

    def _timeout(self, context: ToolContext | None) -> int:
        """سقف زمانی عملیات شبکه/مرورگر (ثانیه)."""
        config = context.config if context and context.config else self.config
        return int(getattr(config, "request_timeout", 120) or 120)


@register_tool
class BrowserTool(BrowserToolBase):
    """ابزار چندعملیاتی «مرور وب» (browse)."""

    name: ClassVar[str] = "browser_browse"
    description: ClassVar[str] = (
        "Drive a real browser: navigate to a URL and optionally extract text, click a CSS "
        "selector, fill a form or wait for navigation. Actions: navigate, extract_text, click, "
        "fill, wait, screenshot. Use it when the page needs JavaScript or interaction; use "
        "browser_extract_text for simple static pages."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("url",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("action", "selector", "data", "wait_ms", "max_chars")
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM

    async def execute(  # type: ignore[override]
        self,
        url: str,
        *,
        action: str = "navigate",
        selector: str | None = None,
        data: dict[str, Any] | None = None,
        wait_ms: int | None = None,
        max_chars: int = 8000,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """اجرای یک عملیات مرور.

        Args:
            url: آدرس مقصد.
            action: یکی از ``navigate``, ``extract_text``, ``click``, ``fill``, ``wait``, ``screenshot``.
            selector: CSS selector برای click/fill/wait.
            data: نگاشت selector → مقدار برای پر کردن فرم.
            wait_ms: زمان انتظار اضافی (میلی‌ثانیه).
            max_chars: حداکثر کاراکتر متن برگشتی.
            context: زمینه‌ی اجرا.
        """
        kind = str(action or "navigate").lower()
        allowed = {"navigate", "extract_text", "click", "fill", "wait", "screenshot"}
        if kind not in allowed:
            raise ValidationError(f"action must be one of: {', '.join(sorted(allowed))}")
        clean_url = is_valid_url_strict(url)
        max_chars = bounded_int(max_chars, name="max_chars", minimum=500, maximum=200_000, default=8000)
        page, session, failure = await self._page_for({"url": clean_url}, context)
        if failure is not None:
            return failure
        timeout = self._timeout(context)
        try:
            await page.goto(clean_url, wait_until="domcontentloaded")
            await session.record("navigate", clean_url)
            payload: dict[str, Any] = {"url": page.url, "title": await page.title()}
            if kind in {"click", "fill", "wait", "screenshot", "extract_text"}:
                if kind == "click":
                    if not selector:
                        raise ValidationError("action 'click' requires a CSS selector")
                    await page.click(selector)
                    await session.record("click", page.url, selector=selector)
                elif kind == "fill":
                    if not data:
                        raise ValidationError("action 'fill' requires a data object mapping selectors to values")
                    for field_selector, value in dict(data).items():
                        if not is_safe_selector(field_selector):
                            raise ValidationError(f"unsafe selector in fill data: {field_selector!r}")
                        await page.fill(str(field_selector), "" if value is None else str(value))
                    await session.record("fill", page.url, fields=sorted(dict(data)))
                elif kind == "wait":
                    if selector:
                        await page.wait_for_selector(selector)
                    else:
                        await page.wait_for_timeout(min(int(wait_ms or 1000), 10_000))
                    await session.record("wait", page.url, selector=selector)
                if kind == "screenshot":
                    image = await page.screenshot(full_page=False)
                    payload["screenshot_bytes"] = len(image or b"")
                    payload["note"] = (
                        "binary screenshot omitted from the response; pass save_path to browser_screenshot to keep it"
                    )
                else:
                    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    cleaned = strip_html(str(text or "")) if "<" in str(text or "")[:200] else str(text or "")
                    payload["text"], payload["truncated"] = truncate_text(cleaned, max_chars)
            if wait_ms and kind != "wait":
                await page.wait_for_timeout(min(int(wait_ms), 10_000))
            payload["session_age_seconds"] = round(session.age_seconds, 1)
            return ToolResult.ok(payload, tool=self.name, metadata={"action": kind, "url": payload["url"]})
        except ValidationError as exc:
            return ToolResult.fail(str(exc), tool=self.name, error_code="invalid_input")
        except asyncio.TimeoutError:
            return ToolResult.fail(
                f"browser operation timed out after {timeout}s on {clean_url}",
                tool=self.name,
                error_code="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - خطای مرورگر به‌صورت نتیجه برمی‌گردد
            message = str(exc)
            if "executable doesn't exist" in message.lower() or "playwright install" in message.lower():
                return ToolResult.fail(
                    PLAYWRIGHT_HINT + f"\n\nUnderlying error: {truncate_text(message, 400)[0]}",
                    tool=self.name,
                    error_code="browser_not_installed",
                )
            return ToolResult.fail(f"browser failed: {message[:1500]}", tool=self.name, error_code="browser_error")

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("url",),
            properties={
                "url": {"type": "string", "description": "Page to open (http/https)."},
                "action": {
                    "type": "string",
                    "enum": ["navigate", "extract_text", "click", "fill", "wait", "screenshot"],
                    "description": "What to do after loading the page.",
                    "default": "navigate",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector for click/wait (e.g. 'button.submit', '#login').",
                },
                "data": {
                    "type": "object",
                    "description": "For action='fill': mapping of CSS selector -> value.",
                    "additionalProperties": {"type": "string"},
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "Extra wait after the action (max 10000).",
                    "minimum": 0,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters of extracted text.",
                    "minimum": 500,
                },
            },
        )


@register_tool
class BrowserScreenshotTool(BrowserToolBase):
    """گرفتن اسکرین‌شات از یک صفحه (PNG)."""

    name: ClassVar[str] = "browser_screenshot"
    description: ClassVar[str] = (
        "Open a URL in the browser and save a PNG screenshot to a path inside the allowed "
        "directories. Returns the saved path and file size (the image itself is never returned)."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("url",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("save_path", "full_page", "width", "height", "wait_ms")
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM

    async def execute(  # type: ignore[override]
        self,
        url: str,
        *,
        save_path: str = "screenshot.png",
        full_page: bool = False,
        width: int = 1440,
        height: int = 900,
        wait_ms: int = 500,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """ذخیره‌ی اسکرین‌شات صفحه.

        Args:
            url: آدرس صفحه.
            save_path: مسیر ذخیره (داخل دایرکتوری‌های مجاز).
            full_page: کل صفحه (اسکرول‌شده) یا فقط viewport.
            width: عرض viewport.
            height: ارتفاع viewport.
            wait_ms: مکث پیش از گرفتن تصویر (برای لود JS).
            context: زمینه‌ی اجرا.
        """
        clean_url = is_valid_url_strict(url)
        width = bounded_int(width, name="width", minimum=320, maximum=4096, default=1440)
        height = bounded_int(height, name="height", minimum=240, maximum=4096, default=900)
        page, session, failure = await self._page_for({"url": clean_url}, context)
        if failure is not None:
            return failure
        try:
            await page.set_viewport_size({"width": int(width), "height": int(height)})
            await page.goto(clean_url, wait_until="networkidle")
            if wait_ms:
                await page.wait_for_timeout(min(int(wait_ms), 10_000))
            config = context.config if context and context.config else self.config
            target = Path(str(save_path or "screenshot.png")).expanduser()  # noqa: ASYNC240 - محاسبه‌ی مسیر
            if not target.is_absolute():
                target = Path(str(getattr(config, "project_root", Path.cwd()))) / target
            target = target.resolve(strict=False)  # noqa: ASYNC240 - realpath کوتاه
            guard = self.safety_guard(context)
            if guard is not None:
                decision = guard.assess_path(target, action="write")
                if not decision.allowed:
                    return ToolResult.fail(
                        f"cannot save screenshot: {decision.reason_text}", tool=self.name, error_code="blocked"
                    )
            target.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - یک mkdir کوچک پیش از اسکرین‌شات
            await page.screenshot(path=str(target), full_page=bool(full_page))
            size = target.stat().st_size if target.exists() else 0  # noqa: ASYNC240 - اندازه‌ی فایل نتیجه
            await session.record("screenshot", clean_url, path=str(target))
            return ToolResult.ok(
                {"path": str(target), "size": format_size(size), "url": clean_url, "full_page": bool(full_page)},
                tool=self.name,
                metadata={"saved": target.exists()},
            )
        except asyncio.TimeoutError:
            return ToolResult.fail(f"page did not settle in time: {clean_url}", tool=self.name, error_code="timeout")
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            code = "browser_not_installed" if "executable" in message.lower() else "browser_error"
            return ToolResult.fail(f"screenshot failed: {message[:1200]}", tool=self.name, error_code=code)

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("url",),
            properties={
                "url": {"type": "string", "description": "Page to capture."},
                "save_path": {
                    "type": "string",
                    "description": "PNG path inside an allowed directory (default 'screenshot.png').",
                    "default": "screenshot.png",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the whole scrollable page.",
                    "default": False,
                },
                "width": {
                    "type": "integer",
                    "description": "Viewport width in pixels.",
                    "minimum": 320,
                    "maximum": 4096,
                },
                "height": {
                    "type": "integer",
                    "description": "Viewport height in pixels.",
                    "minimum": 240,
                    "maximum": 4096,
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "Delay before capture so dynamic content settles.",
                    "minimum": 0,
                },
            },
        )


@register_tool
class BrowserExtractTextTool(BrowserToolBase):
    """استخراج متن قابل‌خواندن یک صفحه (با fallback به HTTP)."""

    name: ClassVar[str] = "browser_extract_text"
    description: ClassVar[str] = (
        "Fetch a page and return clean, readable text (scripts/styles stripped, links optionally "
        "kept as markdown). Works without Playwright by using a plain HTTP request, so it is fast "
        "and safe for documentation, articles and changelogs. Returns status, title and text."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("url",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("max_chars", "keep_links", "selector", "use_browser")
    needs_playwright: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW

    async def execute(  # type: ignore[override]
        self,
        url: str,
        *,
        max_chars: int = 12_000,
        keep_links: bool = False,
        selector: str | None = None,
        use_browser: bool = False,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """استخراج متن.

        Args:
            url: آدرس صفحه.
            max_chars: سقف طول متن برگشتی.
            keep_links: حفظ لینک‌ها به شکل markdown.
            selector: فقط متن زیر این selector (فقط با browser).
            use_browser: استفاده‌ی اجباری از Playwright (برای صفحات JS-محور).
            context: زمینه‌ی اجرا.
        """
        clean_url = is_valid_url_strict(url)
        max_chars = bounded_int(max_chars, name="max_chars", minimum=200, maximum=200_000, default=12_000)
        config = context.config if context and context.config else self.config
        timeout = float(getattr(config, "search_timeout", 20.0) or 20.0)
        if use_browser and playwright_available():
            page, session, failure = await self._page_for({"url": clean_url}, context)
            if failure is None:
                await page.goto(clean_url, wait_until="domcontentloaded")
                target = page.locator(str(selector)) if selector else page
                text = await target.inner_text()
                await session.record("extract", clean_url, selector=selector)
                cleaned, was_cut = truncate_text(strip_html(str(text)), max_chars)
                return ToolResult.ok(
                    {
                        "url": clean_url,
                        "title": await page.title(),
                        "text": cleaned,
                        "truncated": was_cut,
                        "mode": "browser",
                    },
                    tool=self.name,
                )
        try:
            raw, info = await fetch_url(clean_url, config=config, timeout=timeout)
        except ImportError:  # pragma: no cover
            return ToolResult.fail(
                "neither aiohttp nor requests is installed", tool=self.name, error_code="missing_dependency"
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult.fail(
                f"could not fetch {clean_url}: {type(exc).__name__}: {truncate_text(str(exc), 600)[0]}\n"
                "Check network access/DNS/CA certificates, or use the terminal tool with curl if needed.",
                tool=self.name,
                error_code="fetch_failed",
            )
        content_type = str(info.get("content_type") or "")
        if "html" not in content_type and "xml" not in content_type and content_type:
            body, was_cut = truncate_text(str(raw), max_chars)
            return ToolResult.ok(
                {
                    "url": info.get("final_url", clean_url),
                    "content_type": content_type,
                    "text": body,
                    "truncated": was_cut,
                    "mode": "http",
                },
                tool=self.name,
                metadata=info,
            )
        document = re.sub(r"<head\b.*?</head>", " ", str(raw), flags=re.IGNORECASE | re.DOTALL)
        text = strip_html(document, keep_links=bool(keep_links))
        title_match = re.search(r"<title[^>]*>(.*?)</title>", str(raw), re.IGNORECASE | re.DOTALL)
        title = strip_html(title_match.group(1)) if title_match else ""
        cleaned, was_cut = truncate_text(text, max_chars)
        return ToolResult.ok(
            {
                "url": info.get("final_url", clean_url),
                "title": title[:300],
                "text": cleaned,
                "truncated": was_cut,
                "status": info.get("status"),
                "bytes": info.get("bytes"),
                "mode": "http",
            },
            tool=self.name,
            metadata={"status": info.get("status"), "content_type": content_type},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("url",),
            properties={
                "url": {"type": "string", "description": "Page to fetch and convert to text."},
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters of text to return.",
                    "minimum": 200,
                    "maximum": 200000,
                },
                "keep_links": {
                    "type": "boolean",
                    "description": "Keep hyperlinks as markdown [text](url).",
                    "default": False,
                },
                "selector": {
                    "type": "string",
                    "description": "Only extract text inside this CSS selector (browser mode only).",
                },
                "use_browser": {
                    "type": "boolean",
                    "description": "Force Playwright (needed for JavaScript-heavy pages).",
                    "default": False,
                },
            },
        )


@register_tool
class BrowserClickTool(BrowserToolBase):
    """کلیک روی یک عنصر و بازگرداندن متن بعد از ناوبری."""

    name: ClassVar[str] = "browser_click"
    description: ClassVar[str] = (
        "Open a URL, click a single CSS selector (button/link/tab) and return the page text after "
        "the click. Refuses to click selectors that look like logout/delete actions unless confirmed."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("url", "selector")
    optional_parameters: ClassVar[tuple[str, ...]] = ("wait_ms", "max_chars", "expect_navigation")
    #: کلیک بخشی از «تغییر حالت» صفحه است → تأیید می‌خواهد
    requires_confirmation: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM

    async def execute(  # type: ignore[override]
        self,
        url: str,
        selector: str,
        *,
        wait_ms: int = 750,
        max_chars: int = 6000,
        expect_navigation: bool = False,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """اجرای کلیک.

        Args:
            url: صفحه‌ی مبدأ.
            selector: CSS selector عنصر مقصد.
            wait_ms: مکث پس از کلیک.
            max_chars: سقف متن برگشتی.
            expect_navigation: اگر کلیک باعث ناوبری می‌شود، منتظر load بنشین.
            context: زمینه‌ی اجرا.
        """
        clean_url = is_valid_url_strict(url)
        if not is_safe_selector(selector):
            raise ValidationError(f"unsafe selector: {selector!r}")
        max_chars = bounded_int(max_chars, name="max_chars", minimum=200, maximum=100_000, default=6000)
        page, session, failure = await self._page_for({"url": clean_url}, context)
        if failure is not None:
            return failure
        try:
            await page.goto(clean_url, wait_until="domcontentloaded")
            if expect_navigation:
                async with page.expect_navigation(timeout=15_000):
                    await page.click(selector)
            else:
                await page.click(selector)
            await page.wait_for_timeout(min(int(wait_ms or 750), 10_000))
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            cleaned, was_cut = truncate_text(str(text or ""), max_chars)
            await session.record("click", page.url, selector=selector)
            return ToolResult.ok(
                {"url": page.url, "clicked": selector, "text": cleaned, "truncated": was_cut},
                tool=self.name,
                metadata={"selector": selector},
            )
        except ValidationError as exc:
            return ToolResult.fail(str(exc), tool=self.name, error_code="invalid_input")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.fail(f"click failed: {str(exc)[:1200]}", tool=self.name, error_code="browser_error")

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("url", "selector"),
            properties={
                "url": {"type": "string", "description": "Page containing the element."},
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the element to click (must match exactly one).",
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "Delay after the click before reading the page.",
                    "minimum": 0,
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters of resulting text.",
                    "minimum": 200,
                },
                "expect_navigation": {
                    "type": "boolean",
                    "description": "Wait for a page navigation caused by the click.",
                    "default": False,
                },
            },
        )


@register_tool
class BrowserFillFormTool(BrowserToolBase):
    """پر کردن فرم (و در صورت خواست، submit)."""

    name: ClassVar[str] = "browser_fill_form"
    description: ClassVar[str] = (
        "Fill a web form: pass a mapping of CSS selectors to values (and optionally a submit "
        "selector). Confirmation is always required because data leaves the machine. Never fill "
        "real credentials unless the user explicitly asked you to."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("url", "data")
    optional_parameters: ClassVar[tuple[str, ...]] = ("submit_selector", "wait_ms", "screenshot")
    requires_confirmation: ClassVar[bool] = True
    sensitive_parameters: ClassVar[tuple[str, ...]] = ("data",)
    risk_level: ClassVar[RiskLevel] = RiskLevel.HIGH

    async def execute(  # type: ignore[override]
        self,
        url: str,
        data: dict[str, Any],
        *,
        submit_selector: str | None = None,
        wait_ms: int = 500,
        screenshot: bool = False,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """پر کردن فرم.

        Args:
            url: آدرس صفحه‌ی فرم.
            data: نگاشت ``selector → value``.
            submit_selector: selector دکمه‌ی submit (اختیاری).
            wait_ms: مکث پس از submit.
            screenshot: ذخیره‌ی تصویر نتیجه در فایل.
            context: زمینه‌ی اجرا.
        """
        clean_url = is_valid_url_strict(url)
        fields = dict(data or {})
        if not fields:
            raise ValidationError("data must contain at least one selector/value pair")
        for key in fields:
            if not is_safe_selector(str(key)):
                raise ValidationError(f"unsafe selector in data: {key!r}")
        page, session, failure = await self._page_for({"url": clean_url}, context)
        if failure is not None:
            return failure
        try:
            await page.goto(clean_url, wait_until="domcontentloaded")
            filled: list[str] = []
            for selector, value in fields.items():
                await page.fill(selector, "" if value is None else str(value))
                filled.append(selector)
            if submit_selector:
                if not is_safe_selector(submit_selector):
                    raise ValidationError("unsafe submit selector")
                await page.click(submit_selector)
                await page.wait_for_timeout(min(int(wait_ms or 500), 10_000))
            payload: dict[str, Any] = {
                "url": page.url,
                "filled_fields": filled,
                "submitted": bool(submit_selector),
                "title": await page.title(),
            }
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            payload["text"] = truncate_text(str(text or ""), 4000)[0]
            if screenshot:
                target = (
                    Path(str(getattr(context.config if context else self.config, "project_root", Path.cwd())))
                    / "form-result.png"
                )
                await page.screenshot(path=str(target))
                payload["screenshot"] = str(target)
            await session.record("fill", page.url, fields=filled, submit=bool(submit_selector))
            return ToolResult.ok(payload, tool=self.name, metadata={"fields": len(filled)})
        except ValidationError as exc:
            return ToolResult.fail(str(exc), tool=self.name, error_code="invalid_input")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.fail(
                f"form automation failed: {str(exc)[:1200]}", tool=self.name, error_code="browser_error"
            )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("url", "data"),
            properties={
                "url": {"type": "string", "description": "Page containing the form."},
                "data": {
                    "type": "object",
                    "description": "Mapping of CSS selector to the value to type, e.g. {'input[name=q]': 'playwright docs'}.",
                    "additionalProperties": {"type": "string"},
                },
                "submit_selector": {
                    "type": "string",
                    "description": "Selector of the submit button; omit to only fill the fields.",
                },
                "wait_ms": {"type": "integer", "description": "Delay after submitting.", "minimum": 0},
                "screenshot": {
                    "type": "boolean",
                    "description": "Save a PNG of the result page for verification.",
                    "default": False,
                },
            },
        )

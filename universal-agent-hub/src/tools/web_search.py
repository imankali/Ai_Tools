"""ابزارهای جست‌وجوی وب (Web search) — استراتژی قابل تعویض.

سه «پشتیبان» (backend) پیاده‌سازی شده است و :class:`WebSearchTool` طبق
``SEARCH_BACKEND`` آن‌ها را به ترتیب fallback امتحان می‌کند:

``ddgs``
    بسته‌ی ``ddgs`` (DuckDuckGo Search). بهترین کیفیت، نیازمند اینترنت.
``html``
    درخواست مستقیم به ``html.duckduckgo.com`` با aiohttp/requests. بدون وابستگی اضافه.
``tavily``
    API Tavily اگر ``TAVILY_API_KEY`` تنظیم شده باشد (جواب‌های آماده‌یLLM).
``offline``
    هیچ شبکه‌ای زده نمی‌شود؛ برای تست و محیط‌های ایزوله.

الگوی طراحی: **Strategy** (هر provider یک کلاس مستقل) + **Registry** برای ثبت ابزار.
"""

from __future__ import annotations

import abc
import asyncio
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from html import unescape
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.helpers import run_in_thread, strip_html
from src.utils.validators import ValidationError, bounded_int, clean_text

__all__ = [
    "AdvancedSearchTool",
    "DdgSearchProvider",
    "HtmlSearchProvider",
    "OfflineSearchProvider",
    "SearchProvider",
    "SearchResult",
    "TavilySearchProvider",
    "WebSearchTool",
]

#: سقف نتایج برگشتی
MAX_RESULTS = 20
#: کش پاسخ‌ها (query → نتایج) تا هزینه‌ی شبکه تکراری کم شود
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL_SECONDS = 600.0
_CACHE_MAX_ENTRIES = 64


@dataclass(slots=True)
class SearchResult:
    """یک رکورد نرمال‌شده‌ی نتایج جست‌وجو.

    Attributes:
        title: عنوان نتیجه.
        url: آدرس.
        snippet: توضیح کوتاه.
        source: نام provider.
        extra: فیلدهای اضافه (تاریخ، امتیاز relevance و…).
    """

    title: str
    url: str
    snippet: str = ""
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """تبدیل به dict (برای JSON/ToolResult)."""
        payload = {"title": self.title, "url": self.url, "snippet": self.snippet}
        if self.source:
            payload["source"] = self.source
        if self.extra:
            payload.update(self.extra)
        return payload


class SearchProvider(abc.ABC):
    """پایه‌ی provider های جست‌وجو (Strategy)."""

    #: نام یکتای provider (مقدار ``SEARCH_BACKEND``)
    id: ClassVar[str] = "base"
    #: توضیح برای کاربر
    description: ClassVar[str] = ""

    @abc.abstractmethod
    def is_available(self, config: Any) -> bool:
        """آیا این provider در این محیط قابل استفاده است؟"""
        raise NotImplementedError

    @abc.abstractmethod
    async def search(
        self,
        query: str,
        *,
        num_results: int = 6,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: str | None = None,
        config: Any = None,
    ) -> list[SearchResult]:
        """اجرای جست‌وجو و بازگرداندن نتایج نرمال‌شده.

        Raises:
            RuntimeError: خطای شبکه/سرویس.
        """
        raise NotImplementedError

    @staticmethod
    def _as_domain_list(value: Any) -> list[str]:
        """تبدیل ورودی انعطاف‌پذیر دامنه به فهرست رشته.

        مدل‌های زبانی گاه رشته‌ی ساده (یا جداشده با کاما) به‌جای آرایه می‌فرستند؛
        اگر همان رشته را روی کاراکترها iterate کنیم فیلتر بی‌صدا خراب می‌شود،
        پس همه‌چیز اینجا به فهرست نرمال می‌شود.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set, frozenset)):
            return [str(part).strip() for part in value if str(part).strip()]
        return [str(value).strip()]

    @staticmethod
    def apply_filters(
        results: list[SearchResult],
        *,
        include_domains: Iterable[str] | None = None,
        exclude_domains: Iterable[str] | None = None,
        min_snippet_chars: int = 0,
    ) -> list[SearchResult]:
        """اعمال فیلتر دامنه و حداقل طول snippet.

        Args:
            results: نتایج خام.
            include_domains: فقط این دامنه‌ها (پسونداً match می‌شوند).
            exclude_domains: این دامنه‌ها حذف شوند.
            min_snippet_chars: حداقل طول توضیح لازم برای حفظ نتیجه.

        Returns:
            نتایج فیلترشده.
        """
        include = {domain.lower().lstrip(".") for domain in SearchProvider._as_domain_list(include_domains) if domain}
        exclude = {domain.lower().lstrip(".") for domain in SearchProvider._as_domain_list(exclude_domains) if domain}
        filtered: list[SearchResult] = []
        for item in results:
            host = (urlparse(item.url).hostname or "").lower()
            if include and not any(host == domain or host.endswith("." + domain) for domain in include):
                continue
            if exclude and any(host == domain or host.endswith("." + domain) for domain in exclude):
                continue
            if min_snippet_chars and len(item.snippet) < min_snippet_chars:
                continue
            filtered.append(item)
        return filtered


class DdgSearchProvider(SearchProvider):
    """provider مبتنی بر بسته‌ی ``ddgs`` (DuckDuckGo Search)."""

    id = "ddgs"
    description = "DuckDuckGo via the `ddgs` package (text/news/images/videos)."

    def is_available(self, config: Any) -> bool:
        """آیا ddgs import می‌شود؟"""
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return False
        return True

    async def search(
        self,
        query: str,
        *,
        num_results: int = 6,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: str | None = None,
        config: Any = None,
        kind: str = "text",
    ) -> list[SearchResult]:
        """اجرای جست‌وجو با ddgs در thread جدا (کتابخانه sync است).

        Args:
            query: عبارت جست‌وجو.
            num_results: حداکثر تعداد نتایج.
            region: منطقه‌ی جغرافیایی (``wt-wt``, ``us-en``, ``fa-ir`` …).
            safesearch: ``on`` / ``moderate`` / ``off``.
            timelimit: ``d``, ``w``, ``m``, ``y`` برای فیلتر زمانی.
            config: پیکربندی (برای timeout).
            kind: نوع جست‌وجو: text/news/images/videos.

        Returns:
            فهرست نتایج نرمال‌شده.
        """
        from ddgs import DDGS

        timeout = float(getattr(config, "search_timeout", 20.0) or 20.0)
        kwargs: dict[str, Any] = {
            "region": region,
            "safesearch": safesearch,
            "max_results": min(num_results, MAX_RESULTS),
        }
        if timelimit:
            kwargs["timelimit"] = timelimit

        def _run() -> list[dict[str, Any]]:
            # DDGS فقط timeout عدد صحیح (ثانیه) می‌پذیرد
            with DDGS(timeout=max(1, round(timeout))) as client:
                method = getattr(client, kind if kind != "text" else "text")
                return list(method(query, **kwargs) or [])

        raw = await asyncio.wait_for(run_in_thread(_run), timeout=timeout + 10)
        results: list[SearchResult] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("href") or item.get("url") or item.get("image") or "")
            if not url:
                continue
            results.append(
                SearchResult(
                    title=clean_text(str(item.get("title") or ""), max_length=240),
                    url=url,
                    snippet=clean_text(str(item.get("body") or item.get("snippet") or ""), max_length=600),
                    source=self.id,
                    extra={
                        key: value
                        for key, value in item.items()
                        if key in {"date", "author", "source", "thumbnail", "height", "width"} and value
                    },
                )
            )
        return results


class HtmlSearchProvider(SearchProvider):
    """provider سبک مبتنی بر نسخه‌ی HTML داکیومنت‌های DuckDuckGo."""

    id = "html"
    description = "Fetch https://html.duckduckgo.com/html and parse result links (no extra deps)."

    _RESULT_RE = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r"(?P<rest>.*?)(?=<a|$)",
        re.DOTALL | re.IGNORECASE,
    )
    _SNIPPET_RE = re.compile(
        r'class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )

    def is_available(self, config: Any) -> bool:
        """همیشه در دسترس است (اگر شبکه باشد)."""
        return True

    async def _fetch(self, url: str, *, config: Any, timeout: float) -> str:
        """دریافت متن خام یک URL (aiohttp اگر هست، وگرنه requests)."""
        headers = {"User-Agent": str(getattr(config, "user_agent", "UniversalAgentHub/1.0"))}
        try:
            import aiohttp

            timeout_ = aiohttp.ClientTimeout(total=timeout)
            async with (
                aiohttp.ClientSession(timeout=timeout_, headers=headers) as session,
                session.get(url) as response,
            ):
                if response.status >= 400:
                    raise RuntimeError(f"search endpoint returned HTTP {response.status}")
                return await response.text(errors="replace")
        except ImportError:  # pragma: no cover - aiohttp جزء وابستگی‌هاست
            import requests

            sync_response = await run_in_thread(requests.get, url, timeout=timeout, headers=headers)
            if sync_response.status_code >= 400:
                raise RuntimeError(f"search endpoint returned HTTP {sync_response.status_code}") from None
            return str(sync_response.text)

    async def search(
        self,
        query: str,
        *,
        num_results: int = 6,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: str | None = None,
        config: Any = None,
    ) -> list[SearchResult]:
        """جست‌وجو از طریق endpoint HTML."""
        from urllib.parse import urlencode

        params: dict[str, str] = {"q": query}
        if safesearch != "off":
            params["kl"] = region
        timeout = float(getattr(config, "search_timeout", 20.0) or 20.0)
        body = await self._fetch(
            f"https://html.duckduckgo.com/html/?{urlencode(params)}", config=config, timeout=timeout
        )
        results: list[SearchResult] = []
        matches = list(self._RESULT_RE.finditer(body))
        for index, match in enumerate(matches):
            # توضیح معمولاً در تگ <a> بعدی است، پس پنجره‌ی جست‌وجو تا <a> بعدی نیست
            window_end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            window = body[match.end() : window_end] or (match.group("rest") or "")
            snippet_match = self._SNIPPET_RE.search(window) or self._SNIPPET_RE.search(match.group("rest") or "")
            snippet = strip_html(snippet_match.group("snippet")) if snippet_match else ""
            href = unescape(match.group("href"))
            if href.startswith("//duckduckgo.com/l/") or "uddg=" in href:
                # لینک‌های redirect داک‌داک‌گو: مقصد واقعی در پارامتر uddg است
                extracted = re.search(r"uddg=([^&]+)", href)
                href = unquote(extracted.group(1)) if extracted else ""
            if not href.startswith("http"):
                continue
            title = strip_html(match.group("title")) or href
            results.append(
                SearchResult(
                    title=clean_text(title, max_length=240),
                    url=href,
                    snippet=clean_text(snippet, max_length=600),
                    source=self.id,
                )
            )
            if len(results) >= num_results:
                break
        return results


class TavilySearchProvider(SearchProvider):
    """provider Tavily (نتایج آماده برای مدل‌های زبانی) — نیازمند API key."""

    id = "tavily"
    description = "Tavily API (needs TAVILY_API_KEY); returns answer + sources."

    def is_available(self, config: Any) -> bool:
        """آیا کلید Tavily تنظیم شده است؟"""
        return bool(getattr(config, "tavily_api_key", ""))

    async def search(
        self,
        query: str,
        *,
        num_results: int = 6,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: str | None = None,
        config: Any = None,
    ) -> list[SearchResult]:
        """فراخوانی ``POST https://api.tavily.com/search``."""
        import json

        timeout = float(getattr(config, "search_timeout", 20.0) or 20.0)
        payload = {
            "api_key": str(getattr(config, "tavily_api_key", "") or ""),
            "query": query,
            "max_results": min(num_results, MAX_RESULTS),
            "search_depth": "basic",
            "include_answer": True,
        }
        if timelimit:
            payload["time_range"] = {"d": "day", "w": "week", "m": "month", "y": "year"}.get(timelimit, "year")
        try:
            import aiohttp

            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session,
                session.post(
                    "https://api.tavily.com/search",
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                ) as response,
            ):
                if response.status >= 400:
                    raise RuntimeError(f"Tavily returned HTTP {response.status}")
                data = await response.json(content_type=None)
        except ImportError:  # pragma: no cover
            import requests

            sync_response = await run_in_thread(
                requests.post, "https://api.tavily.com/search", json=payload, timeout=timeout
            )
            if sync_response.status_code >= 400:
                raise RuntimeError(f"Tavily returned HTTP {sync_response.status_code}") from None
            data = sync_response.json()
        results: list[SearchResult] = []
        for item in (data or {}).get("results", []) or []:
            results.append(
                SearchResult(
                    title=clean_text(str(item.get("title") or ""), max_length=240),
                    url=str(item.get("url") or ""),
                    snippet=clean_text(str(item.get("content") or ""), max_length=900),
                    source=self.id,
                    extra={"score": item.get("score")} if item.get("score") is not None else {},
                )
            )
        return results


class OfflineSearchProvider(SearchProvider):
    """provider بی‌شبکه — برای تست واحد و محیط‌های sandbox."""

    id = "offline"
    description = "No network access; returns a fixed stub result set (testing/CI)."

    #: نتایج ساختگی ولی پایدار برای تست
    STUB: ClassVar[list[dict[str, Any]]] = [
        {
            "title": "Offline mode: network access disabled",
            "url": "https://example.com/offline",
            "snippet": "SEARCH_BACKEND=offline is active, so no external request was made.",
        }
    ]

    def is_available(self, config: Any) -> bool:
        """همیشه در دسترس."""
        return True

    async def search(
        self,
        query: str,
        *,
        num_results: int = 6,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: str | None = None,
        config: Any = None,
    ) -> list[SearchResult]:
        """بازگرداندن نتایج ثابت (بدون شبکه)."""
        return [SearchResult(source=self.id, **item) for item in self.STUB[: max(1, num_results)]]


PROVIDERS: dict[str, SearchProvider] = {
    provider.id: provider
    for provider in (
        DdgSearchProvider(),
        HtmlSearchProvider(),
        TavilySearchProvider(),
        OfflineSearchProvider(),
    )
}


def _cache_get(key: str) -> list[dict[str, Any]] | None:
    """خواندن کش اگر منقضی نشده باشد."""
    entry = _CACHE.get(key)
    if not entry:
        return None
    timestamp, value = entry
    if time.time() - timestamp > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: list[dict[str, Any]]) -> None:
    """نوشتن در کش با حذف موارد قدیمی."""
    _CACHE[key] = (time.time(), value)
    if len(_CACHE) > _CACHE_MAX_ENTRIES:
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: len(_CACHE) - _CACHE_MAX_ENTRIES]
        for stale_key, _ in oldest:
            _CACHE.pop(stale_key, None)


class SearchToolBase(BaseTool):
    """منطق مشترک ابزارهای جست‌وجو (انتخاب provider و فیلترها)."""

    category: ClassVar[ToolCategory] = ToolCategory.NETWORK
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    requires_confirmation: ClassVar[bool] = False
    #: نوع جست‌وجو برای ddgs (text/news/images/videos)
    search_kind: ClassVar[str] = "text"

    async def _collect(
        self,
        query: str,
        *,
        num_results: int,
        region: str,
        safesearch: str,
        timelimit: str | None,
        include_domains: list[str],
        exclude_domains: list[str],
        use_cache: bool,
        context: ToolContext | None,
        search_kind: str | None = None,
        forced_backend: str | None = None,
    ) -> ToolResult:
        """اجرای جست‌وجو با fallback بین provider ها.

        همه‌ی حالت‌ها پارامتری‌اند (نه attribute روی نمونه) تا اجرای موازی چند
        فراخوانی، نتیجه‌ی یکدیگر را خراب نکند.

        Returns:
            ToolResult با کلیدهای ``query``, ``results``, ``backend``, ``count``.
        """
        config = context.config if context and context.config else self.config
        kind = search_kind or self.search_kind
        cache_key = f"{kind}:{query}:{num_results}:{region}:{timelimit}:{forced_backend or ''}"
        if use_cache:
            cached = _cache_get(cache_key)
            if cached is not None:
                return ToolResult.ok(
                    {"query": query, "results": cached, "count": len(cached), "backend": "cache", "cached": True},
                    tool=self.name,
                )

        ordered = self._provider_order(config, forced_backend=forced_backend)
        if not ordered:
            raise ValidationError(
                "no search backend available: install 'ddgs' (pip install ddgs) or set SEARCH_BACKEND=html"
            )
        errors: list[str] = []
        for provider_id in ordered:
            provider = PROVIDERS[provider_id]
            try:
                results = await provider.search(
                    query,
                    num_results=num_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                    config=config,
                    **({"kind": kind} if isinstance(provider, DdgSearchProvider) else {}),
                )
            except Exception as exc:  # noqa: BLE001 - fallback به provider بعدی
                errors.append(f"{provider_id}: {type(exc).__name__}: {exc}")
                continue
            results = SearchProvider.apply_filters(
                results, include_domains=include_domains, exclude_domains=exclude_domains
            )
            # حذف تکراری بر اساس URL
            seen: set[str] = set()
            unique: list[SearchResult] = []
            for item in results:
                if item.url in seen:
                    continue
                seen.add(item.url)
                unique.append(item)
            payload = [item.to_dict() for item in unique[:num_results]]
            if not payload and errors:
                continue
            if use_cache:
                _cache_put(cache_key, payload)
            await self._emit(
                context,
                "search.completed",
                {"tool": self.name, "query": query[:120], "backend": provider_id, "count": len(payload)},
            )
            metadata: dict[str, Any] = {"backend": provider_id, "count": len(payload)}
            if errors:
                metadata["fallbacks"] = errors
            return ToolResult.ok(
                {
                    "query": query,
                    "backend": provider_id,
                    "count": len(payload),
                    "results": payload,
                    "formatted": self._format(payload),
                },
                tool=self.name,
                metadata=metadata,
            )
        return ToolResult.fail(
            "all search backends failed: "
            + " | ".join(errors)[:1200]
            + "\n\nHints: check network access/CA certificates, or set SEARCH_BACKEND=offline for testing.",
            tool=self.name,
            error_code="search_unavailable",
            metadata={"attempts": errors},
        )

    @staticmethod
    def _provider_order(config: Any, *, forced_backend: str | None = None) -> list[str]:
        """ترتیب امتحان provider ها بر اساس ``SEARCH_BACKEND`` (یا backend اجباری).

        Args:
            config: پیکربندی (برای ``search_backend`` و دسترسی‌ها).
            forced_backend: اگر داده شود، فقط همین provider استفاده می‌شود.

        Raises:
            ValidationError: نام backend ناشناخته باشد.
        """
        if forced_backend:
            if forced_backend not in PROVIDERS:
                raise ValidationError(
                    f"unknown backend '{forced_backend}'. Available: {', '.join(sorted(PROVIDERS))}"
                )
            return [forced_backend]
        preference = str(getattr(config, "search_backend", "auto") or "auto").lower()
        if preference == "offline":
            return ["offline"]
        if preference in PROVIDERS and preference != "auto":
            return [preference]
        order = ["ddgs", "html", "tavily"] if PROVIDERS["ddgs"].is_available(config) else ["html", "tavily"]
        return [name for name in order if PROVIDERS[name].is_available(config)]

    @staticmethod
    def _format(payload: list[dict[str, Any]]) -> str:
        """قالب‌بندی خواندلیست نتایج (برای خواندن سریع توسط مدل)."""
        lines: list[str] = []
        for index, item in enumerate(payload, start=1):
            lines.append(f"{index}. {item.get('title', '')}")
            lines.append(f"   {item.get('url', '')}")
            snippet = str(item.get("snippet") or "").strip()
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")
        return "\n".join(lines).strip()

    def _common_properties(self) -> dict[str, Any]:
        """پارامترهای مشترک دو ابزار جست‌وجو."""
        return {
            "query": {"type": "string", "description": "What to search for (plain keywords work best)."},
            "num_results": {
                "type": "integer",
                "description": f"How many results to return (1-{MAX_RESULTS}).",
                "minimum": 1,
                "maximum": MAX_RESULTS,
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restrict results to these domains, e.g. ['docs.python.org'].",
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Drop results from these domains.",
            },
            "region": {
                "type": "string",
                "description": "Search region: 'wt-wt' (worldwide), 'us-en', 'fa-ir', 'de-de', …",
            },
            "safesearch": {
                "type": "string",
                "enum": ["on", "moderate", "off"],
                "description": "Filter explicit/violent results ('on' is strictest).",
            },
            "timelimit": {
                "type": "string",
                "enum": ["d", "w", "m", "y"],
                "description": "Restrict to results from the last day/week/month/year.",
            },
            "use_cache": {
                "type": "boolean",
                "description": "Reuse results cached during this process (10 min TTL).",
                "default": True,
            },
        }

    def _validate_common(self, kwargs: dict[str, Any]) -> None:
        """اعتبارسنجی مشترک پارامترها."""
        query = clean_text(kwargs.get("query"), max_length=500)
        if not query:
            raise ValidationError("query must not be empty")
        kwargs["query"] = query
        bounded_int(kwargs.get("num_results"), name="num_results", minimum=1, maximum=MAX_RESULTS, default=6)
        for key in ("include_domains", "exclude_domains"):
            value = kwargs.get(key)
            if value is None:
                kwargs[key] = []
            elif isinstance(value, str):
                kwargs[key] = [part.strip() for part in value.split(",") if part.strip()]
            elif not isinstance(value, list):
                raise ValidationError(f"{key} must be a list of domain strings")


@register_tool
class WebSearchTool(SearchToolBase):
    """جست‌وجوی سریع وب و بازگرداندن نتایج.

    معادل ``search(query)`` در شرح پروژه؛ پارامترهای پیشرفته اختیاری‌اند.
    """

    name: ClassVar[str] = "web_search"
    description: ClassVar[str] = (
        "Search the web and return ranked results (title, URL, snippet). Use this for facts you "
        "cannot know locally: library docs, error messages, prices, news, version numbers. "
        "Prefer 3-6 results and then read the best page with fetch_page."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("query",)
    optional_parameters: ClassVar[tuple[str, ...]] = (
        "num_results",
        "include_domains",
        "exclude_domains",
        "region",
        "safesearch",
        "timelimit",
        "use_cache",
        "backend",
        "context",
    )

    async def execute(  # type: ignore[override]
        self,
        query: str,
        *,
        num_results: int = 6,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: str | None = None,
        use_cache: bool = True,
        backend: str | None = None,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """اجرای جست‌وجو.

        Args:
            query: عبارت جست‌وجو.
            backend: بازنویسی ``SEARCH_BACKEND`` برای همین فراخوانی (مثلاً ``offline``).
            num_results: حداکثر تعداد نتایج.
            include_domains: فیلتر دامنه‌ی مثبت.
            exclude_domains: فیلتر دامنه‌ی منفی.
            region: منطقه‌ی جست‌وجو.
            safesearch: سطح فیلتر محتوای ناخواسته.
            timelimit: فیلتر زمانی (d/w/m/y).
            use_cache: استفاده از کش درون‌فرآیندی.
            context: زمینه‌ی اجرا.
        """
        return await self._collect(
            clean_text(query, max_length=500),
            num_results=int(num_results or 6),
            region=str(region or "wt-wt"),
            safesearch=str(safesearch or "moderate"),
            timelimit=timelimit,
            include_domains=SearchProvider._as_domain_list(include_domains),
            exclude_domains=SearchProvider._as_domain_list(exclude_domains),
            use_cache=bool(use_cache),
            context=context,
            forced_backend=(str(backend).lower() if backend else None),
        )

    def validate_input(self, **kwargs: Any) -> bool:
        """اعتبارسنجی پارامترهای جست‌وجو."""
        super().validate_input(**kwargs)
        self._validate_common(kwargs)
        return True

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        properties = self._common_properties()
        properties["backend"] = {
            "type": "string",
            "enum": sorted(PROVIDERS),
            "description": "Force one provider for this call instead of using SEARCH_BACKEND fallback order.",
        }
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("query",),
            properties=properties,
        )


@register_tool
class AdvancedSearchTool(SearchToolBase):
    """جست‌وجوی پیشرفته با نوع نتیجه (text/news/images/videos) و فیلترها."""

    name: ClassVar[str] = "search_advanced"
    description: ClassVar[str] = (
        "Advanced web search with result-type selection (text, news, images, videos), stricter "
        "filters and pagination hints. Use it when a plain search is too generic — e.g. 'news from "
        "the last day about X' or 'official docs only for Y'."
    )
    required_parameters: ClassVar[tuple[str, ...]] = ("query",)
    optional_parameters: ClassVar[tuple[str, ...]] = (
        "num_results",
        "include_domains",
        "exclude_domains",
        "region",
        "safesearch",
        "timelimit",
        "use_cache",
        "search_type",
        "backend",
        "min_snippet_chars",
        "context",
    )

    async def execute(  # type: ignore[override]
        self,
        query: str,
        *,
        num_results: int = 6,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        region: str = "wt-wt",
        safesearch: str = "moderate",
        timelimit: str | None = None,
        use_cache: bool = True,
        search_type: str = "text",
        backend: str | None = None,
        min_snippet_chars: int = 0,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """جست‌وجوی پیشرفته.

        Args:
            query: عبارت جست‌وجو.
            num_results: تعداد نتایج.
            search_type: یکی از text/news/images/videos.
            backend: بازنویسی اجباری ``SEARCH_BACKEND`` برای همین فراخوانی.
            min_snippet_chars: حداقل طول توضیح لازم برای حفظ یک نتیجه.
            بقیه‌ی آرگومان‌ها مانند :class:`WebSearchTool`.
        """
        kind = str(search_type or "text").lower()
        if kind not in {"text", "news", "images", "videos"}:
            raise ValidationError("search_type must be one of: text, news, images, videos")
        result = await self._collect(
            clean_text(query, max_length=500),
            num_results=int(num_results or 6),
            region=str(region or "wt-wt"),
            safesearch=str(safesearch or "moderate"),
            timelimit=timelimit,
            include_domains=SearchProvider._as_domain_list(include_domains),
            exclude_domains=SearchProvider._as_domain_list(exclude_domains),
            use_cache=bool(use_cache),
            context=context,
            search_kind=kind,
            forced_backend=(str(backend).lower() if backend else None),
        )
        if min_snippet_chars and isinstance(result.data, dict):
            result.data["results"] = [
                item
                for item in result.data["results"]
                if len(str(item.get("snippet") or "")) >= int(min_snippet_chars)
            ]
            result.data["count"] = len(result.data["results"])
        if isinstance(result.data, dict):
            result.data["search_type"] = kind
        return result

    def validate_input(self, **kwargs: Any) -> bool:
        """اعتبارسنجی پارامترهای پیشرفته."""
        super().validate_input(**kwargs)
        self._validate_common(kwargs)
        bounded_int(kwargs.get("min_snippet_chars"), name="min_snippet_chars", minimum=0, maximum=2000, default=0)
        return True

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI با فیلدهای پیشرفته."""
        properties = self._common_properties()
        properties.update(
            {
                "search_type": {
                    "type": "string",
                    "enum": ["text", "news", "images", "videos"],
                    "description": "Which DuckDuckGo vertical to query.",
                    "default": "text",
                },
                "backend": {
                    "type": "string",
                    "enum": sorted(PROVIDERS),
                    "description": "Force a specific provider instead of using SEARCH_BACKEND fallback order.",
                },
                "min_snippet_chars": {
                    "type": "integer",
                    "description": "Drop results whose snippet is shorter than this (quality filter).",
                    "minimum": 0,
                    "maximum": 2000,
                },
            }
        )
        return self.function_schema(
            name=self.name, description=self.description, required=("query",), properties=properties
        )

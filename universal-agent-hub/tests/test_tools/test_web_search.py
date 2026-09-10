"""تست ابزارهای جست‌وجوی وب (:mod:`src.tools.web_search`).

هیچ تستی شبکه را لمس نمی‌کند؛ provider ساختگی/آفلاین و monkeypatch کافی است.
برای اجرای تست‌های واقعی (نیازمند اینترنت) از علامت ``network`` استفاده کنید::

    pytest -m network
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from src.config import Config
from src.core.base_tool import ToolContext
from src.models.tool_models import ToolResult
from src.tools import web_search as ws
from src.tools.web_search import (
    AdvancedSearchTool,
    HtmlSearchProvider,
    OfflineSearchProvider,
    SearchProvider,
    SearchResult,
    TavilySearchProvider,
    WebSearchTool,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    """کش بین تست‌ها نباید نتایج را قرض بدهد."""
    ws._CACHE.clear()
    yield
    ws._CACHE.clear()


def make_result(title: str, url: str, snippet: str = "text") -> SearchResult:
    """ساخت نتیجه‌ی تستی."""
    return SearchResult(title=title, url=url, snippet=snippet, source="fake")


class StubProvider(SearchProvider):
    """provider ساختگی با نتایج از پیش تعیین‌شده."""

    id = "stub"

    def __init__(self, results: list[SearchResult] | None = None, error: BaseException | None = None) -> None:
        self._results = results or [
            make_result("Alpha", "https://alpha.example/1"),
            make_result("Beta", "https://beta.example/2", snippet=""),
            make_result("Alpha duplicate", "https://alpha.example/1"),
        ]
        self._error = error
        self.queries: list[str] = []

    def is_available(self, config: Any) -> bool:
        return True

    async def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        self.queries.append(query)
        if self._error:
            raise self._error
        return list(self._results)


def use_stub(monkeypatch: pytest.MonkeyPatch, provider: SearchProvider) -> None:
    """جاگذاری provider ساختگی در همه‌ی مسیرها."""
    ws.PROVIDERS["stub"] = provider
    monkeypatch.setattr(
        ws.SearchToolBase, "_provider_order", staticmethod(lambda config, forced_backend=None: ["stub"])
    )


# ---------------------------------------------------------------------------
# مسیر موفق
# ---------------------------------------------------------------------------
async def test_offline_backend_returns_stub_results(config: Config) -> None:
    """backend آفلاین (پیش‌فرض تست) نتیجه‌ی ثابت می‌دهد."""
    ctx = ToolContext(config=config)
    result = await WebSearchTool(config).run({"query": "anything"}, context=ctx)
    assert result.success
    assert result.data["backend"] == "offline"
    assert result.data["count"] == 1
    assert "formatted" in result.data and "Offline mode" in result.data["formatted"]


async def test_provider_results_are_deduplicated(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """URL تکراری فقط یک بار گزارش می‌شود و ترتیب حفظ می‌شود."""
    provider = StubProvider()
    use_stub(monkeypatch, provider)
    result = await WebSearchTool(config).run(
        {"query": "python", "num_results": 5}, context=ToolContext(config=config)
    )
    urls = [item["url"] for item in result.data["results"]]
    assert urls == ["https://alpha.example/1", "https://beta.example/2"]
    assert result.data["count"] == 2
    assert provider.queries == ["python"]


async def test_domain_filters_are_applied(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """فیلتر دامنه‌ی مثبت و منفی."""
    use_stub(monkeypatch, StubProvider())
    include = await WebSearchTool(config).run(
        {"query": "x", "include_domains": ["alpha.example"]}, context=ToolContext(config=config)
    )
    assert [item["url"] for item in include.data["results"]] == ["https://alpha.example/1"]
    exclude = await WebSearchTool(config).run(
        {"query": "x", "exclude_domains": "alpha.example", "use_cache": False}, context=ToolContext(config=config)
    )
    assert [item["url"] for item in exclude.data["results"]] == ["https://beta.example/2"]


async def test_num_results_limits_output(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """سقف تعداد نتیجه."""
    use_stub(monkeypatch, StubProvider([make_result(f"t{i}", f"https://e{i}.example") for i in range(10)]))
    result = await WebSearchTool(config).run({"query": "x", "num_results": 3}, context=ToolContext(config=config))
    assert result.data["count"] == 3


async def test_cache_is_used_and_can_be_bypassed(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """کش درون‌فرآیندی (با امکان خاموش‌کردن)."""
    provider = StubProvider()
    use_stub(monkeypatch, provider)
    tool = WebSearchTool(config)
    first = await tool.run({"query": "cached"}, context=ToolContext(config=config))
    assert first.success and provider.queries == ["cached"]
    second = await tool.run({"query": "cached"}, context=ToolContext(config=config))
    assert second.data["backend"] == "cache" and second.data["cached"] is True
    assert provider.queries == ["cached"]  # فراخوانی دوم به provider نرفت
    third = await tool.run({"query": "cached", "use_cache": False}, context=ToolContext(config=config))
    assert third.data["backend"] == "stub" and len(provider.queries) == 2


async def test_advanced_tool_search_types(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """نوع جست‌وجو در پروفایل کش و خروجی ثبت می‌شود."""
    use_stub(monkeypatch, StubProvider())
    result = await AdvancedSearchTool(config).run(
        {"query": "news", "search_type": "news"}, context=ToolContext(config=config)
    )
    assert result.success and result.data["search_type"] == "news"
    bad = await AdvancedSearchTool(config).run(
        {"query": "news", "search_type": "podcasts"}, context=ToolContext(config=config)
    )
    assert not bad.success and "text, news, images, videos" in (bad.error or "")


async def test_advanced_min_snippet_filter(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """فیلتر حداقل طول توضیح."""
    use_stub(monkeypatch, StubProvider())
    result = await AdvancedSearchTool(config).run(
        {"query": "q", "min_snippet_chars": 1, "use_cache": False, "search_type": "text"},
        context=ToolContext(config=config),
    )
    assert [item["url"] for item in result.data["results"]] == ["https://alpha.example/1"]


async def test_advanced_forced_backend_is_validated(config: Config) -> None:
    """backend نامعتبر → پیام لیست پشتیبان‌ها."""
    result = await AdvancedSearchTool(config).run(
        {"query": "q", "backend": "google"}, context=ToolContext(config=config)
    )
    assert not result.success and "unknown backend" in (result.error or "")


# ---------------------------------------------------------------------------
# خطاها
# ---------------------------------------------------------------------------
async def test_all_backends_failing_reports_hints(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """وقتی همه‌ی provider ها خطا بدهند، راهنمای عیب‌یابی برگردد."""
    use_stub(monkeypatch, StubProvider(error=RuntimeError("TLS handshake eof")))
    result = await WebSearchTool(config).run({"query": "x"}, context=ToolContext(config=config))
    assert not result.success and result.error_code == "search_unavailable"
    assert "TLS handshake eof" in (result.error or "") and "SEARCH_BACKEND=offline" in (result.error or "")
    assert result.metadata["attempts"][0].startswith("stub:")


async def test_empty_query_is_invalid(config: Config) -> None:
    """query تهی."""
    for tool in (WebSearchTool(config), AdvancedSearchTool(config)):
        result = await tool.run({"query": "   "}, context=ToolContext(config=config))
        assert not result.success and "query" in (result.error or "")


async def test_num_results_bounds(config: Config) -> None:
    """تعداد خارج از بازه."""
    result = await WebSearchTool(config).run({"query": "x", "num_results": 99}, context=ToolContext(config=config))
    assert not result.success and "between 1 and 20" in (result.error or "")


async def test_domains_must_be_lists(config: Config) -> None:
    """نوع اشتباه فیلتر دامنه."""
    result = await WebSearchTool(config).run(
        {"query": "x", "include_domains": 12}, context=ToolContext(config=config)
    )
    assert not result.success and "list of domain strings" in (result.error or "")


async def test_no_provider_available_raises(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """نبود هیچ provider قابل استفاده."""
    monkeypatch.setattr(ws.SearchToolBase, "_provider_order", staticmethod(lambda config, forced_backend=None: []))
    result = await WebSearchTool(config).run({"query": "x"}, context=ToolContext(config=config))
    assert not result.success and "pip install ddgs" in (result.error or "")


# ---------------------------------------------------------------------------
# provider ها (واحد)
# ---------------------------------------------------------------------------
def test_provider_order_respects_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """ترتیب provider بر اساس config و دسترسی‌ها."""
    monkeypatch.setattr(ws.PROVIDERS["ddgs"], "is_available", lambda config: True)
    assert ws.SearchToolBase._provider_order(Config(openai_api_key="x", log_file=None, search_backend="auto")) == [
        "ddgs",
        "html",
    ]
    monkeypatch.setattr(ws.PROVIDERS["ddgs"], "is_available", lambda config: False)
    assert ws.SearchToolBase._provider_order(Config(openai_api_key="x", log_file=None, search_backend="auto")) == [
        "html"
    ]
    # پیش‌فرض امن پروژه آفلاین است تا تست/CI شبکه نزند
    assert ws.SearchToolBase._provider_order(Config(openai_api_key="x", log_file=None)) == ["offline"]
    assert ws.SearchToolBase._provider_order(Config(openai_api_key="x", log_file=None, search_backend="html")) == [
        "html"
    ]
    assert ws.SearchToolBase._provider_order(Config(openai_api_key="x", log_file=None), forced_backend="html") == [
        "html"
    ]
    with pytest.raises(ValueError, match="unknown backend"):
        ws.SearchToolBase._provider_order(Config(openai_api_key="x", log_file=None), forced_backend="bing")


def test_availability_of_tavily_and_offline() -> None:
    """Tavily فقط با کلید فعال است؛ offline همیشه."""
    with_key = Config(openai_api_key="x", log_file=None, tavily_api_key="tvly-xyz")
    assert TavilySearchProvider().is_available(with_key)
    assert not TavilySearchProvider().is_available(Config(openai_api_key="x", log_file=None))
    assert OfflineSearchProvider().is_available(None)
    assert DdgAvailability().ddgs_importable()


class DdgAvailability:
    """کمکیِ خوانا برای تست دسترسی ddgs."""

    def ddgs_importable(self) -> bool:
        """وضعیت import بسته‌ی ddgs با نتیجه‌ی provider یکسان است؟"""
        try:
            import ddgs  # noqa: F401

            expected = True
        except ImportError:
            expected = False
        return ws.DdgSearchProvider().is_available(None) is expected


async def test_offline_provider_shape() -> None:
    """شکل خروجی provider آفلاین."""
    results = await OfflineSearchProvider().search("anything", num_results=1)
    assert len(results) == 1 and results[0].url == "https://example.com/offline"
    assert results[0].source == "offline"


async def test_html_provider_parses_ddg_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """تجزیه‌ی HTML داکیومنت نتایج (بدون شبکه)."""
    html = """
    <div class="result">
      <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html&amp;rut=x">asyncio &mdash; Python 3 docs</a></h2>
      <a class="result__snippet" href="#">Access to the <b>asyncio</b> documentation.</a>
    </div>
    <div class="result">
      <h2><a class="result__a" href="mailto:nobody@example.com">skip me</a></h2>
    </div>
    """

    async def fake_fetch(self, url: str, *, config: Any, timeout: float) -> str:  # noqa: ANN001
        return html

    monkeypatch.setattr(HtmlSearchProvider, "_fetch", fake_fetch)
    results = await HtmlSearchProvider().search("asyncio docs", config=Config(openai_api_key="x", log_file=None))
    assert len(results) == 1
    assert results[0].url == "https://docs.python.org/3/library/asyncio.html"
    assert "asyncio" in results[0].title and "Python 3 docs" in results[0].title
    assert results[0].snippet.startswith("Access to the asyncio documentation")


async def test_html_provider_handles_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """صفحه‌ی بی‌نتایج → فهرست خالی (نه خطا)."""

    async def fake_fetch(self, url: str, *, config: Any, timeout: float) -> str:  # noqa: ANN001
        return "<html><body>no results</body></html>"

    monkeypatch.setattr(HtmlSearchProvider, "_fetch", fake_fetch)
    assert await HtmlSearchProvider().search("nothing here") == []


async def test_apply_filters_helper() -> None:
    """تابع فیلتر (unit)."""
    data = [make_result("a", "https://x.io/a", "short"), make_result("b", "https://y.io/b", "a much longer snippet")]
    assert [item.url for item in SearchProvider.apply_filters(data, include_domains=["y.io"])] == ["https://y.io/b"]
    assert SearchProvider.apply_filters(data, exclude_domains=["x.io", "y.io"]) == []
    assert [item.url for item in SearchProvider.apply_filters(data, min_snippet_chars=10)] == ["https://y.io/b"]
    assert SearchProvider.apply_filters(data) == data


def test_search_result_to_dict() -> None:
    """سریال‌سازی نتیجه."""
    item = SearchResult(title="t", url="u", snippet="s", source="src", extra={"date": "2026"})
    assert item.to_dict() == {"title": "t", "url": "u", "snippet": "s", "source": "src", "date": "2026"}
    assert "source" not in SearchResult(title="t", url="u").to_dict()


async def test_ddgs_provider_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """normalize کردن خروجی ddgs (با DDGS ساختگی)."""

    class FakeDDGS:
        """جای‌نمای DDGS."""

        def __init__(self, timeout: float = 0) -> None:
            self.timeout = timeout

        def __enter__(self) -> FakeDDGS:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            assert kwargs["max_results"] == 4
            return [
                {"title": "one", "href": "https://one.example", "body": "body one", "date": "2026-01-01"},
                {"title": "no url"},
                "not-a-dict",
            ]

        def news(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"title": "news item", "url": "https://news.example", "source": "example"}]

    ddgs = pytest.importorskip("ddgs")

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    provider = ws.DdgSearchProvider()
    results = await provider.search("q", num_results=4, config=Config(openai_api_key="x", log_file=None))
    assert len(results) == 1
    assert results[0].extra == {"date": "2026-01-01"}
    news = await provider.search("q", num_results=4, kind="news", config=None)
    assert news[0].url == "https://news.example" and news[0].source == "ddgs"


def test_schemas_of_search_tools(config: Config) -> None:
    """اسکیمای هر دو ابزار."""
    for tool in (WebSearchTool(config), AdvancedSearchTool(config)):
        schema = tool.get_schema()["function"]
        assert schema["parameters"]["required"] == ["query"]
        assert "timelimit" in schema["parameters"]["properties"]
    advanced = AdvancedSearchTool(config).get_schema()["function"]["parameters"]["properties"]
    assert "search_type" in advanced and "backend" in advanced
    assert set(advanced["backend"]["enum"]) == set(ws.PROVIDERS)


@pytest.mark.network  # نیازمند اینترنت؛ فقط با ``UAGENT_ONLINE=1`` اجرا می‌شود
@pytest.mark.skipif(not os.environ.get("UAGENT_ONLINE"), reason="sandbox بدون اینترنت؛ با UAGENT_ONLINE=1 فعال کنید")
async def test_real_ddgs_search_smoke(config: Config) -> None:
    """دود‌سنج واقعی (opt-in)."""
    cfg = config.model_copy(update={"search_backend": "auto"})
    result = await WebSearchTool(cfg).run(
        {"query": "python asyncio documentation", "num_results": 3}, context=ToolContext(config=cfg)
    )
    assert isinstance(result, ToolResult)
    assert result.success and result.data["count"] >= 1


async def test_registry_registration(config: Config) -> None:
    """هر دو ابزار در رجیستری ثبت‌اند و جدا هستند."""
    from src.core.tool_registry import ToolRegistry, discover_tools

    discover_tools(force=True)
    assert ToolRegistry.get("web_search") is not ToolRegistry.get("search_advanced")
    assert ToolRegistry.get("web_search").category.value == "network"  # type: ignore[union-attr]

"""تست رجیستری ابزارها (:mod:`src.core.tool_registry`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.config import Config
from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import (
    ToolRegistry,
    discover_tools,
    register_tool,
    registry_snapshot,
    tools_module,
    unregister_tool,
)
from src.models.tool_models import ToolCategory, ToolResult
from src.tools import register_builtin_tools


class _Dummy(BaseTool):
    """ابزار تستی ساده."""

    name = "dummy_tool"
    description = "dummy"

    async def execute(self, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        return ToolResult.ok(kwargs, tool=self.name)

    def get_schema(self) -> dict[str, Any]:
        return self.function_schema(self.name, self.description, required=(), properties={})


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """رجیستری را بعد از هر تست به حالت اول برمی‌گرداند."""
    original_tools = ToolRegistry.classes()
    original_instances = dict(ToolRegistry._instances)  # noqa: SLF001
    original_discovered = getattr(ToolRegistry, "_discovered", False)
    yield
    ToolRegistry._tools = dict(original_tools)  # noqa: SLF001
    ToolRegistry._instances = original_instances  # noqa: SLF001
    ToolRegistry._discovered = original_discovered  # noqa: SLF001


def test_register_and_get() -> None:
    """ثبت، دریافت و information یک ابزار."""
    register_tool(_Dummy)
    tool = ToolRegistry.get("dummy_tool")
    assert isinstance(tool, _Dummy)
    assert ToolRegistry.get("does_not_exist") is None
    info = tool.to_info() if tool else None
    assert info and info.name == "dummy_tool"
    assert "dummy_tool" in ToolRegistry.names()


def test_duplicate_registration_rejected() -> None:
    """نام تکراری بدون replace خطا می‌دهد."""
    register_tool(_Dummy)

    class Other(_Dummy):
        """ابزار هم‌نام دیگر."""

        name = "dummy_tool"

    with pytest.raises(ValueError, match="already registered"):
        register_tool(Other)
    register_tool(Other, replace=True)
    assert ToolRegistry.get("dummy_tool").__class__ is Other  # type: ignore[union-attr]


def test_register_rejects_non_tools() -> None:
    """ورودی نامعتبر و کلاس انتزاعی رد می‌شود."""

    class NotATool:
        """کلاس بی‌ربط."""

    with pytest.raises(TypeError, match="BaseTool"):
        ToolRegistry.register(NotATool)  # type: ignore[arg-type]

    class Abstract(BaseTool):
        """ابزاری که execute را پیاده نکرده."""

        name = "abstract_tool"
        description = "missing execute"

        def get_schema(self) -> dict[str, Any]:
            return {}

    with pytest.raises(TypeError, match="does not implement"):
        ToolRegistry.register(Abstract)

    class Nameless(BaseTool):
        """ابزار بدون name."""

        name = "base_tool"
        description = "no name"

        async def execute(self, **kwargs: Any) -> Any:  # type: ignore[override]
            raise NotImplementedError

        def get_schema(self) -> dict[str, Any]:
            return {}

    with pytest.raises(ValueError, match="unique"):
        ToolRegistry.register(Nameless)


def test_unregister_and_schemas() -> None:
    """حذف ابزار و تولید schema یکتا."""
    register_tool(_Dummy)
    schemas = ToolRegistry.schemas(only=["dummy_tool"])
    assert schemas[0]["function"]["name"] == "dummy_tool"
    assert ToolRegistry.unregister("dummy_tool")
    assert not ToolRegistry.unregister("dummy_tool")


def test_instances_filtering_by_category(config: Config) -> None:
    """فیلتر نمونه‌ها بر اساس دسته."""
    discover_tools(force=True)
    filesystem_tools = ToolRegistry.instances(categories=[ToolCategory.FILESYSTEM], config=config)
    assert filesystem_tools
    assert all(tool.category is ToolCategory.FILESYSTEM for tool in filesystem_tools)
    assert len(filesystem_tools) == len({tool.name for tool in filesystem_tools})


def test_lazy_instances_use_config(config: Config) -> None:
    """نمونه‌ها با config ساخته می‌شوند و با config تازه عوض می‌شوند."""
    discover_tools(force=True)
    ToolRegistry.set_default_config(config)
    first = ToolRegistry.get("read_file")
    assert first is not None and first.config is config
    other = Config(openai_api_key="sk-other", log_file=None)
    second = ToolRegistry.get("read_file", config=other)
    assert second is not first and second.config is other  # type: ignore[union-attr]
    ToolRegistry.set_default_config(other)
    assert ToolRegistry.get("read_file").config is other  # type: ignore[union-attr]


def test_discover_imports_all_builtin_modules() -> None:
    """کشف، همه‌ی ماژول‌های استاندارد را import می‌کند."""
    names = discover_tools(force=True)
    for expected in (
        "terminal_run",
        "read_file",
        "write_file",
        "list_directory",
        "delete_file",
        "move_file",
        "search_files",
        "browser_browse",
        "browser_extract_text",
        "web_search",
        "search_advanced",
        "os_info",
        "cpu_info",
        "memory_info",
        "disk_info",
        "network_info",
    ):
        assert expected in names, f"missing tool {expected}"
    assert register_builtin_tools() == names


def test_schemas_are_valid_openai_functions() -> None:
    """اسکیمای همه‌ی ابزارها برای OpenAI معتبر است."""
    discover_tools(force=True)
    schemas = ToolRegistry.get_schemas()
    assert schemas
    seen: set[str] = set()
    for schema in schemas:
        assert schema["type"] == "function"
        function = schema["function"]
        name = function["name"]
        assert name and name not in seen, f"duplicate tool name {name}"
        seen.add(name)
        assert function["description"], f"{name} has no description"
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        for required in parameters["required"]:
            assert required in parameters["properties"], f"{name} requires unknown {required}"
        for prop, spec in parameters["properties"].items():
            assert spec.get("description"), f"{name}.{prop} has no description"
            assert spec.get("type") in {"string", "integer", "number", "boolean", "object", "array"}, f"{name}.{prop}"


def test_register_module_with_prefix() -> None:
    """ثبت دستی ابزارهای یک ماژول با پیشوند."""
    module = tools_module("template")
    registered = ToolRegistry.register_module(module, prefix="sample")
    assert registered == ["sample_new_tool"]
    tool = ToolRegistry.get("sample_new_tool")
    assert tool is not None and tool.name == "sample_new_tool"


def test_build_context_and_snapshot() -> None:
    """ساخت context و snapshot رجیستری."""
    discover_tools(force=True)
    context = ToolRegistry.build_context(config=Config(openai_api_key="sk", log_file=None), session={"a": 1})
    assert context.session["a"] == 1
    snapshot = registry_snapshot()
    assert snapshot["count"] >= 15 and "terminal_run" in snapshot["names"]
    assert "filesystem" in snapshot["categories"]


async def test_registered_tool_can_execute() -> None:
    """ابزار ثبت‌شده قابل اجراست (integration سبک با رجیستری)."""
    register_tool(_Dummy)
    result = await ToolRegistry.get("dummy_tool").run({"hello": "world"})
    assert result.success and result.data == {"hello": "world"}


def test_registry_rejects_unsafe_tool_name_conflicts() -> None:
    """حذف ابزار نباید رجیستری را بشکند."""
    register_tool(_Dummy)
    unregister_tool("dummy_tool")
    assert "dummy_tool" not in ToolRegistry.names()


class TestFrozenDiscovery:
    """کشف ابزار در حالت فریزشده (باینلی PyInstaller/zipapp)."""

    def test_builtin_fallback_when_directory_not_scannable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`iter_modules` خالی برگردد → فهرست داخلی import می‌شود (برنامه بی‌ابزار نمی‌ماند)."""
        import pkgutil

        from src.core import tool_registry as registry_module

        ToolRegistry._tools = {}  # noqa: SLF001 - ثبت‌نام را از صفر شروع می‌کنیم
        ToolRegistry._discovered = False  # noqa: SLF001
        monkeypatch.setattr(pkgutil, "iter_modules", lambda *a, **k: [])
        names = ToolRegistry.discover(force=True)
        assert {"terminal_run", "read_file", "os_info", "web_search"}.issubset(names)
        assert registry_module.BUILTIN_TOOL_MODULES  # ثابت فهرست باید موجود باشد

    def test_template_is_never_autoloaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`template` نه در اسکن دایرکتوری و نه در فهرست داخلی نباید باشد."""
        import pkgutil
        from types import SimpleNamespace

        from src.core import tool_registry as registry_module

        monkeypatch.setattr(
            pkgutil, "iter_modules", lambda *a, **k: [SimpleNamespace(name="template"), SimpleNamespace(name="_x")]
        )
        ToolRegistry._discovered = False  # noqa: SLF001
        assert "template" not in registry_module.BUILTIN_TOOL_MODULES
        before = dict(ToolRegistry._tools)  # noqa: SLF001
        ToolRegistry.discover(force=True)
        assert "template" not in ToolRegistry._tools  # noqa: SLF001
        assert set(ToolRegistry._tools) >= set(before)  # noqa: SLF001


class TestPluginDirectories:
    """پلاگین‌های کاربر از ``AGENT_HUB_TOOL_DIRS`` (یا آرگومان مستقیم)."""

    PLUGIN = '''"""یک ابزار پلاگین ساده برای تست کشف پوشه."""

from typing import Any, ClassVar

from src.core.base_tool import BaseTool
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult


@register_tool
class PluginEchoTool(BaseTool):
    """ابزار تستی که متن را برمی‌گرداند (از پوشه‌ی پلاگین بارگذاری می‌شود)."""

    name: ClassVar[str] = "plugin_echo"
    description: ClassVar[str] = "Echo the given text back (test plugin)."
    category: ClassVar[ToolCategory] = ToolCategory.CUSTOM
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    required_parameters: ClassVar[tuple[str, ...]] = ("text",)

    async def execute(self, text: str = "", **_kwargs: Any) -> ToolResult:  # type: ignore[override]
        """برگرداندن همان متن."""
        return ToolResult.ok(text, tool=self.name)

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling این ابزار."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("text",),
            properties={"text": {"type": "string", "description": "Text to echo."}},
        )
'''

    @pytest.fixture
    def plugin_dir(self, tmp_path: Path) -> Path:
        """پوشه‌ای با یک فایل ابزار و چند فایل مزاحم (باید نادیده گرفته شوند)."""
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "plugin_echo.py").write_text(self.PLUGIN, encoding="utf-8")
        (tools / "_skipme.py").write_text("raise RuntimeError('must not be imported')", encoding="utf-8")
        (tools / "template.py").write_text("raise RuntimeError('must not be imported')", encoding="utf-8")
        (tools / "broken.py").write_text("this is not python", encoding="utf-8")
        return tools

    def test_load_plugins_registers_tool(self, plugin_dir: Path, registered: None) -> None:
        """import موفق → ابزار ثبت می‌شود و فایل‌های خراب فقط لاگ می‌شوند."""
        loaded = ToolRegistry.load_plugins([plugin_dir])
        assert any(name.endswith("plugin_echo") for name in loaded)
        assert ToolRegistry.get("plugin_echo") is not None
        assert not any("broken" in name for name in loaded)

    async def test_plugin_tool_runs(self, plugin_dir: Path, context: ToolContext) -> None:
        """ابزار پلاگین مثل ابزار داخلی اجرا می‌شود."""
        ToolRegistry.load_plugins([plugin_dir])
        tool = ToolRegistry.get("plugin_echo", config=context.config)
        assert tool is not None
        result = await tool.run({"text": "hi"}, context=context)
        assert result.success is True and result.data == "hi"

    def test_env_var_is_used_by_discover(
        self, plugin_dir: Path, monkeypatch: pytest.MonkeyPatch, registered: None
    ) -> None:
        """``discover()`` بعد از کشف پکیج، پوشه‌های محیطی را هم بار می‌کند."""
        monkeypatch.setenv("AGENT_HUB_TOOL_DIRS", str(plugin_dir))
        ToolRegistry._discovered = False  # noqa: SLF001
        ToolRegistry.discover(force=True)
        assert "plugin_echo" in ToolRegistry.names()

    def test_missing_directory_is_ignored(self, tmp_path: Path) -> None:
        """پوشه نبودن → فهرست خالی، بدون استثنا."""
        assert ToolRegistry.load_plugins([tmp_path / "nope"]) == []
        assert ToolRegistry.load_plugins([]) == []

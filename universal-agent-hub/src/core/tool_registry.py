"""ثبت‌کننده‌ی ابزارها (Tool registry).

الگوی پیاده‌سازی‌شده: **Registry + Strategy + (lazy) Plugin discovery**

* ابزارها با decorator ``@register_tool`` ثبت می‌شوند.
* ثبت‌نام «کلاس» است و نمونه‌سازی lazy انجام می‌شود؛ بنابراین import شدن یک
  ابزار (مثلاً مرورگر که به Playwright نیاز دارد) هزینه‌ی runtime ندارد.
* ابزارها می‌توانند از پیکربندی (config) هنگام ساخت نمونه استفاده کنند؛ برای
  همین هر بار که config عوض شود، `instances_for()` نمونه‌های تازه می‌سازد.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import os
import pkgutil
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, cast

from src.core.base_tool import BaseTool, ToolContext
from src.models.tool_models import ToolCategory, ToolInfo

__all__ = [
    "TOOL_DIRS_ENV",
    "ToolRegistry",
    "discover_tools",
    "register_tool",
    "tools_module",
    "unregister_tool",
]

#: پکیج پیش‌فرض ابزارها
TOOLS_PACKAGE = "src.tools"

#: متغیر محیطی فهرست پوشه‌های پلاگین (جداکننده: ``os.pathsep`` یا ویرگول)
TOOL_DIRS_ENV = "AGENT_HUB_TOOL_DIRS"

#: ابزارهای داخلی که همیشه باید import شوند (حتی وقتی دایرکتوری بسته قابل اسکن نیست،
#: مثلاً در باینلی فریزشده‌ی PyInstaller یا zipapp). فایل `template.py` عمداً اینجا نیست.
BUILTIN_TOOL_MODULES: tuple[str, ...] = (
    "terminal",
    "filesystem",
    "browser",
    "web_search",
    "system_info",
    "memory",
)


class ToolRegistry:
    """کاتالوگ سراسری ابزارها (stateless بر اساس نام کلاس).

    Attributes:
        _tools: نگاشت نام ابزار → کلاس ابزار.
        _instances: نگاشت نام ابزار → نمونه‌ی ساخته‌شده (با config پیش‌فرض).
        _default_config: config پیش‌فرض که برای ساخت نمونه‌ها استفاده می‌شود.
    """

    _tools: ClassVar[dict[str, type[BaseTool]]] = {}
    _instances: ClassVar[dict[str, BaseTool]] = {}
    _default_config: Any = None
    _instance_cache_key: int | None = None

    # ------------------------------------------------------------------
    # ثبت‌نام
    # ------------------------------------------------------------------
    @classmethod
    def register(cls, tool_class: type[BaseTool], *, replace: bool = False) -> type[BaseTool]:
        """ثبت یک کلاس ابزار.

        Args:
            tool_class: کلاسِ فرزندِ :class:`BaseTool`.
            replace: اجازه‌ی بازنویسی ابزار هم‌نام.

        Returns:
            همان کلاس (تا بتواند به‌عنوان decorator استفاده شود).

        Raises:
            TypeError: ورودی زیرکلاس BaseTool نیست یا abstract باقی مانده است.
            ValueError: ابزار هم‌نام ثبت شده و ``replace=False`` است.
        """
        if not (isinstance(tool_class, type) and issubclass(tool_class, BaseTool)):
            raise TypeError(f"{tool_class!r} is not a BaseTool subclass")
        abstracts = {
            name
            for name, attr in inspect.getmembers(tool_class, predicate=inspect.isfunction)
            if getattr(attr, "__isabstractmethod__", False)
        }
        if abstracts and inspect.isabstract(tool_class):
            raise TypeError(f"tool {tool_class.__name__} does not implement: {', '.join(sorted(abstracts))}")
        if not getattr(tool_class, "name", "") or tool_class.name == BaseTool.name:
            raise ValueError(f"tool {tool_class.__name__} must define a unique 'name'")
        if tool_class.name in cls._tools and not replace:
            raise ValueError(f"tool '{tool_class.name}' is already registered (pass replace=True to override)")
        cls._tools[tool_class.name] = tool_class
        cls._instances.pop(tool_class.name, None)
        cls._emit_registry_event(f"registered:{tool_class.name}")
        return tool_class

    @classmethod
    def unregister(cls, name: str) -> bool:
        """حذف یک ابزار از کاتالوگ.

        Returns:
            ``True`` اگر ابزاری حذف شد.
        """
        removed = cls._tools.pop(name, None) is not None
        cls._instances.pop(name, None)
        if removed:
            cls._emit_registry_event(f"unregistered:{name}")
        return removed

    @classmethod
    def clear(cls) -> None:
        """پاک کردن کامل رجیستری (فقط برای تست)."""
        cls._tools.clear()
        cls._instances.clear()
        cls._instance_cache_key = None

    @classmethod
    def set_default_config(cls, config: Any) -> None:
        """تنظیم config پیش‌فرض برای ساخت نمونه‌ها (اعتبار config را عوض می‌کند)."""
        cls._default_config = config
        cls._instances.clear()
        cls._instance_cache_key = id(config)

    # ------------------------------------------------------------------
    # کشف (discovery)
    # ------------------------------------------------------------------
    @classmethod
    def discover(cls, package: str = TOOLS_PACKAGE, *, force: bool = False) -> list[str]:
        """import کردن همه‌ی ماژول‌های یک پکیج تا decoratorها اجرا شوند.

        Args:
            package: نام پکیج ابزارها.
            force: حتی اگر قبلاً کشف شده، دوباره انجام شود.

        Returns:
            فهرست نام ابزارهای ثبت‌شده پس از کشف.
        """
        if not force and getattr(cls, "_discovered", False) and package == TOOLS_PACKAGE:
            return cls._register_from_loaded_modules(package) or list(cls._tools)
        try:
            module = importlib.import_module(package)
        except ModuleNotFoundError as exc:  # pragma: no cover - بسته‌ی ناقص
            from src.utils.logger import get_logger

            get_logger("core.tool_registry").warning("tool discovery skipped: %s", exc)
            return list(cls._tools)
        path = getattr(module, "__path__", None)
        if path is None:
            return list(cls._tools)
        found = [info.name for info in pkgutil.iter_modules(path)]
        if not found and package == TOOLS_PACKAGE:
            # باینلی‌های فریزشده (PyInstaller) و zipapp دایرکتوری قابل‌اسکن ندارند؛
            # فهرست ثابت ابزارهای داخلی را وارد می‌کنیم تا برنامه بی‌ابزار نشود.
            found = list(BUILTIN_TOOL_MODULES)
        for name in found:
            if name.startswith("_") or name == "template":
                continue
            try:
                importlib.import_module(f"{package}.{name}")
            except Exception as exc:  # noqa: BLE001 - ابزار خراب نباید کل برنامه را بخواباند
                from src.utils.logger import get_logger

                get_logger("core.tool_registry").error("failed to import tool module '%s.%s': %s", package, name, exc)
        cls._discovered = True  # type: ignore[attr-defined]
        # ماژول‌ها ممکن است قبلاً import شده باشند (مثلاً در یک تست دیگر)؛ decorator
        # در آن صورت دوباره اجرا نمی‌شود، پس ابزارها را از sys.modules بازثبت می‌کنیم.
        cls._register_from_loaded_modules(package)
        if package == TOOLS_PACKAGE:
            # پلاگین‌های کاربر (کنار باینلی یا در ~/.agent-hub/tools) — خطا نادیده می‌ماند
            with contextlib.suppress(Exception):
                cls.load_plugins()
        return list(cls._tools)

    @classmethod
    def _register_tools_from(cls, module: ModuleType) -> list[str]:
        """ثبت (دوباره‌ی) ابزارهای یک ماژول، بی‌نیاز از اجرای مجدد decorator."""
        registered: list[str] = []
        for attr in vars(module).values():
            tool_name = getattr(attr, "name", "")
            if isinstance(attr, type) and issubclass(attr, BaseTool) and attr is not BaseTool:
                if not isinstance(tool_name, str) or not tool_name or inspect.isabstract(attr):
                    continue
                cls.register(attr, replace=True)
                registered.append(tool_name)
        return registered

    @classmethod
    def load_plugins(cls, directories: Iterable[str | Path] | None = None) -> list[str]:
        """import ماژول‌های ابزار داخل پوشه‌های سفارشی (پلاگین).

        پوشه‌ها یا از آرگومان داده می‌شوند یا از متغیر محیطی
        ``AGENT_HUB_TOOL_DIRS`` (جداکننده ``os.pathsep``؛ ویرگول هم پذیرفته می‌شود).
        هر ``*.py`` که با زیرخط شروع نشود و ``template`` نباشد import می‌شود؛
        ``@register_tool`` داخل همان فایل کار ثبت را می‌کند. فایل خراب فقط لاگ
        می‌شود و اجرای بقیه را نمی‌خواباند.

        Args:
            directories: فهرست پوشه؛ ``None`` یعنی فقط متغیر محیطی.

        Returns:
            نام ماژول‌هایی که با موفقیت import شدند.
        """
        from src.utils.logger import get_logger

        logger = get_logger("core.tool_registry")
        if directories is None:
            raw = os.environ.get(TOOL_DIRS_ENV, "")
        else:
            raw = os.pathsep.join(str(item) for item in directories)
        loaded: list[str] = []
        seen_dirs: set[Path] = set()
        for entry in raw.replace(",", os.pathsep).split(os.pathsep):
            directory = Path(entry.strip()).expanduser() if entry.strip() else None
            if directory is None or not directory.is_dir() or directory in seen_dirs:
                continue
            seen_dirs.add(directory)
            if str(directory) not in sys.path:
                sys.path.insert(0, str(directory))
            for file in sorted(directory.glob("*.py")):
                stem = file.stem
                if stem.startswith("_") or stem == "template":
                    continue
                module_name = f"agent_hub_plugin.{stem}"
                cached = sys.modules.get(module_name)
                if cached is not None:
                    # بار دوم decorator اجرا نمی‌شود (مثلاً رجیستری در تست پاک شده)؛
                    # ابزارهای همان ماژول را مستقیم دوباره ثبت می‌کنیم.
                    cls._register_tools_from(cached)
                    loaded.append(module_name)
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(module_name, file)
                    if spec is None or spec.loader is None:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)
                    loaded.append(module_name)
                except Exception as exc:  # noqa: BLE001 - پلاگین خراب نباید برنامه را بخواباند
                    sys.modules.pop(module_name, None)
                    logger.error("plugin '%s' failed to load: %s", file, exc)
        if loaded:
            logger.info("loaded %d tool plugin module(s) from %d dir(s)", len(loaded), len(seen_dirs))
        return loaded

    @classmethod
    def _register_from_loaded_modules(cls, package: str) -> None:
        """ثبت ابزارهای کشف‌نشده در ماژول‌های import‌شده‌ی همان پکیج."""
        import sys

        for name, module in list(sys.modules.items()):
            if not (name == package or name.startswith(package + ".")):
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseTool)
                    and obj is not BaseTool
                    and getattr(obj, "__module__", "") == name
                    and not inspect.isabstract(obj)
                    and getattr(obj, "name", "")
                    and obj.name not in cls._tools
                ):
                    try:
                        cls.register(obj)
                    except (TypeError, ValueError):
                        continue

    @classmethod
    def register_module(cls, module: ModuleType | str, *, prefix: str = "") -> list[str]:
        """ثبت دستی همه‌ی ابزارهای یک ماژول (مفید برای پلاگین‌های بیرونی).

        Args:
            module: ماژول یا نام ماژول.
            prefix: پیشوند اختیاری برای جلوگیری از تداخل نام.

        Returns:
            نام‌های ثبت‌شده.
        """
        target = importlib.import_module(module) if isinstance(module, str) else module
        registered: list[str] = []
        for _, obj in inspect.getmembers(target, inspect.isclass):
            if obj is BaseTool or not issubclass(obj, BaseTool) or obj.__module__ != target.__name__:
                continue
            if inspect.isabstract(obj):
                continue
            if prefix:
                obj = cast(
                    "type[BaseTool]", type(f"{prefix}_{obj.__name__}", (obj,), {"name": f"{prefix}_{obj.name}"})
                )
            cls.register(obj)
            registered.append(obj.name)
        return registered

    # ------------------------------------------------------------------
    # دریافت ابزار
    # ------------------------------------------------------------------
    @classmethod
    def classes(cls) -> dict[str, type[BaseTool]]:
        """نگاشت نام → کلاس ابزار (نسخه‌ی copy-safe)."""
        return dict(cls._tools)

    @classmethod
    def names(cls) -> list[str]:
        """نام همه‌ی ابزارهای ثبت‌شده (مرتب)."""
        return sorted(cls._tools)

    @classmethod
    def get(cls, name: str, *, config: Any = None) -> BaseTool | None:
        """دریافت نمونه‌ی ابزار طبق نام.

        Args:
            name: نام ابزار.
            config: در صورت ارائه، نمونه‌ای مستقل از کش با همین config ساخته می‌شود.

        Returns:
            نمونه‌ی ابزار یا ``None``.
        """
        tool_class = cls._tools.get(name)
        if tool_class is None:
            return None
        if config is not None:
            return tool_class(config)
        key = id(config) if config is not None else id(cls._default_config)
        if cls._instance_cache_key is not None and cls._instance_cache_key != key:
            cls._instances.clear()
        cls._instance_cache_key = key
        cached = cls._instances.get(name)
        if cached is None:
            cached = tool_class(cls._default_config)
            cls._instances[name] = cached
        return cached

    @classmethod
    def instances(
        cls,
        *,
        config: Any = None,
        only: Iterable[str] | None = None,
        categories: Iterable[ToolCategory] | None = None,
    ) -> list[BaseTool]:
        """ساخت/دریافت فهرست نمونه‌ها با فیلتر اختیاری.

        Args:
            config: پیکربندی مورد استفاده برای ساخت نمونه‌ها.
            only: فقط این نام‌ها.
            categories: فقط این دسته‌ها.

        Returns:
            فهرست ابزارها به ترتیب الفبای نام.
        """
        names = list(only) if only is not None else sorted(cls._tools)
        wanted_categories = {
            category if isinstance(category, ToolCategory) else ToolCategory(category)
            for category in (categories or [])
        }
        result: list[BaseTool] = []
        for name in names:
            tool = cls.get(name, config=config)
            if tool is None:
                continue
            if wanted_categories and tool.category not in wanted_categories:
                continue
            result.append(tool)
        return result

    @classmethod
    def get_all(cls, *, config: Any = None) -> list[BaseTool]:
        """دریافت همه‌ی ابزارهای ثبت‌شده (معادل ``instances()``)."""
        return cls.instances(config=config)

    @classmethod
    def schemas(cls, *, only: Iterable[str] | None = None, config: Any = None) -> list[dict[str, Any]]:
        """اسکیمای OpenAI همه‌ی ابزارها (یا زیرمجموعه‌ای از آن‌ها)."""
        schemas: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tool in cls.instances(config=config, only=only):
            schema = tool.get_schema()
            name = str(schema.get("function", {}).get("name") or tool.name)
            if name in seen:
                continue
            seen.add(name)
            schemas.append(schema)
        return schemas

    @classmethod
    def get_schemas(cls, *, config: Any = None) -> list[dict[str, Any]]:
        """نام سازگار با پیکربندی درخواستی برای اسکیمای همه‌ی ابزارها."""
        return cls.schemas(config=config)

    @classmethod
    def info(cls, *, config: Any = None) -> list[ToolInfo]:
        """اطلاعات توصیفی ابزارها (برای CLI/docs)."""
        return [tool.to_info() for tool in cls.instances(config=config)]

    @classmethod
    def build_context(
        cls,
        *,
        config: Any = None,
        safety: Any = None,
        bus: Any = None,
        confirm: Callable[..., Any] | None = None,
        session: dict[str, Any] | None = None,
    ) -> ToolContext:
        """ساخت :class:`ToolContext` با پیش‌فرض‌های امن."""
        return ToolContext(
            config=config if config is not None else cls._default_config,
            safety=safety,
            bus=bus,
            confirm=confirm,
            session=session or {},
        )

    @classmethod
    def iter_names(cls) -> Iterator[str]:
        """پیمایش نام‌ها (برای اسکریپت‌ها)."""
        return iter(sorted(cls._tools))

    @classmethod
    def _emit_registry_event(cls, payload: str) -> None:
        """انتشار رویداد تغییر رجیستری (بی‌صدا اگر bus در دسترس نبود)."""
        bus = getattr(cls._default_config, "event_bus", None)
        if bus is None:
            return
        with contextlib.suppress(Exception):  # pragma: no cover - مسیر جانبی و اختیاری
            publish = getattr(bus, "publish_nowait", None)
            if publish is not None:
                publish("registry.changed", {"detail": payload}, source="core.tool_registry")


# ----------------------------------------------------------------------
# API راحت (functional)
# ----------------------------------------------------------------------
def register_tool(tool_class: type[BaseTool] | None = None, *, replace: bool = False) -> Any:
    """Decorator ثبت ابزار.

    قابل استفاده هم به‌صورت ``@register_tool`` و هم ``@register_tool(replace=True)``.
    """
    if tool_class is None:  # استفاده با آرگومان: @register_tool(replace=True)
        return lambda cls_: ToolRegistry.register(cls_, replace=replace)
    return ToolRegistry.register(tool_class, replace=replace)


def unregister_tool(name: str) -> bool:
    """حذف ابزار از رجیستری (میان‌بر برای ``ToolRegistry.unregister``)."""
    return ToolRegistry.unregister(name)


def discover_tools(package: str = TOOLS_PACKAGE, *, force: bool = False) -> list[str]:
    """کشف ابزارها از یک پکیج (میان‌بر برای ``ToolRegistry.discover``)."""
    return ToolRegistry.discover(package, force=force)


def tools_module(name: str) -> ModuleType:
    """import امن یک ماژول ابزار (برای اسکریپت‌های سفارشی)."""
    return importlib.import_module(f"{TOOLS_PACKAGE}.{name}")


def registry_snapshot(root: Path | None = None) -> dict[str, Any]:
    """خلاصه‌ی رجیستری برای مستندات/عیب‌یابی."""
    infos = ToolRegistry.info()
    return {
        "count": len(infos),
        "names": [info.name for info in infos],
        "needs_confirmation": [info.name for info in infos if info.requires_confirmation],
        "categories": sorted({info.category.value for info in infos}),
        "root": str(root) if root else None,
    }

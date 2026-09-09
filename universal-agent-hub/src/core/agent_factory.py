"""کارخانه‌ی ساخت ایجنت (Factory pattern).

:func:`src.agent.UniversalAgent` همه‌ی قطعات (config، رجیستری ابزارها، باس
رویداد، نگهبان ایمنی) را کنار هم می‌چیند. کارخانه این چیدمان را برای
«پروفایل‌های» آماده تکرارپذیر می‌کند::

    from src.core.agent_factory import AgentFactory

    agent = AgentFactory.create("generalist")          # همه ابزارها
    agent = AgentFactory.create("read_only")            # بدون نوشتن/حذف
    agent = AgentFactory.create(profile="developer", config=my_config)

پروفایل‌ها را می‌توان با :meth:`AgentFactory.register` گسترش داد یا از فایل
JSON بارگذاری کرد (``src/models/config_models.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, ClassVar

from src.agent import UniversalAgent
from src.config import Config, get_config
from src.core.event_bus import EventBus, install_default_observers
from src.core.tool_registry import ToolRegistry, discover_tools
from src.models.agent_models import AgentState  # noqa: F401  (برای export سازگار در type-check)
from src.models.config_models import AgentProfile, SafetySettings, load_profile_file
from src.utils.logger import get_logger

__all__ = ["PROFILE_REGISTRY", "AgentFactory"]

#: رجیستری ساده‌ی نام → پروفایل
PROFILE_REGISTRY: dict[str, AgentProfile] = {}

#: امضای کارخانه‌ی سفارشی ساخت ایجنت
AgentBuilder = Callable[..., UniversalAgent]

_DEFAULT_SYSTEM_PROMPT = """You are Universal Agent Hub, an autonomous assistant with tool access.

Rules:
- Prefer a tool call over guessing when the answer depends on the machine, the filesystem or the web.
- One tool call at a time is fine; batch only independent calls.
- Never fabricate command output, file contents or URLs. If a tool fails, report the failure and try a safer alternative.
- Treat destructive operations (delete, overwrite, force-push, package removal) as last resorts and explain what you will do before doing it.
- Keep answers concise; use lists for multi-step results.
- When a task is finished, say what changed and how the user can verify it.
"""


def _default_profiles() -> dict[str, AgentProfile]:
    """پروفایل‌های پیش‌فرضِ همراه با بسته."""
    return {
        "generalist": AgentProfile(
            name="generalist",
            description="Full access: terminal, files, browser, search, system info.",
            system_prompt_extra="You may use every available tool, but always respect the safety guard.",
        ),
        "read_only": AgentProfile(
            name="read_only",
            description="Inspection only: no writes, no deletes, no shell writes.",
            disabled_tools=[
                "write_file",
                "delete_file",
                "move_file",
                "terminal_run",
                "browser_click",
                "browser_fill_form",
                "browser_screenshot",
                "browser_browse",
            ],
            system_prompt_extra="You must not modify anything. Read, summarize and propose commands instead of running them.",
            safety=SafetySettings(
                blocked_commands=[r"(?:^|\s)(?:rm|mv|cp|tee|truncate|sed\s+-i)\b"],
                max_input_bytes=1024 * 1024,
            ),
        ),
        "developer": AgentProfile(
            name="developer",
            description="Coding workflow: terminal, files, search (no browser automation).",
            enabled_tools=[
                "terminal_run",
                "read_file",
                "write_file",
                "list_directory",
                "delete_file",
                "move_file",
                "search_files",
                "web_search",
                "search_advanced",
                "os_info",
                "cpu_info",
                "memory_info",
                "disk_info",
                "network_info",
            ],
            temperature=0.2,
            system_prompt_extra="You are a senior engineer. Run commands, edit files and verify with tests.",
        ),
        "ops": AgentProfile(
            name="ops",
            description="System operations: terminal + system info, confirmation always on.",
            enabled_tools=[
                "terminal_run",
                "os_info",
                "cpu_info",
                "memory_info",
                "disk_info",
                "network_info",
                "list_directory",
                "read_file",
            ],
            temperature=0.1,
            system_prompt_extra="You are an SRE. Confirm the state of the machine before acting, and explain every command.",
        ),
    }


class AgentFactory:
    """ساخت ایجنت با پیکربندی و مجموعه‌ی ابزار قابل تنظیم."""

    _builders: ClassVar[dict[str, AgentBuilder]] = {}

    # ------------------------------------------------------------------
    # مدیریت پروفایل
    # ------------------------------------------------------------------
    @classmethod
    def ensure_loaded(cls) -> None:
        """بارگذاری پروفایل‌های پیش‌فرض (idempotent)."""
        if not PROFILE_REGISTRY:
            PROFILE_REGISTRY.update(_default_profiles())

    @classmethod
    def register(cls, profile: AgentProfile, *, replace: bool = True) -> str:
        """ثبت (یا بازنویسی) یک پروفایل.

        Args:
            profile: پروفایل ساخته‌شده.
            replace: اجازه‌ی بازنویسی نام تکراری.

        Returns:
            نام پروفایل.

        Raises:
            ValueError: پروفایل تکراری و ``replace=False``.
        """
        cls.ensure_loaded()
        if profile.name in PROFILE_REGISTRY and not replace:
            raise ValueError(f"profile '{profile.name}' already exists")
        PROFILE_REGISTRY[profile.name] = profile
        return profile.name

    @classmethod
    def register_builder(cls, name: str, builder: AgentBuilder) -> None:
        """ثبت کارخانه‌ی زیرکلاسی/سفارشی برای ساخت ایجنت."""
        cls._builders[name] = builder

    @classmethod
    def profiles(cls) -> dict[str, AgentProfile]:
        """فهرست پروفایل‌های موجود."""
        cls.ensure_loaded()
        return dict(PROFILE_REGISTRY)

    @classmethod
    def get_profile(cls, name: str) -> AgentProfile:
        """دریافت یک پروفایل طبق نام.

        Raises:
            KeyError: پروفایل ثبت نشده است.
        """
        cls.ensure_loaded()
        try:
            return PROFILE_REGISTRY[name]
        except KeyError as exc:
            known = ", ".join(sorted(PROFILE_REGISTRY))
            raise KeyError(f"unknown profile '{name}'. Available: {known}") from exc

    @classmethod
    def load_profiles_from_dir(cls, directory: str | Path, *, prefix: str = "") -> list[str]:
        """بارگذاری پروفایل‌ها از فایل‌های JSON/YAML یک پوشه.

        Args:
            directory: پوشه‌ی شامل فایل‌های پروفایل.
            prefix: پیشوند اختیاری برای نام پروفایل‌ها.

        Returns:
            نام‌های پروفایل بارگذاری‌شده.
        """
        loaded: list[str] = []
        base = Path(directory).expanduser()
        if not base.is_dir():
            return loaded
        for file_path in sorted(base.glob("*.*")):
            if file_path.suffix.lower() not in {".json", ".yaml", ".yml"}:
                continue
            try:
                profile = load_profile_file(file_path)
            except (ValueError, OSError) as exc:  # noqa: PERF203 - گزارش خطا و ادامه
                get_logger("core.agent_factory").warning("skipping profile %s: %s", file_path.name, exc)
                continue
            if prefix:
                profile = profile.model_copy(update={"name": f"{prefix}{profile.name}"})
            cls.register(profile)
            loaded.append(profile.name)
        return loaded

    # ------------------------------------------------------------------
    # ساخت ایجنت
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        profile: str | AgentProfile = "generalist",
        *,
        config: Config | None = None,
        tools: Iterable[str] | None = None,
        event_bus: EventBus | None = None,
        agent_class: type[UniversalAgent] | None = None,
        log: bool = True,
        **overrides: Any,
    ) -> UniversalAgent:
        """ساخت یک ایجنت آماده‌ی اجرا.

        Args:
            profile: نام پروفایل یا نمونه‌ی :class:`AgentProfile`.
            config: پیکربندی (پیش‌فرض: :func:`src.config.get_config`).
            tools: بازنویسی مستقیم فهرست نام ابزارها (اولویت بالاتر از پروفایل).
            event_bus: باس رویداد؛ اگر None باشد یک باس تازه با observer لاگ ساخته می‌شود.
            agent_class: کلاس ایجنت (برای subclass‌ها).
            log: افزودن observer لاگ به باس.
            **overrides: پارامترهای اضافی که به سازنده‌ی ایجنت داده می‌شود.

        Returns:
            نمونه‌ی :class:`UniversalAgent`.
        """
        logger = get_logger("core.agent_factory")
        resolved_profile = profile if isinstance(profile, AgentProfile) else cls.get_profile(str(profile))
        settings = config or get_config()
        ToolRegistry.set_default_config(settings)
        discover_tools()

        exposed = sorted(tools) if tools is not None else cls._tools_for_profile(resolved_profile)
        if exposed:
            unknown = [name for name in exposed if ToolRegistry.get(name, config=settings) is None]
            if unknown:
                logger.warning("profile '%s' references unknown tools: %s", resolved_profile.name, ", ".join(unknown))
            exposed = [name for name in exposed if name not in unknown]

        bus = event_bus or EventBus(persist_path=settings.event_log_path)
        if log:
            install_default_observers(bus)

        safety = cls._build_safety(settings, resolved_profile)
        builder = cls._builders.get(resolved_profile.name) or agent_class or UniversalAgent
        kwargs: dict[str, Any] = {
            "config": cls._apply_profile_settings(settings, resolved_profile),
            "tools": exposed,
            "event_bus": bus,
            "safety": safety,
            "profile": resolved_profile,
            "system_prompt": cls._system_prompt(resolved_profile),
        }
        if overrides:
            kwargs.update(overrides)
        agent = builder(**kwargs)
        logger.debug("built agent for profile '%s' with %d tool(s)", resolved_profile.name, len(exposed))
        return agent

    @classmethod
    def tools_for(cls, profile: str | AgentProfile) -> list[str]:
        """نام ابزارهای مؤثر یک پروفایل (رجیستری را کشف می‌کند).

        برای UI ها لازم است که بدانیم «این پروفایل دقیقاً چه چیزهایی را
        مجاز می‌گذارد»؛ بنابراین فهرست نهایی (بعد از disabled_tools و
        فیلتر دسته‌ها) برگردانده می‌شود.

        Args:
            profile: نام پروفایل یا نمونه‌ی آن.

        Returns:
            فهرست مرتب‌شده‌ی نام ابزارها.
        """
        cls.ensure_loaded()
        discover_tools()
        resolved = profile if isinstance(profile, AgentProfile) else cls.get_profile(profile)
        return sorted(cls._tools_for_profile(resolved))

    # ------------------------------------------------------------------
    # کمک‌متدها
    # ------------------------------------------------------------------
    @staticmethod
    def _tools_for_profile(profile: AgentProfile) -> list[str]:
        """تعیین نام ابزارهای فعال یک پروفایل از روی رجیستری."""
        available = ToolRegistry.names()
        if profile.enabled_tools:
            return [name for name in available if name in set(profile.enabled_tools)]
        if profile.categories:
            wanted = {category.value for category in profile.categories}
            selected: list[str] = []
            for name in available:
                tool = ToolRegistry.get(name)
                if tool is not None and tool.category.value in wanted:
                    selected.append(name)
            return selected
        return [name for name in available if profile.should_include(name)]

    @staticmethod
    def _apply_profile_settings(config: Config, profile: AgentProfile) -> Config:
        """اعمال تغییرات مدل/دمای پروفایل روی یک کپی از config."""
        updates: dict[str, Any] = {}
        if profile.model:
            updates["model_name"] = profile.model
            updates["active_model"] = profile.model
        if profile.temperature is not None:
            updates["temperature"] = profile.temperature
        if profile.max_tool_iterations is not None:
            updates["max_tool_iterations"] = profile.max_tool_iterations
        if profile.auto_confirm_all:
            updates["enable_confirmation"] = False
        if not updates:
            return config
        return config.model_copy(update=updates)

    @staticmethod
    def _build_safety(config: Config, profile: AgentProfile) -> Any:
        """ساخت :class:`SafetyGuard` با ادغام قوانین config و پروفایل.

        قوانین پروفایل *روی* قوانین config سوار می‌شوند تا یک پروفایل بتواند
        سخت‌گیرانه‌تر از تنظیمات عمومی باشد (نه شل‌تر).
        """
        from src.utils.safety import SafetyGuard

        guard = SafetyGuard.from_config(config)
        safety_settings: SafetySettings = profile.safety
        if safety_settings.blocked_commands or safety_settings.confirm_commands or safety_settings.read_only_paths:
            guard = SafetyGuard(
                policy=guard.policy,
                allowed_directories=config.allowed_directories,
                unrestricted_filesystem=config.unrestricted_filesystem,
                blocked_commands=list(safety_settings.blocked_commands),
                confirm_commands=list(safety_settings.confirm_commands),
                read_only_paths=list(safety_settings.read_only_paths),
                blocked_paths=list(safety_settings.blocked_paths),
                project_root=config.project_root,
            )
        return guard

    @staticmethod
    def _system_prompt(profile: AgentProfile) -> str:
        """ساخت system prompt نهایی از قالب پایه + توضیح پروفایل."""
        base = _DEFAULT_SYSTEM_PROMPT
        tool_hint = ""
        names = AgentFactory._tools_for_profile(profile)
        if names:
            tool_hint = "\nAvailable tools: " + ", ".join(f"`{name}`" for name in names) + "\n"
        extra = f"\nProfile: {profile.name}. {profile.description}\n{profile.system_prompt_extra}\n"
        return base + tool_hint + extra

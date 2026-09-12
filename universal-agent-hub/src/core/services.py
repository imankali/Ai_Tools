"""سیم‌کشی مرکزی سرویس‌های Agent-OS (composition root).

چرا یک ماژول جدا؟
-----------------
پیش از این، هر لایه (CLI، سرور، ایجنت) خودش memory و safety را می‌ساخت. با
افزودن هفت زیرسیستم جدید (scheduler، notifications، skills، MCP، subagents،
backup، audit، heartbeat) اگر هر مصرف‌کننده خودش آن‌ها را بسازد، سه نسخه‌ی
ناهمگام از یک وضعیت روی دیسک خواهیم داشت.

پس یک :class:`ServiceHub` داریم که:

* یک‌بار همه‌چیز را می‌سازد و به هم وصل می‌کند (bus ↔ notifications ↔ audit)،
* یک :class:`~src.core.base_tool.ToolContext` مشترک می‌دهد تا ابزارها به
  سرویس‌ها برسند،
* چرخه‌ی عمر (``start``/``stop``) را در یک جا مدیریت می‌کند،
* و برای ``/api/diagnostics`` یک خلاصه‌ی یکپارچه می‌دهد.

هیچ‌کدام از سرویس‌ها در ``__init__`` کار سنگین یا شبکه‌ای نمی‌کنند؛ اتصال MCP
و شروع حلقه‌ها فقط در :meth:`ServiceHub.start` اتفاق می‌افتد.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.config import Config
from src.core.audit import AuditLog
from src.core.backup import BackupManager
from src.core.heartbeat import Heartbeat, Watchdog
from src.core.mcp_client import MCPClient, load_mcp_config
from src.core.notifications import NotificationCenter
from src.core.routines import RoutineScheduler
from src.core.skills import SkillLibrary
from src.core.subagents import SubagentPool
from src.utils.injection import PromptInjectionScanner
from src.utils.leakscan import LeakDetector
from src.utils.logger import get_logger

__all__ = ["ServiceHub"]

logger = get_logger("core.services")

#: سازنده‌ی ایجنت برای روتین‌ها و ساب‌ایجنت‌ها: ``(prompt, profile) -> dict``
AgentRunner = Callable[[str, str], Any]


class ServiceHub:
    """مجموعه‌ی سرویس‌های Agent-OS با چرخه‌ی عمر مشترک.

    نمونه::

        hub = ServiceHub(config)
        await hub.start(agent_runner=my_runner)
        …
        await hub.stop()
    """

    def __init__(self, config: Config, *, bus: Any = None) -> None:
        """سرویس‌ها را می‌سازد (بدون اتصال شبکه و بدون شروع حلقه).

        Args:
            config: تنظیمات پروژه.
            bus: یک :class:`~src.core.event_bus.EventBus` مشترک (اختیاری).
        """
        self.config = config
        self.bus = bus
        self.audit: AuditLog | None = AuditLog(config.audit_path) if config.audit_enabled else None
        self.notifications: NotificationCenter | None = (
            NotificationCenter(
                config.notifications_path,
                max_records=config.notifications_max,
                webhook_url=config.notification_webhook,
            )
            if config.notifications_enabled
            else None
        )
        if self.notifications is not None and bus is not None:
            self.notifications.attach_bus(bus)
        self.injection_scanner = PromptInjectionScanner()
        self.leak_detector = LeakDetector.from_env() if config.leak_scan_enabled else LeakDetector({})
        self.skills = SkillLibrary(
            config.skill_directories,
            scanner=self.injection_scanner,
            max_context_chars=config.skills_context_chars,
        )
        self.skills.scan()
        self.scheduler = RoutineScheduler(
            config.routines_path,
            tick_seconds=config.routines_tick_seconds,
            max_history=config.routine_max_history,
            notify=self._notify,
            audit=self.audit,
        )
        self.subagents = SubagentPool(
            max_depth=config.subagent_max_depth,
            max_concurrent=config.subagent_max_concurrent,
            allowed_profiles=frozenset(config.subagent_profiles) if config.subagent_profiles else None,
            audit=self.audit,
            notify=self._notify,
        )
        self.backup = BackupManager(
            config.backup_path,
            {name: path for name, path in config.state_paths.items() if path.exists()},
            keep=config.backup_keep,
            # provider ⇒ فهرست منابع در هر بکاپ بازخوانی می‌شود؛ وگرنه فایلی که
            # بعد از شروع سرور ساخته می‌شود (مثلاً اولین memory.jsonl) جا می‌افتاد.
            source_provider=lambda: dict(config.state_paths),
        )
        self.watchdog = Watchdog(default_timeout=config.watchdog_timeout, on_timeout=self._on_watchdog_timeout)
        self.heartbeat = Heartbeat(
            interval_seconds=config.heartbeat_interval,
            notify=self._notify,
            disk_min_free_mb=config.heartbeat_disk_min_mb,
            bus=bus,
        )
        self.heartbeat.add_check("audit_chain", self._check_audit_chain)
        self.mcp_clients: list[MCPClient] = []
        #: نام ابزارهای MCP که واقعاً در ToolRegistry ثبت شدند (برای diagnostics).
        self.mcp_registered_tools: list[str] = []
        self._started = False

    # ------------------------------------------------------------------ helpers
    def _notify(self, kind: str, title: str, body: str) -> None:
        """پل اعلان برای سرویس‌هایی که مرکز اعلان‌ها را مستقیم نمی‌شناسند."""
        if self.notifications is None:
            return
        try:
            self.notifications.push(kind, title, body, source="service-hub")
        except Exception as exc:  # noqa: BLE001 - اعلان نباید سرویس را بشکند
            logger.debug("services: notify failed: %s", exc)

    def _on_watchdog_timeout(self, key: str, label: str) -> None:
        """وقتی watchdog موردی را آزاد می‌کند، اعلان و ممیزی ثبت می‌شود."""
        self._notify("watchdog", f"run '{label}' exceeded its deadline", f"released by watchdog (key={key})")
        if self.audit is not None:
            self.audit.record("watchdog", "watchdog.released", label, "failed", key=key)

    async def _check_audit_chain(self) -> Any:
        """سلامت زنجیره‌ی ممیزی به‌عنوان یک بررسی heartbeat."""
        from src.core.heartbeat import CheckResult

        if self.audit is None:
            return CheckResult("audit_chain", ok=True, message="audit disabled", severity="info")
        verdict = self.audit.verify_chain()
        if verdict.ok:
            return CheckResult(
                "audit_chain", ok=True, message=f"{verdict.entries} entries verified", value=verdict.entries
            )
        if self.notifications is not None:
            self.notifications.push(
                "security",
                "audit chain is broken",
                verdict.reason,
                severity="critical",
                source="heartbeat",
            )
        return CheckResult("audit_chain", ok=False, severity="error", message=verdict.reason, value=verdict.broken_at)

    # ------------------------------------------------------------------ lifecycle
    async def connect_mcp(self) -> list[str]:
        """سرورهای MCP را از فایل پیکربندی وصل می‌کند.

        Returns:
            نام سرورهایی که موفق وصل شدند.
        """
        if not self.config.mcp_enabled:
            return []
        configs = load_mcp_config(self.config.mcp_config_path)
        connected: list[str] = []
        for server_config in configs:
            if not server_config.enabled:
                continue
            client = MCPClient(server_config, scanner=self.injection_scanner)
            try:
                await client.connect()
            except Exception as exc:  # noqa: BLE001 - یک سرور خراب نباید بقیه را بگیرد
                logger.warning("services: MCP server '%s' failed: %s", server_config.name, exc)
                self._notify("mcp", f"MCP server '{server_config.name}' failed", str(exc)[:300])
                continue
            self.mcp_clients.append(client)
            connected.append(server_config.name)
            if self.audit is not None:
                self.audit.record("mcp", "mcp.connected", server_config.name, "allowed", tools=len(client.tools))

        # ابزارهای راه دور را به ابزار درجه‌یک تبدیل می‌کند تا مدل بتواند
        # صداشان بزند. این کار *بعد از* همه‌ی اتصال‌ها انجام می‌شود تا registry
        # چند بار دست‌نخورد. ابزارهایی که توضیحشان آلوده است ثبت نمی‌شوند.
        if connected:
            from src.tools.mcp import register_mcp_tools

            try:
                self.mcp_registered_tools = await register_mcp_tools(self.mcp_clients, scanner=self.injection_scanner)
            except Exception as exc:  # noqa: BLE001 - نبود ابزار راه دور نباید hub را بخواباند
                logger.warning("services: MCP tool registration failed: %s", exc)
                self.mcp_registered_tools = []
        return connected

    def mcp_tools(self) -> list[dict[str, Any]]:
        """همه‌ی ابزارهای راه دور MCP (برای نمایش و ثبت)."""
        return [tool.as_dict() for client in self.mcp_clients for tool in client.tools.values()]

    async def start(self, *, agent_runner: AgentRunner | None = None, agent_factory: Any = None) -> dict[str, Any]:
        """حلقه‌ها را راه‌اندازی و اتصال‌ها را برقرار می‌کند.

        Args:
            agent_runner: ``async (prompt, profile) -> dict`` برای روتین‌ها.
            agent_factory: ``(profile) -> agent`` برای ساب‌ایجنت‌ها.

        Returns:
            خلاصه‌ی آنچه شروع شد.
        """
        if self._started:
            return {"already_started": True}
        if agent_runner is not None:
            self.scheduler.executor = agent_runner
        if agent_factory is not None:
            self.subagents.factory = agent_factory
        mcp_connected = await self.connect_mcp()
        scheduler_started = False
        if self.config.scheduler_enabled and self.scheduler.executor is not None:
            scheduler_started = await self.scheduler.start()
        heartbeat_started = await self.heartbeat.start() if self.config.heartbeat_enabled else False
        await self.watchdog.start()
        self._started = True
        return {
            "already_started": False,
            "mcp_servers": mcp_connected,
            "mcp_tools": len(self.mcp_tools()),
            "scheduler": scheduler_started,
            "heartbeat": heartbeat_started,
            "skills": len(self.skills),
        }

    async def stop(self) -> None:
        """حلقه‌ها را متوقف و اتصال‌ها را می‌بندد (idempotent)."""
        if not self._started:
            return
        await self.scheduler.stop()
        await self.heartbeat.stop()
        await self.watchdog.stop()
        for client in self.mcp_clients:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("services: MCP close failed: %s", exc)
        if self.mcp_registered_tools:
            from src.tools.mcp import unregister_mcp_tools

            try:
                await unregister_mcp_tools(self.mcp_clients)
            except Exception as exc:  # noqa: BLE001
                logger.debug("services: MCP unregister failed: %s", exc)
        self.mcp_registered_tools.clear()
        self.mcp_clients.clear()
        if self.notifications is not None and self.bus is not None:
            self.notifications.detach_bus(self.bus)
        self._started = False

    @property
    def started(self) -> bool:
        """آیا hub شروع شده؟"""
        return self._started

    # ------------------------------------------------------------------ reporting
    def diagnostics(self) -> dict[str, Any]:
        """خلاصه‌ی یکپارچه برای ``/api/diagnostics`` و ``--doctor``."""
        audit_verdict = self.audit.verify_chain().as_dict() if self.audit is not None else {"ok": True, "entries": 0}
        return {
            "started": self._started,
            "audit": {**(self.audit.stats() if self.audit else {}), "chain": audit_verdict},
            "notifications": self.notifications.stats() if self.notifications else {"total": 0, "unread": 0},
            "scheduler": self.scheduler.describe(),
            "heartbeat": self.heartbeat.describe(),
            "watchdog": self.watchdog.describe(),
            "skills": {
                "count": len(self.skills),
                "enabled": len(self.skills.enabled()),
                "errors": len(self.skills.describe()["errors"]),
            },
            "subagents": self.subagents.describe(),
            "backup": self.backup.describe(),
            "mcp": {
                "servers": [client.describe() for client in self.mcp_clients],
                # چند ابزار راه دور *واقعاً* به ابزار درجه‌یک تبدیل شد؟ اگر این
                # عدد از مجموع tools سرورها کمتر باشد، یعنی چیزی reject شده —
                # و این تنها جایی است که آن سکوت دیده می‌شود.
                "registered_tools": list(self.mcp_registered_tools),
            },
            "security": {
                "injection_rules": self.injection_scanner.describe()["active_rules"],
                "injection_min_severity": self.config.injection_min_severity,
                "leak_sources": len(self.leak_detector.sources),
                "http_allowlist": list(self.config.http_allowlist),
            },
            "state_files": {name: str(path) for name, path in self.config.state_paths.items()},
        }

    def tool_context_services(self) -> dict[str, Any]:
        """kwargs آماده برای :meth:`ToolRegistry.build_context`."""
        return {
            "skills": self.skills,
            "subagents": self.subagents,
            "scheduler": self.scheduler,
            "notifications": self.notifications,
            "audit": self.audit,
            "mcp": self.mcp_clients or None,
        }

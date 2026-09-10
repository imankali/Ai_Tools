"""ابزارهای اطلاعات سیستم (System info).

این ابزارها «فقط‌خواندنی»‌اند و همیشه بدون تأیید اجرا می‌شوند. اگر بسته‌ی
``psutil`` نصب باشد از آن استفاده می‌شود (دقیق‌تر و کامل‌تر)؛ در غیر این‌صورت
به داده‌های استاندارد Python/``/proc`` برمی‌گردیم تا پروژه بدون وابستگی اختیاری
هم کار کند.

نکته‌ی حریم خصوصی: مقادیر «مهمان‌نازک» مثل IP عمومی فقط با ``public_ip=true``
درخواست می‌شوند و پیش‌فرض خاموش‌اند.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import platform
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.helpers import format_size, run_in_thread
from src.utils.logger import get_logger
from src.utils.safety import SafetyDecision
from src.utils.validators import ValidationError, coerce_bool

__all__ = [
    "CpuInfoTool",
    "DiskInfoTool",
    "MemoryInfoTool",
    "NetworkInfoTool",
    "OsInfoTool",
    "collect_cpu_info",
    "collect_disk_info",
    "collect_memory_info",
    "collect_network_info",
    "collect_os_info",
]


def _psutil() -> Any:
    """import امن psutil (None اگر نصب نباشد)."""
    try:
        import psutil

        return psutil
    except ImportError:
        return None


# ----------------------------------------------------------------------
# جمع‌آوری داده (توابع خالص و قابل تست)
# ----------------------------------------------------------------------
def collect_os_info() -> dict[str, Any]:
    """اطلاعات سیستم‌عامل و محیط اجرا.

    Returns:
        dict شامل سیستم‌عامل، نسخه‌ی kernel، hostname، user، shell، python و uptime.
    """
    uname = platform.uname()
    info: dict[str, Any] = {
        "system": uname.system,
        "release": uname.release,
        "version": (uname.version or "")[:120],
        "machine": uname.machine,
        "cpu_architecture": platform.machine(),
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "hostname": socket.gethostname(),
        "username": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "home": str(Path.home()),
        "cwd": str(Path.cwd()),
        "shell": os.environ.get("SHELL", ""),
        "filesystem_encoding": sys.getfilesystemencoding(),
        "timezone": time.strftime("%Z") or "unknown",
        "boot_time_iso": None,
        "uptime_seconds": None,
    }
    psutil = _psutil()
    if psutil is not None:
        try:
            boot = psutil.boot_time()
            info["boot_time_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot))
            info["uptime_seconds"] = int(time.time() - boot)
        except (OSError, ValueError):  # pragma: no cover
            pass
    else:  # fallback ساده برای لینوکس
        try:
            raw = Path("/proc/uptime").read_text(encoding="utf-8").split()
            info["uptime_seconds"] = int(float(raw[0]))
        except (OSError, ValueError, IndexError):
            pass
    return info


def collect_cpu_info() -> dict[str, Any]:
    """اطلاعات CPU: هسته‌ها، فرکانس، بار سیستم و درصد استفاده."""
    psutil = _psutil()
    info: dict[str, Any] = {
        "physical_cores": None,
        "logical_cores": os.cpu_count(),
        "brand": platform.processor() or None,
        "frequency_mhz": None,
        "percent": None,
        "load_average": None,
    }
    getloadavg = getattr(os, "getloadavg", None)
    if callable(getloadavg):
        try:
            info["load_average"] = [round(item, 2) for item in getloadavg()]
        except OSError:  # pragma: no cover - بعضی کانتینرها
            info["load_average"] = None
    if psutil is None:
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    info["brand"] = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        return info
    try:
        info["physical_cores"] = psutil.cpu_count(logical=False)
        info["percent"] = psutil.cpu_percent(interval=0.25)
        frequency = psutil.cpu_freq()
        if frequency:
            info["frequency_mhz"] = round(frequency.current, 1)
        info["per_cpu_percent"] = [round(value, 1) for value in psutil.cpu_percent(interval=0.1, percpu=True)][:16]
        with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover - پلتفرم‌های مختلف
            info["categorization"] = {
                "user": round(psutil.cpu_times().user, 1),
                "system": round(psutil.cpu_times().system, 1),
                "idle": round(psutil.cpu_times().idle, 1),
            }
    except (OSError, RuntimeError, AttributeError) as exc:  # pragma: no cover
        info["error"] = f"psutil failed: {exc}"
    return info


def collect_memory_info() -> dict[str, Any]:
    """اطلاعات RAM و swap به بایت و درصد."""
    psutil = _psutil()
    if psutil is not None:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "total_bytes": virtual.total,
            "available_bytes": virtual.available,
            "used_bytes": virtual.total - virtual.available,
            "percent": round(virtual.percent, 1),
            "total": format_size(virtual.total),
            "available": format_size(virtual.available),
            "used": format_size(virtual.total - virtual.available),
            "swap_total": format_size(swap.total),
            "swap_used": format_size(swap.used),
            "swap_percent": round(swap.percent, 1),
            "buffers_bytes": getattr(virtual, "buffers", 0),
            "cached_bytes": getattr(virtual, "cached", 0),
        }
    # fallback: /proc/meminfo
    data: dict[str, Any] = {"source": "/proc/meminfo"}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            number = value.strip().split()[0] if value.strip() else "0"
            data[key.strip().lower()] = int(number) * 1024
        total = data.get("memtotal", 0)
        available = data.get("memavailable", data.get("memfree", 0))
        data.update(
            {
                "total_bytes": total,
                "available_bytes": available,
                "used_bytes": max(0, total - available),
                "percent": round(100 * (total - available) / total, 1) if total else 0.0,
                "total": format_size(total),
                "available": format_size(available),
                "used": format_size(max(0, total - available)),
            }
        )
    except (OSError, ValueError, IndexError):  # pragma: no cover - پلتفرم غیرلینوکسی
        data.update(
            {
                "total_bytes": 0,
                "available_bytes": 0,
                "used_bytes": 0,
                "percent": 0.0,
                "total": "n/a",
                "available": "n/a",
                "used": "n/a",
            }
        )
    return data


def collect_disk_info(path: str = ".", *, top_entries: int = 0) -> dict[str, Any]:
    """اطلاعات دیسک: ظرفیت، فضای آزاد و (اختیاراً) بزرگ‌ترین پوشه‌ها.

    Args:
        path: مسیری از volume مورد نظر.
        top_entries: اگر >0 باشد، N ورودی برتر از نظر حجم اضافه می‌شود.

    Returns:
        dict با total/used/free/percent و فهرست volumes (در لینوکس).
    """
    psutil = _psutil()
    target = Path(path or ".").expanduser()
    if psutil is not None:
        usage = psutil.disk_usage(str(target))
        info: dict[str, Any] = {
            "path": str(target),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": round(usage.percent, 1),
            "total": format_size(usage.total),
            "used": format_size(usage.used),
            "free": format_size(usage.free),
            "read_only": os.access(str(target), os.W_OK) is False,
        }
        try:
            info["partitions"] = [
                {"device": item.device, "mountpoint": item.mountpoint, "fstype": item.fstype}
                for item in psutil.disk_partitions(all=False)
            ][:20]
            counters = psutil.disk_io_counters()
            if counters:
                info["io"] = {
                    "read_bytes": counters.read_bytes,
                    "write_bytes": counters.write_bytes,
                    "read_count": counters.read_count,
                    "write_count": counters.write_count,
                }
        except (OSError, AttributeError, RuntimeError):  # pragma: no cover
            pass
    else:
        try:
            usage = shutil.disk_usage(str(target))
            info = {
                "path": str(target),
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": round(100 * usage.used / usage.total, 1) if usage.total else 0.0,
                "total": format_size(usage.total),
                "used": format_size(usage.used),
                "free": format_size(usage.free),
                "read_only": os.access(str(target), os.W_OK) is False,
            }
        except OSError as exc:
            raise ValidationError(f"cannot read disk usage for '{target}': {exc}") from exc
    if top_entries > 0:
        info["largest_entries"] = _largest_entries(target, top_entries)
    return info


def _largest_entries(root: Path, limit: int) -> list[dict[str, Any]]:
    """اندازه‌ی تقریبی بزرگ‌ترین ورودی‌های یک پوشه (بدون پیمایش نامحدود)."""
    sizes: dict[str, int] = {}
    visited = 0
    for dirpath, _dirnames, filenames in os.walk(root, topdown=True):
        visited += len(filenames)
        base = Path(dirpath)
        key = str(base.relative_to(root)) if base != root else "."
        try:
            total = sum((base / name).stat().st_size for name in filenames if not (base / name).is_symlink())
        except OSError:
            total = 0
        sizes[key] = sizes.get(key, 0) + total
        if visited > 20_000:  # محافظ در برابر درخت‌های غول‌پیکر
            break
    ordered = sorted(sizes.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [{"path": key, "size": format_size(value), "size_bytes": value} for key, value in ordered if value > 0]


def collect_network_info(config: Any = None, *, public_ip: bool = False, timeout: float = 5.0) -> dict[str, Any]:
    """اطلاعات شبکه: hostname، IP های محلی، DNS و (اختیاراً) IP عمومی.

    Args:
        config: برای user_agent (هنگام درخواست IP عمومی).
        public_ip: آیا IP عمومی از ``api.ipify.org`` خوانده شود؟
        timeout: سقف زمانی درخواست IP عمومی.

    Returns:
        dict با keys: hostname, local_ips, primary_ip, dns, public_ip, interfaces.
    """
    info: dict[str, Any] = {"hostname": socket.gethostname()}
    ips: list[str] = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            address = str(item[4][0])
            if address not in ips and ":" not in address:
                ips.append(address)
    except socket.gaierror:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.5)
            probe.connect(("8.8.8.8", 80))
            primary = str(probe.getsockname()[0])
            if primary not in ips:
                ips.insert(0, primary)
    except OSError:
        primary = ""
    info["local_ips"] = ips
    info["primary_ip"] = primary or (ips[0] if ips else "unknown")
    try:
        info["dns_servers"] = _read_resolv_conf()
    except OSError:
        info["dns_servers"] = []
    psutil = _psutil()
    if psutil is not None:
        try:
            stats = psutil.net_if_stats()
            info["interfaces"] = [
                {
                    "name": name,
                    "is_up": bool(value.isup),
                    "speed_mbps": int(value.speed),
                    "mtu": int(value.mtu),
                }
                for name, value in list(stats.items())[:12]
            ]
            counters = psutil.net_io_counters()
            if counters:
                info["traffic"] = {
                    "bytes_sent": counters.bytes_sent,
                    "bytes_recv": counters.bytes_recv,
                    "packets_sent": counters.packets_sent,
                    "packets_recv": counters.packets_recv,
                }
        except (OSError, AttributeError, RuntimeError):  # pragma: no cover
            info["interfaces"] = []
    if public_ip:
        info["public_ip"] = _fetch_public_ip(config=config, timeout=timeout)
    return info


def _read_resolv_conf() -> list[str]:
    """خواندن DNS های فعال از ``/etc/resolv.conf`` (لینوکس/macOS)."""
    path = Path("/etc/resolv.conf")
    if not path.is_file():
        return []
    servers: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("nameserver"):
            parts = stripped.split()
            if len(parts) > 1 and parts[1] not in servers:
                servers.append(parts[1])
    return servers[:4]


def _fetch_public_ip(config: Any = None, *, timeout: float = 5.0) -> str:
    """دریافت IP عمومی به‌صورت best-effort (حریم خصوصی: پیش‌فرض استفاده نمی‌شود).

    فقط از بیرون حلقه‌ی ایوا قابل استفاده است؛ در ابزارها مستقیماً
    :func:`fetch_public_ip_raw` را await کنید.
    """
    try:  # pragma: no cover - مسیر شبکه‌ای
        text, _info = asyncio.run(fetch_public_ip_raw(config=config, timeout=timeout))
        return text.strip()
    except Exception:  # noqa: BLE001 - اطلاعات شبکه نباید اجرا را بشکند
        return "unavailable"


async def fetch_public_ip_raw(config: Any = None, *, timeout: float = 5.0) -> tuple[str, dict[str, Any]]:
    """فراخوانی واقعی IP عمومی (قابل await برای استفاده در ابزار)."""
    from src.tools.browser import fetch_url

    return await fetch_url("https://api.ipify.org", config=config, timeout=timeout, max_bytes=64)


# ----------------------------------------------------------------------
# ابزارها
# ----------------------------------------------------------------------
class SystemInfoToolBase(BaseTool):
    """پایه‌ی ابزارهای اطلاعات سیستم (فقط‌خواندنی، بدون تأیید)."""

    category: ClassVar[ToolCategory] = ToolCategory.SYSTEM
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    requires_confirmation: ClassVar[bool] = False
    #: تابع جمع‌آوری داده (در execute صدا زده می‌شود)
    collector: ClassVar[str] = ""

    async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
        """همیشه مجاز: این ابزارها فقط می‌خوانند."""
        return SafetyDecision(
            allowed=True, risk=RiskLevel.SAFE, requires_confirmation=False, reasons=["read-only system metrics"]
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای پیش‌فرض (بدون پارامتر)."""
        return self.function_schema(name=self.name, description=self.description, required=(), properties={})

    async def execute(self, **kwargs: Any) -> ToolResult:
        """فراخوانی collector مربوطه در thread جدا (با فیلتر پارامترهای ناشناخته)."""
        collector = getattr(sys.modules[__name__], self.collector)
        kwargs.pop("context", None)
        allowed = set(inspect.signature(collector).parameters)
        dropped = sorted(set(kwargs) - allowed)
        if dropped:
            get_logger("tools.system_info").debug(
                "%s: ignoring unknown parameter(s) %s", self.name, ", ".join(dropped)
            )
            kwargs = {key: value for key, value in kwargs.items() if key in allowed}
        try:
            data = await run_in_thread(collector, **kwargs)
        except ValidationError as exc:
            return ToolResult.fail(str(exc), tool=self.name, error_code="invalid_input")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.fail(
                f"could not collect info: {type(exc).__name__}: {exc}", tool=self.name, error_code="collector_failed"
            )
        return ToolResult.ok(data, tool=self.name, metadata={"keys": len(data) if isinstance(data, dict) else 0})


@register_tool
class OsInfoTool(SystemInfoToolBase):
    """اطلاعات سیستم‌عامل و محیط."""

    name: ClassVar[str] = "os_info"
    description: ClassVar[str] = (
        "Return OS and environment facts: platform, kernel, hostname, user, python version, cwd, "
        "shell and uptime. Use it first to orient yourself on a new machine."
    )
    collector: ClassVar[str] = "collect_os_info"


@register_tool
class CpuInfoTool(SystemInfoToolBase):
    """اطلاعات پردازنده."""

    name: ClassVar[str] = "cpu_info"
    description: ClassVar[str] = (
        "Return CPU details: logical/physical cores, model, frequency, current load percentage and "
        "load averages. Useful before starting heavy parallel work."
    )
    collector: ClassVar[str] = "collect_cpu_info"


@register_tool
class MemoryInfoTool(SystemInfoToolBase):
    """اطلاعات حافظه."""

    name: ClassVar[str] = "memory_info"
    description: ClassVar[str] = (
        "Return RAM and swap usage (total, used, available, percent). Falls back to /proc/meminfo "
        "when psutil is not installed."
    )
    collector: ClassVar[str] = "collect_memory_info"


@register_tool
class DiskInfoTool(SystemInfoToolBase):
    """اطلاعات دیسک."""

    name: ClassVar[str] = "disk_info"
    description: ClassVar[str] = (
        "Return disk capacity, usage and free space for a path, plus mounted partitions. Pass "
        "top_entries to also list the largest sub-directories of the given path."
    )
    collector: ClassVar[str] = "collect_disk_info"
    optional_parameters: ClassVar[tuple[str, ...]] = ("path", "top_entries")

    def validate_input(self, **kwargs: Any) -> bool:
        """اعتبارسنجی آرگومان‌های اختیاری."""
        from src.utils.validators import bounded_int

        super().validate_input(**kwargs)
        bounded_int(kwargs.get("top_entries"), name="top_entries", minimum=0, maximum=25, default=0)
        return True

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای ابزار دیسک."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=(),
            properties={
                "path": {
                    "type": "string",
                    "description": "Any path on the volume you want info about (defaults to the project root).",
                },
                "top_entries": {
                    "type": "integer",
                    "description": "Also report the N largest sub-directories (0 disables, max 25).",
                    "minimum": 0,
                    "maximum": 25,
                },
            },
        )

    async def execute(  # type: ignore[override]
        self, path: str = ".", top_entries: int = 0, context: ToolContext | None = None
    ) -> ToolResult:
        """اجرای collect_disk_info با پارامترها."""
        config = context.config if context and context.config else self.config
        raw_path = str(path or ".")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(str(getattr(config, "project_root", Path.cwd()))) / candidate
        try:
            data = await run_in_thread(
                collect_disk_info, str(candidate.resolve(strict=False)), top_entries=int(top_entries or 0)
            )
        except ValidationError as exc:
            return ToolResult.fail(str(exc), tool=self.name, error_code="invalid_input")
        except OSError as exc:
            return ToolResult.fail(f"disk info failed: {exc}", tool=self.name, error_code="disk_error")
        return ToolResult.ok(data, tool=self.name)


@register_tool
class NetworkInfoTool(SystemInfoToolBase):
    """اطلاعات شبکه."""

    name: ClassVar[str] = "network_info"
    description: ClassVar[str] = (
        "Return hostname, local IP addresses, DNS servers, interface state and traffic counters. "
        "Set public_ip=true only when the user explicitly asks for the outward-facing IP."
    )
    collector: ClassVar[str] = "collect_network_info"
    optional_parameters: ClassVar[tuple[str, ...]] = ("public_ip",)
    sensitive_parameters: ClassVar[tuple[str, ...]] = ()

    def validate_input(self, **kwargs: Any) -> bool:
        """تبدیل public_ip به بولین."""
        super().validate_input(**kwargs)
        kwargs["public_ip"] = coerce_bool(kwargs.get("public_ip"), default=False)
        return True

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای ابزار شبکه."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=(),
            properties={
                "public_ip": {
                    "type": "boolean",
                    "description": "Also query api.ipify.org for the public IP (off by default, privacy).",
                    "default": False,
                }
            },
        )

    async def execute(self, public_ip: bool = False, context: ToolContext | None = None) -> ToolResult:  # type: ignore[override]
        """اجرای collect_network_info (IP عمومی فقط در صورت درخواست)."""
        config = context.config if context and context.config else self.config
        flag = coerce_bool(public_ip, default=False)
        if flag:
            try:
                text, _info = await fetch_public_ip_raw(config=config)
                public_value = text.strip()
            except Exception as exc:  # noqa: BLE001
                public_value = f"unavailable ({type(exc).__name__})"
            data = await run_in_thread(collect_network_info, config=config, public_ip=False)
            data["public_ip"] = public_value
        else:
            data = await run_in_thread(collect_network_info, config=config, public_ip=False)
        return ToolResult.ok(data, tool=self.name)

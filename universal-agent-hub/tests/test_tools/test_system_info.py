"""تست ابزارهای اطلاعات سیستم (:mod:`src.tools.system_info`).

این ابزارها فقط‌خواندنی‌اند، پس تست روی داده‌ی واقعی ماشین اجرا می‌شود و فقط
سطح «شکل داده» و رفتار fallback (نبود psutil / نبود شبکه) بررسی می‌گردد.
"""

from __future__ import annotations

import platform
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from src.config import Config
from src.core.base_tool import ToolContext
from src.tools import system_info as si
from src.tools.system_info import (
    CpuInfoTool,
    DiskInfoTool,
    MemoryInfoTool,
    NetworkInfoTool,
    OsInfoTool,
    SystemInfoToolBase,
    collect_cpu_info,
    collect_disk_info,
    collect_memory_info,
    collect_network_info,
    collect_os_info,
)

ALL_TOOLS = (OsInfoTool, CpuInfoTool, MemoryInfoTool, DiskInfoTool, NetworkInfoTool)


# ---------------------------------------------------------------------------
# جمع‌کننده‌ها (توابع خالص)
# ---------------------------------------------------------------------------
def test_collect_os_info_reports_real_machine() -> None:
    """اطلاعات OS با platform/os یکسان است."""
    info = collect_os_info()
    assert info["system"] == platform.system()
    assert info["release"] == platform.release()
    assert info["python_version"] == platform.python_version()
    assert Path(info["cwd"]).is_dir()
    assert info["uptime_seconds"] >= 0
    assert info["hostname"] == socket.gethostname()
    assert isinstance(info["shell"], str) and isinstance(info["username"], str)
    assert info["machine"] == platform.machine()


def test_collect_cpu_info_bounds() -> None:
    """هسته‌ها و درصدها در بازه‌ی منطقی‌اند."""
    info = collect_cpu_info()
    assert info["logical_cores"] >= 1
    assert 0.0 <= float(info["percent"]) <= 100.0
    per_cpu = info["per_cpu_percent"]
    assert isinstance(per_cpu, list) and 0 < len(per_cpu) <= 16 and len(per_cpu) <= info["logical_cores"]
    assert all(0.0 <= float(value) <= 100.0 for value in per_cpu)
    assert info["load_average"] is None or len(info["load_average"]) == 3
    assert info["physical_cores"] is None or 1 <= info["physical_cores"] <= info["logical_cores"]
    assert set(info["categorization"]) == {"user", "system", "idle"}


def test_collect_memory_info_shape() -> None:
    """حافظه: مقدار خام بایتی + رشته‌ی خوانا."""
    info = collect_memory_info()
    assert info["total_bytes"] > 0
    assert 0.0 <= float(info["percent"]) <= 100.0
    assert 0.0 <= float(info["swap_percent"]) <= 100.0
    for key in ("total", "used", "available"):
        assert info[key].endswith("B") or "iB" in info[key]
    assert int(info["available_bytes"]) <= int(info["total_bytes"])


def test_memory_falls_back_to_proc_when_psutil_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """بدون psutil از /proc/meminfo خوانده می‌شود (وابستگی اختیاری است)."""
    monkeypatch.setattr(si, "_psutil", lambda: None)
    info = collect_memory_info()
    assert info["total_bytes"] > 0 and info["percent"] >= 0
    assert isinstance(info["available"], str)


def test_disk_info_usage_and_partitions() -> None:
    """ظرفیت/استفاده‌ی دیسک و فهرست پارتیشن‌ها."""
    info = collect_disk_info(".", top_entries=0)
    assert info["total_bytes"] > 0 and info["free_bytes"] > 0
    assert info["used_bytes"] + info["free_bytes"] <= info["total_bytes"]
    assert info["read_only"] is False
    assert 0.0 <= float(info["percent"]) <= 100.0
    assert isinstance(info["partitions"], list)
    if info["partitions"]:
        assert info["partitions"][0]["mountpoint"] and "fstype" in info["partitions"][0]
    assert "largest_entries" not in info  # top_entries=0 یعنی نخواسته شده


def test_disk_top_entries_are_sorted_and_limited(tmp_path: Path) -> None:
    """بزرگ‌ترین پوشه‌ها: بیشترین حجم اول و سقف تعداد."""
    for name, size in (("small", 100), ("big", 5000), ("medium", 900)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "file.bin").write_bytes(b"x" * size)
    info = collect_disk_info(str(tmp_path), top_entries=2)
    entries = info["largest_entries"]
    assert len(entries) == 2
    assert [Path(item["path"]).name for item in entries] == ["big", "medium"]
    assert entries[0]["size_bytes"] >= entries[1]["size_bytes"]
    assert entries[0]["size"].endswith("B")


def test_disk_missing_path_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """مسیر ناموجود: با psutil خطای OS، بدون psutil ValidationError."""
    with pytest.raises(OSError):
        collect_disk_info("/definitely-not-here-xyz")
    monkeypatch.setattr(si, "_psutil", lambda: None)
    with pytest.raises(ValueError, match="cannot read disk usage"):
        collect_disk_info("/definitely-not-here-xyz")


def test_network_info_local_data() -> None:
    """IP محلی، DNS و اینترفیس‌ها (بدون تماس با بیرون)."""
    info = collect_network_info()
    assert info["hostname"] == socket.gethostname()
    assert isinstance(info["local_ips"], list)
    assert info["primary_ip"]
    assert isinstance(info["dns_servers"], list)
    assert "public_ip" not in info  # پیش‌فرض حریم خصوصی: درخواست بیرونی نمی‌شود
    for item in info["interfaces"]:
        assert set(item) == {"name", "is_up", "speed_mbps", "mtu"}
    assert info["traffic"]["bytes_sent"] >= 0


def test_network_public_ip_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """خطای گرفتن IP عمومی نباید جمع‌آوری شبکه را بشکند."""
    monkeypatch.setattr(si, "_fetch_public_ip", lambda **kwargs: "unavailable")
    info = collect_network_info(public_ip=True)
    assert info["public_ip"] == "unavailable"

    monkeypatch.setattr(si, "_fetch_public_ip", lambda **kwargs: "203.0.113.7")
    assert collect_network_info(public_ip=True)["public_ip"] == "203.0.113.7"


def test_read_resolv_conf_parses_nameservers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """تجزیه‌ی resolv.conf: حذف کامنت، حذف تکراری و سقف ۴ سرویس."""
    target = tmp_path / "resolv.conf"
    target.write_text(
        "# c\nnameserver 1.1.1.1\nnameserver 1.1.1.1\nnameserver 9.9.9.9\nnameserver 8.8.8.8\nsearch local\n",
        encoding="utf-8",
    )

    class FakePath:
        """جای‌نمای Path که فقط فایل نمونه را می‌شناسد."""

        def __init__(self, value: Any) -> None:
            self.text = str(value)

        def is_file(self) -> bool:
            return target.exists()

        def read_text(self, **kwargs: Any) -> str:
            return target.read_text(encoding="utf-8")

    monkeypatch.setattr(si, "Path", FakePath)
    assert si._read_resolv_conf() == ["1.1.1.1", "9.9.9.9", "8.8.8.8"]
    target.unlink()
    assert si._read_resolv_conf() == []


# ---------------------------------------------------------------------------
# ابزارها (از مسیر کامل BaseTool.run)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool_class", ALL_TOOLS, ids=lambda value: value.name)
async def test_system_tools_run_without_input(config: Config, tool_class: Any) -> None:
    """هر پنج ابزار بدون پارامتر کار می‌کنند و dict برمی‌گردانند."""
    tool = tool_class(config)
    result = await tool.run({}, context=ToolContext(config=config))
    assert result.success, result.error
    assert isinstance(result.data, dict) and result.data
    assert result.tool == tool.name
    text = result.to_llm_string()
    assert text.startswith("{") and tool.name not in text  # خطا نبود


@pytest.mark.parametrize("tool_class", ALL_TOOLS, ids=lambda value: value.name)
def test_system_tools_are_read_only_and_never_confirm(config: Config, tool_class: Any) -> None:
    """ریسک SAFE، بدون تأیید، دسته‌ی system و اسکیمای بدون required."""
    tool = tool_class(config)
    assert isinstance(tool, SystemInfoToolBase)
    assert tool.risk_level.value == "safe" and tool.requires_confirmation is False
    assert tool.category.value == "system"
    schema = tool.get_schema()["function"]
    assert schema["parameters"]["required"] == []
    for spec in schema["parameters"]["properties"].values():
        assert spec.get("description")
    info = tool.to_info()
    assert info.requires_confirmation is False and info.risk_level.value == "safe"


async def test_safety_check_allows_even_with_strict_policy(config: Config) -> None:
    """خط‌مشی سخت هم نباید خواندن متریک‌ها را ببندد."""
    strict = config.model_copy(update={"dangerous_command_policy": "deny"})
    tool = OsInfoTool(strict)
    decision = await tool.safety_check({}, ToolContext(config=strict, safety=tool.safety_guard()))
    assert decision.allowed is True and decision.requires_confirmation is False
    assert "read-only" in decision.reasons[0]


async def test_unknown_collector_kwargs_are_ignored_not_fatal(config: Config) -> None:
    """پارامتر اضافه از مدل نباید collector را بشکند (regression)."""
    import json

    direct = await OsInfoTool(config).execute(context=None, extra_junk=1)
    assert direct.success and json.loads(direct.to_llm_string())["data"]["system"] == platform.system()
    # ابزار بدون پارامتر، ورودی اضافه را رد نمی‌کند؛ فیلترِ collector آن را دور می‌اندازد
    via_run = await OsInfoTool(config).run({"extra_junk": 1}, context=ToolContext(config=config))
    assert via_run.success and via_run.data["system"] == platform.system()
    # اما ابزاری که پارامتر اعلام کرده، ورودی ناشناخته را رد می‌کند
    rejected = await DiskInfoTool(config).run({"top_entries": 1, "extra_junk": 1}, context=ToolContext(config=config))
    assert not rejected.success and rejected.error_code == "invalid_input"
    assert "unknown parameter(s): extra_junk" in (rejected.error or "")


async def test_disk_tool_path_and_top_entries(config: Config, workspace: Path) -> None:
    """path نسبی به project_root می‌چسبد و top_entries اعمال می‌شود."""
    (workspace / "assets").mkdir()
    (workspace / "assets" / "big.bin").write_bytes(b"1" * 2048)
    result = await DiskInfoTool(config).run({"path": "assets", "top_entries": 3}, context=ToolContext(config=config))
    assert result.success, result.error
    assert result.data["path"] == str(workspace / "assets")
    largest = result.data["largest_entries"]
    assert largest[0]["path"] == "." and largest[0]["size_bytes"] == 2048


async def test_disk_tool_rejects_bad_top_entries(config: Config) -> None:
    """top_entries خارج از بازه (۰..۲۵)."""
    result = await DiskInfoTool(config).run({"top_entries": 99}, context=ToolContext(config=config))
    assert not result.success and result.error_code == "invalid_input"
    assert "between 0 and 25" in (result.error or "")


async def test_disk_tool_reports_os_error(config: Config) -> None:
    """مسیر ناموجود → disk_error (نه استثنای بازجوشی)."""
    result = await DiskInfoTool(config).run({"path": "/no-such-volume-xyz"}, context=ToolContext(config=config))
    assert not result.success and result.error_code in ("disk_error", "invalid_input")
    assert "no-such-volume-xyz" in (result.error or "")


async def test_collector_exception_is_reported(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """اگر collector خطا بدهد، پیام کوتاه و error_code مناسب برگردد."""

    def boom() -> dict[str, Any]:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(si, "collect_os_info", boom)
    result = await OsInfoTool(config).run({}, context=ToolContext(config=config))
    assert not result.success and result.error_code == "collector_failed"
    assert "RuntimeError: probe failed" in (result.error or "")


async def test_validation_error_from_collector_maps_to_invalid_input(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ValidationError داخل collector → invalid_input."""

    def bad() -> dict[str, Any]:
        raise si.ValidationError("path is not readable")

    monkeypatch.setattr(si, "collect_memory_info", bad)
    result = await MemoryInfoTool(config).run({}, context=ToolContext(config=config))
    assert (
        not result.success and result.error_code == "invalid_input" and "path is not readable" in (result.error or "")
    )


# ---------------------------------------------------------------------------
# حریم خصوصی شبکه
# ---------------------------------------------------------------------------
async def test_network_tool_does_not_query_public_ip_by_default(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """بدون flag، هیچ درخواست بیرونی زده نمی‌شود."""

    async def forbidden(*args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        raise AssertionError("public IP must not be fetched by default")

    monkeypatch.setattr(si, "fetch_public_ip_raw", forbidden)
    result = await NetworkInfoTool(config).run({}, context=ToolContext(config=config))
    assert result.success and "public_ip" not in result.data


async def test_network_tool_returns_public_ip_when_asked(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """با public_ip=true مقدار از fetch خوانده می‌شود."""
    calls: list[dict[str, Any]] = []

    async def fake_fetch(config: Any = None, *, timeout: float = 5.0) -> tuple[str, dict[str, Any]]:
        calls.append({"timeout": timeout})
        return "  203.0.113.9  \n", {}

    monkeypatch.setattr(si, "fetch_public_ip_raw", fake_fetch)
    result = await NetworkInfoTool(config).run({"public_ip": True}, context=ToolContext(config=config))
    assert result.success and result.data["public_ip"] == "203.0.113.9"
    assert len(calls) == 1 and "local_ips" in result.data


async def test_network_tool_string_flag_is_coerced(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """مدل ممکن است رشته بفرستد؛ 'false' یعنی خاموش."""
    called = {"count": 0}

    async def fake_fetch(config: Any = None, *, timeout: float = 5.0) -> tuple[str, dict[str, Any]]:
        called["count"] += 1
        return "1.2.3.4", {}

    monkeypatch.setattr(si, "fetch_public_ip_raw", fake_fetch)
    off = await NetworkInfoTool(config).run({"public_ip": "false"}, context=ToolContext(config=config))
    assert off.success and called["count"] == 0 and "public_ip" not in off.data
    on = await NetworkInfoTool(config).run({"public_ip": "yes"}, context=ToolContext(config=config))
    assert on.success and called["count"] == 1 and on.data["public_ip"] == "1.2.3.4"


async def test_network_tool_degrades_when_public_lookup_fails(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """خطای IP عمومی با پیام unavailable (و نه شکست کل ابزار)."""

    async def failing(config: Any = None, *, timeout: float = 5.0) -> tuple[str, dict[str, Any]]:
        raise RuntimeError("no route to host")

    monkeypatch.setattr(si, "fetch_public_ip_raw", failing)
    result = await NetworkInfoTool(config).run({"public_ip": True}, context=ToolContext(config=config))
    assert result.success and result.data["public_ip"].startswith("unavailable (RuntimeError)")


# ---------------------------------------------------------------------------
# رجیستری و psutil
# ---------------------------------------------------------------------------
def test_all_system_tools_registered() -> None:
    """همه‌ی ابزارهای سیستم در رجیستری‌اند."""
    from src.core.tool_registry import ToolRegistry, discover_tools

    discover_tools(force=True)
    names = ToolRegistry.names()
    for expected in ("os_info", "cpu_info", "memory_info", "disk_info", "network_info"):
        assert expected in names
    assert ToolRegistry.get("memory_info").category.value == "system"  # type: ignore[union-attr]


def test_psutil_helper_returns_module_or_none() -> None:
    """_psutil هیچ‌وقت throw نمی‌کند."""
    psutil = si._psutil()
    try:
        import psutil as expected  # noqa: F401
    except ImportError:
        expected = None  # type: ignore[assignment]
    assert (psutil is None) == (expected is None)
    assert sys.modules.get("psutil") or psutil is None


COLLECTOR_BY_TOOL = {
    "os_info": "collect_os_info",
    "cpu_info": "collect_cpu_info",
    "memory_info": "collect_memory_info",
    "disk_info": "collect_disk_info",
    "network_info": "collect_network_info",
}


@pytest.mark.parametrize("tool_class", ALL_TOOLS, ids=lambda value: value.name)
async def test_results_are_capped_like_other_tools(
    config: Config, tool_class: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """خروجی بزرگ با max_output_chars برش می‌خورد (قرارداد مشترک BaseTool)."""
    payload = {f"key_{index}": "x" * 500 for index in range(60)}
    small = config.model_copy(update={"max_output_chars": 900})

    def fake(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(si, COLLECTOR_BY_TOOL[tool_class.name], fake)
    result = await tool_class(small).run({}, context=ToolContext(config=small))
    assert result.success
    assert result.truncated is True and result.data["_truncated_fields"]
    assert result.to_llm_string(max_chars=400).endswith("aaaa") is False
    assert len(result.to_llm_string(max_chars=400)) <= 400

"""تست گزارش فعالیت (:mod:`src.core.reports`) و اتصالش به ایجنت.

گزارش‌ها از دو منبع ساخته می‌شوند: فایل JSONL اجراها (ActivityRecorder) و حافظه‌ی
بلندمدت (برنامه‌های باز/ترجیحات). هر دو در تست روی ``tmp_path`` می‌نویسند.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from src.agent import UniversalAgent
from src.config import Config
from src.core.event_bus import EVENTS, EventBus
from src.core.memory import AgentMemory, reset_memory_cache
from src.core.reports import ActivityRecorder, build_report, redact_line, render_report_text, reset_recorder_cache
from src.core.tool_registry import discover_tools
from tests.fakes import FakeOpenAIClient, make_chat_response


@pytest.fixture(autouse=True)
def _isolate() -> None:
    """کش‌ها و رجیستری برای هر تست آماده شوند."""
    reset_recorder_cache()
    reset_memory_cache()
    discover_tools(force=True)


@pytest.fixture
def activity(config: Config) -> Path:
    """مسیر فایل فعالیتِ همان config تست."""
    return Path(str(config.activity_path))


def recorder(path: Path, **kwargs: Any) -> ActivityRecorder:
    """ساخت recorder مستقیم (بدون config) برای تست منطق خالص."""
    return ActivityRecorder(path, **kwargs)


def read_lines(path: Path) -> list[dict[str, Any]]:
    """خط‌های JSONL فایل."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# ActivityRecorder
# ---------------------------------------------------------------------------
def test_record_appends_and_persists(tmp_path: Path) -> None:
    """هر record یک خط JSON است و با نمونه‌ی تازه هم خوانده می‌شود."""
    path = tmp_path / "activity.jsonl"
    memory = recorder(path)
    entry = memory.record({"kind": "run", "ok": True, "tools": ["terminal_run"], "tokens": 120})
    assert entry["ts"] and entry["iso"]
    lines = read_lines(path)
    assert len(lines) == 1 and lines[0]["tools"] == ["terminal_run"]
    assert [item["kind"] for item in recorder(path).entries()] == ["run"]
    if not _windows():
        assert path.stat().st_mode & 0o777 == 0o600


def test_stats_and_clear(tmp_path: Path) -> None:
    """stats کلیدهای مصرفی UI/CLI را می‌دهد و clear فایل را خالی می‌کند."""
    path = tmp_path / "activity.jsonl"
    store = recorder(path)
    store.record({"kind": "run", "ok": True})
    store.record({"kind": "blocked", "tool": "terminal_run"})
    stats = store.stats()
    assert stats["records"] == 2
    assert stats["by_kind"] == {"run": 1, "blocked": 1}
    assert stats["path"] == str(path)
    assert store.clear() == 2
    assert path.read_text(encoding="utf-8") == ""
    assert store.stats()["records"] == 0


def test_entries_filter_by_window_and_kind(tmp_path: Path) -> None:
    """entries با since/kinds/limit فیلتر می‌شود و ترتیب از تازه به قدیم است."""
    store = recorder(tmp_path / "activity.jsonl")
    store.record({"kind": "run", "ts": time.time() - 90000})
    store.record({"kind": "run"})
    store.record({"kind": "blocked", "tool": "delete_file"})
    assert [item["kind"] for item in store.entries()][:2] == ["blocked", "run"]
    assert len(store.entries(since=time.time() - 3600)) == 2
    assert [item["tool"] for item in store.entries(kinds=["blocked"])] == ["delete_file"]
    assert len(store.entries(limit=1)) == 1


def test_corrupt_lines_are_ignored_on_load(tmp_path: Path) -> None:
    """فایل نیمه‌نویسه‌شده (crash حین نوشتن) خواندن را نمی‌شکند."""
    path = tmp_path / "activity.jsonl"
    path.write_text('{"kind": "run", "ok": true}\n{oops\n\n', encoding="utf-8")
    store = recorder(path)
    assert [item["kind"] for item in store.entries()] == ["run"]


def test_compaction_keeps_newest(tmp_path: Path) -> None:
    """پس از عبور از سقف، فایل فشرده می‌شود و تازه‌ترین‌ها می‌مانند."""
    store = recorder(tmp_path / "activity.jsonl", max_records=50)
    for index in range(90):
        store.record({"kind": "run", "index": index})
    lines = read_lines(tmp_path / "activity.jsonl")
    # فایل بین max_records و ۱.۵× آن نوسان می‌کند (بازنوشتن هر رکورد گران است)
    assert 50 <= len(lines) <= 75
    assert lines[-1]["index"] == 89
    assert store.stats()["records"] <= 75


def test_in_memory_recorder_writes_nothing(tmp_path: Path) -> None:
    """path=None یعنی فقط در حافظه (برای تست‌های سریع)."""
    store = recorder(None)
    store.record({"kind": "run"})
    assert len(store.entries()) == 1
    assert list(tmp_path.iterdir()) == []


def test_disabled_recorder_ignores_records(tmp_path: Path) -> None:
    """enabled=False روی دیسک نمی‌نویسد."""
    path = tmp_path / "activity.jsonl"
    store = recorder(path, enabled=False)
    store.record({"kind": "run"})
    assert not path.exists()
    assert store.entries() == []


def test_note_helper_redacts_secrets(tmp_path: Path) -> None:
    """یادداشت متنی هم ماسک می‌شود (کلید وارد گزارش نمی‌شود)."""
    store = recorder(tmp_path / "activity.jsonl")
    entry = store.note("user asked to reuse sk-abcdefghijklmnopqrstuvwxyz123456 for the gateway")
    assert "abcdefghijklmnopqrstuvwxyz" not in entry["text"]
    assert redact_line("sk-abcdefghijklmnopqrstuvwxyz123456").endswith("[redacted]")


# ---------------------------------------------------------------------------
# اتصال به config و bus
# ---------------------------------------------------------------------------
def test_for_config_respects_reports_flag(config: Config) -> None:
    """با REPORTS_ENABLED=false هیچ recorderی ساخته نمی‌شود."""
    assert ActivityRecorder.for_config(config) is not None
    assert ActivityRecorder.for_config(config.model_copy(update={"reports_enabled": False})) is None
    assert ActivityRecorder.for_config(None) is None


def test_for_config_is_cached_per_path(config: Config) -> None:
    """برای یک فایل، یک نمونه‌ی مشترک (کش) استفاده می‌شود."""
    first = ActivityRecorder.for_config(config)
    assert first is not None
    assert ActivityRecorder.for_config(config) is first
    ActivityRecorder.clear_cache()
    assert ActivityRecorder.for_config(config) is not first


def test_attach_records_bus_events(config: Config) -> None:
    """attach() رویدادهای اجرا و ایمنی را به فایل گزارش می‌رساند."""
    bus = EventBus(history_size=20)
    ids = ActivityRecorder.attach(bus, config)
    assert len(ids) == 5
    import asyncio

    async def emit() -> None:
        await bus.emit(
            EVENTS.AGENT_COMPLETED,
            {"ok": True, "iterations": 2, "tools": ["os_info"], "duration_ms": 30, "tokens": 10},
        )
        await bus.emit(EVENTS.SAFETY_BLOCKED, {"tool": "delete_file", "reasons": ["outside allowed directories"]})
        await bus.emit(EVENTS.TOOL_DENIED, {"tool": "terminal_run"})
        await bus.emit(EVENTS.TOOL_FAILED, {"tool": "web_search", "error": "tls handshake failed"})

    asyncio.run(emit())
    kinds = [item["kind"] for item in read_lines(Path(str(config.activity_path)))]
    assert "run" in kinds and "blocked" in kinds and "denied" in kinds and "tool_failed" in kinds


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------
def test_build_report_aggregates_runs_and_safety(config: Config) -> None:
    """اجرا/توکن/ابزارها و شمارش‌های ایمنی از رکوردهای اجرا درمی‌آیند."""
    store = ActivityRecorder.for_config(config)
    assert store is not None
    store.record(
        {
            "kind": "run",
            "ok": True,
            "iterations": 2,
            "tools": ["terminal_run", "memory_search"],
            "calls": [
                {"tool": "terminal_run", "ok": False, "code": "declined", "duration_ms": 10},
                {"tool": "memory_search", "ok": True, "code": "", "duration_ms": 4},
            ],
            "duration_ms": 1500,
            "tokens": 900,
        }
    )
    store.record(
        {
            "kind": "run",
            "ok": False,
            "tools": ["terminal_run"],
            "calls": [{"tool": "terminal_run", "ok": False, "code": "tool_error", "duration_ms": 8}],
            "error": "model 429",
            "tokens": 0,
            "duration_ms": 40,
        }
    )
    report = build_report(config, days=7, memory=None, recorder=store)
    assert report["runs"]["total"] == 2
    assert report["runs"]["ok"] == 1 and report["runs"]["failed"] == 1
    assert report["runs"]["tokens"] == 900
    assert report["runs"]["duration_human"].endswith("s")
    # «declined» یعنی ابزار هرگز اجرا نشد؛ پس در tool_failures شمرده نمی‌شود
    assert report["safety"]["denied"] == 1 and report["safety"]["tool_failures"] == 1
    assert report["safety"]["last_denied"] == [
        {"tool": "terminal_run", "iso": report["safety"]["last_denied"][0]["iso"]}
    ]
    assert report["recent"][0]["error"] == "model 429"  # تازه‌ترین اول می‌آید
    assert report["recent"][1]["tools"] == ["terminal_run", "memory_search"]
    terminal_row = next(row for row in report["tools"] if row["tool"] == "terminal_run")
    assert terminal_row == {"tool": "terminal_run", "calls": 2, "failures": 2, "denied": 1, "blocked": 0}
    assert not report["warnings"]


def test_build_report_includes_memory_and_plans(config: Config) -> None:
    """بخش حافظه: ترجیحات، برنامه‌های باز و مسیر فایل‌ها."""
    memory = AgentMemory.for_config(config)
    assert memory is not None
    memory.add("user prefers Persian answers", kind="preference", pin=True)
    memory.add("apply for the payment gateway next week", kind="plan", tags=["area:payments"])
    report = build_report(config, days=0, memory=memory)
    assert report["memory"]["enabled"] is True
    assert report["memory"]["records"] == 2
    assert [item["content"] for item in report["plans"]] == ["apply for the payment gateway next week"]
    assert any("Persian" in item["content"] for item in report["notes"])
    assert report["files"]["memory"] and report["files"]["activity"]
    assert any("open plan" in item for item in report["next_actions"])


def test_build_report_warns_when_stores_are_off(config: Config) -> None:
    """وقتی حافظه/گزارش خاموش‌اند، گزارش با هشدار می‌گوید داده کامل نیست."""
    off = config.model_copy(update={"reports_enabled": False, "memory_enabled": False})
    report = build_report(off, days=7)
    assert report["runs"]["total"] == 0
    assert report["memory"]["enabled"] is False
    assert len(report["warnings"]) == 2


def test_build_report_falls_back_to_tools_list(config: Config) -> None:
    """رکوردهای قدیمی بدون ``calls`` هم شمارش می‌شوند (سازگاری با فایل موجود)."""
    store = ActivityRecorder.for_config(config)
    assert store is not None
    store.record({"kind": "run", "ok": True, "tools": ["os_info", "os_info"]})
    report = build_report(config, days=7, memory=None, recorder=store)
    assert report["tools"][0] == {"tool": "os_info", "calls": 2, "failures": 0, "denied": 0, "blocked": 0}


def test_next_actions_mention_denied_and_failing_tools(config: Config) -> None:
    """پیشنهاد «بعدی» از ردّهابرگ می‌آید تا ایجنت اشتباه را تکرار نکند."""
    store = ActivityRecorder.for_config(config)
    assert store is not None
    store.record(
        {"kind": "run", "ok": True, "tools": [], "calls": [{"tool": "delete_file", "ok": False, "code": "declined"}]}
    )
    store.record({"kind": "tool_failed", "tool": "browser_click", "error": "selector not found"})
    report = build_report(config, days=7, memory=None, recorder=store)
    joined = " ".join(report["next_actions"])
    assert "delete_file" in joined and "browser_click" in joined


def test_render_report_text_has_sections(config: Config) -> None:
    """نسخه‌ی متنی برای ترمینال، سرفصل‌های اصلی را دارد."""
    memory = AgentMemory.for_config(config)
    assert memory is not None
    memory.add("deploy with scripts/deploy.sh", kind="procedure")
    report = build_report(config, days=7, memory=memory)
    text = render_report_text(report, title="Weekly report")
    assert "Weekly report" in text
    assert "runs:" in text and "safety:" in text
    assert "from memory" in text and "deploy with scripts/deploy.sh" in text


def test_render_report_text_on_empty_report() -> None:
    """گزارش خالی هم قابل‌رندر است (بدون KeyError)."""
    text = render_report_text(
        {"generated_at": "now", "runs": {}, "safety": {}, "tools": [], "warnings": ["nothing to see"]}
    )
    assert "nothing to see" in text


# ---------------------------------------------------------------------------
# اتصال به ایجنت
# ---------------------------------------------------------------------------
def build_agent(responses: list[Any], config: Config, **kwargs: Any) -> UniversalAgent:  # noqa: ANN401 - kwargs تستی
    """ایجنت با client ساختگی و config تست (حافظه/گزارش روی tmp_path)."""
    return UniversalAgent(
        config,
        tools=["os_info", "terminal_run"],
        client=FakeOpenAIClient(responses),
        event_bus=EventBus(),
        **kwargs,
    )


async def test_agent_writes_activity_and_memory(config: Config, activity: Path) -> None:
    """پس از یک اجرا با ابزار، هم فایل فعالیت هم حافظه به‌روز می‌شوند."""
    agent = build_agent(
        [
            make_chat_response(tool_calls=[{"id": "a", "name": "os_info", "arguments": {}}]),
            make_chat_response(content="linux it is"),
        ],
        config,
    )
    result = await agent.ask("what os am I on?")
    assert result.ok
    lines = read_lines(activity)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["kind"] == "run" and entry["ok"] is True
    assert entry["tools"] == ["os_info"]
    assert entry["profile"] == "generalist"
    assert entry["tokens"] >= 0
    stats = agent.memory_summary()
    assert stats["records"] >= 1
    assert stats["path"] == str(config.memory_path)
    text = render_report_text(agent.activity_report(days=1))
    assert "runs: 1" in text
    await agent.close()


async def test_agent_prompt_contains_memory_block(config: Config) -> None:
    """بلوک حافظه در system prompt می‌نشیند تا مدل «یادش باشد»."""
    memory = AgentMemory.for_config(config)
    assert memory is not None
    memory.add("the user's favourite editor is neovim", kind="preference", pin=True)
    agent = build_agent([make_chat_response(content="noted")], config)
    messages = agent._build_messages("what should I use to edit?")  # noqa: SLF001 - تست behavior داخلی
    assert "neovim" in messages[0]["content"]
    assert "Long-term memory" in messages[0]["content"]
    await agent.close()


async def test_use_memory_false_leaves_store_untouched(config: Config, activity: Path) -> None:
    """use_memory=False فقط حافظه را خاموش می‌کند، گزارش همچنان نوشته می‌شود."""
    agent = build_agent([make_chat_response(content="hello")], config, use_memory=False)
    assert agent.memory is None
    await agent.ask("hello there")
    assert "Long-term memory" not in agent._build_messages("x")[0]["content"]  # noqa: SLF001
    lines = read_lines(activity)  # ضبط فعالیت مستقل از حافظه است و نوشته می‌شود
    assert len(lines) == 1 and lines[0]["kind"] == "run" and lines[0]["tools"] == []
    await agent.close()


async def test_reports_disabled_writes_nothing(config: Config, tmp_path: Path) -> None:
    """REPORTS_ENABLED=false یعنی نه فایل فعالیت، نه recorder."""
    quiet = config.model_copy(update={"reports_enabled": False})
    agent = build_agent([make_chat_response(content="hi")], quiet)
    assert agent.recorder is None
    await agent.ask("hi")
    assert not Path(str(config.activity_path)).exists()
    await agent.close()


async def test_activity_recording_never_breaks_the_run(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """خطای دیسک در گزارش‌دهی، اجرا را شکست نمی‌دهد."""
    agent = build_agent([make_chat_response(content="still fine")], config)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(ActivityRecorder, "record", boom)
    result = await agent.ask("talk to me")
    assert result.ok and result.text == "still fine"
    await agent.close()


def _windows() -> bool:
    """بررسی مجوز فایل فقط روی یونیکس معنا دارد."""
    import os

    return os.name != "posix"

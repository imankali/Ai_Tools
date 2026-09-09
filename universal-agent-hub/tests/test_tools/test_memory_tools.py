"""تست ابزارهای حافظه (:mod:`src.tools.memory`).

ابزارها از همان قرارداد بقیه‌ی ابزارها پیروی می‌کنند: config به *نمونه‌ی ابزار* تزریق
می‌شود (این‌جا fixture :func:`config` که مسیر حافظه را به ``tmp_path`` می‌برد تا home کاربر
دست‌نخورده بماند) و خروجی همیشه :class:`ToolResult` است، نه استثنا.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.config import Config
from src.core.memory import MEMORY_KINDS, AgentMemory, reset_memory_cache
from src.core.tool_registry import ToolRegistry, discover_tools
from src.tools.memory import (
    MemoryForgetTool,
    MemoryReadTool,
    MemorySearchTool,
    MemoryToolBase,
    MemoryWriteTool,
)

MEMORY_TOOLS = ("memory_write", "memory_read", "memory_search", "memory_forget")


@pytest.fixture(autouse=True)
def _registered(config: Config) -> None:
    """رجیستری و کش حافظه برای هر تست آماده و تمیز باشد."""
    reset_memory_cache()
    ToolRegistry.set_default_config(config)
    discover_tools(force=True)
    reset_memory_cache()


def tool(name: str, config: Config) -> Any:  # noqa: ANN401 - نمونه‌ی BaseTool در تست
    """نمونه‌ی ابزار با config تست (مسیر حافظه داخل tmp_path)."""
    found = ToolRegistry.get(name, config=config)
    assert found is not None, f"tool {name} is not registered"
    return found


def stored(config: Config) -> list[str]:
    """متنِ رکوردهای فعلی همان config (بدون وابستگی به ابزار خواندن)."""
    memory = AgentMemory.for_config(config)
    assert memory is not None
    return [item.content for item in memory.all()]


# ---------------------------------------------------------------------------
# ثبت و اسکیماما
# ---------------------------------------------------------------------------
def test_all_memory_tools_are_registered(config: Config) -> None:
    """هر چهار ابزار ثبت شده‌اند و از پایه‌ی مشترک ارث می‌برند."""
    for name in MEMORY_TOOLS:
        assert name in ToolRegistry.names()
        instance = tool(name, config)
        assert isinstance(instance, MemoryToolBase)
        assert instance.to_info().name == name
    assert MemoryWriteTool.risk_level == MemoryReadTool.risk_level == MemorySearchTool.risk_level
    assert MemoryForgetTool.risk_level.value > MemoryWriteTool.risk_level.value  # حذف یک پله خطرناک‌تر است


def test_schemas_are_function_calling_ready(config: Config) -> None:
    """اسکیماها پوسته‌ی استاندارد دارند و لیست requiredها درست است."""
    expected = {
        "memory_write": ["content"],
        "memory_read": [],
        "memory_search": ["query"],
        "memory_forget": ["id"],
    }
    for name, required in expected.items():
        schema = tool(name, config).get_schema()
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] == name
        assert function["description"]
        assert function["parameters"]["required"] == required
        assert function["parameters"]["additionalProperties"] is False
        assert set(function["parameters"]["properties"]) >= set(required)


def test_kind_enum_lists_every_supported_kind(config: Config) -> None:
    """مدل باید بداند چه kindهایی مجاز است؛ منبعش همان ماژول حافظه است."""
    for name in ("memory_write", "memory_read", "memory_search"):
        properties = tool(name, config).get_schema()["function"]["parameters"]["properties"]
        assert properties["kind"]["enum"] == list(MEMORY_KINDS)


# ---------------------------------------------------------------------------
# چرخه‌ی کامل: نوشتن ← خواندن ← جست‌وجو ← حذف
# ---------------------------------------------------------------------------
async def test_write_read_search_forget_flow(config: Config) -> None:
    """چهار ابزار با هم یک دور کامل می‌زنند."""
    written = await tool("memory_write", config).run(
        {
            "content": "staging deploys with scripts/deploy.sh after tests pass",
            "kind": "procedure",
            "tags": ["project:webshop"],
        }
    )
    assert written.success, written.error
    record_id = written.data["id"]
    assert written.data["saved"] is True

    read = await tool("memory_read", config).run({"limit": 5})
    assert read.success and read.data["count"] == 1
    assert read.data["records"][0]["content"].startswith("staging deploys")

    by_id = await tool("memory_read", config).run({"id": record_id})
    assert by_id.success and by_id.data["id"] == record_id

    found = await tool("memory_search", config).run({"query": "deploy staging script"})
    assert found.success and found.data["count"] >= 1

    gone = await tool("memory_forget", config).run({"id": record_id})
    assert gone.success and gone.data["deleted"] == record_id
    assert stored(config) == []


async def test_written_record_survives_a_new_store(config: Config) -> None:
    """هرچه ابزار نوشته واقعاً روی دیسک است و بین اجراها می‌ماند."""
    await tool("memory_write", config).run(
        {"content": "user wants Persian answers", "kind": "preference", "pin": True}
    )
    memory_file = Path(str(config.memory_path))
    assert memory_file.exists()
    reopened = AgentMemory(memory_file)
    assert [item.content for item in reopened.all()] == ["user wants Persian answers"]
    assert reopened.all()[0].pinned is True


async def test_repeated_note_is_deduplicated(config: Config) -> None:
    """متن تکراری حافظه را تورم نمی‌دهد؛ همان رکورد تقویت می‌شود."""
    writer = tool("memory_write", config)
    first = await writer.run({"content": "brief answers only", "kind": "preference"})
    second = await writer.run({"content": "brief answers only", "kind": "preference"})
    assert first.success and second.success
    assert first.data["id"] == second.data["id"]
    assert second.data["records"] == 1


async def test_source_of_tool_written_record(config: Config) -> None:
    """منبع رکورد نشان می‌دهد ابزار نوشته، نه کاربر (برای پنل گزارش مهم است)."""
    written = await tool("memory_write", config).run({"content": "notes about the build", "kind": "note"})
    memory = AgentMemory.for_config(config)
    assert memory is not None
    record = memory.find(written.data["id"])
    assert record is not None and record.source == "tool:memory_write"


async def test_secret_in_note_is_redacted(config: Config) -> None:
    """کلیدی که مدل اشتباهاً می‌خواهد ذخیره کند، ماسک می‌شود."""
    await tool("memory_write", config).run(
        {"content": "use sk-abcdefghijklmnopqrstuvwxyz123456 for payments", "kind": "note"}
    )
    text = stored(config)[0]
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "sk-abc" in text  # پیشوند برای تشخیص باقی می‌ماند


# ---------------------------------------------------------------------------
# اعتبارسنجی ورودی
# ---------------------------------------------------------------------------
async def test_invalid_kind_is_rejected_with_guidance(config: Config) -> None:
    """kind ناشناخته با پیام راهنما رد می‌شود."""
    result = await tool("memory_write", config).run({"content": "x", "kind": "gossip"})
    assert not result.success
    assert result.error_code == "invalid_input"
    assert "preference" in (result.error or "")


async def test_empty_content_and_out_of_range_confidence(config: Config) -> None:
    """متن خالی و اطمینان خارج از بازه رد می‌شوند."""
    writer = tool("memory_write", config)
    empty = await writer.run({"content": "   "})
    assert not empty.success and empty.error_code == "invalid_input"
    bad = await writer.run({"content": "ok note", "confidence": 4})
    assert not bad.success and bad.error_code == "invalid_input"
    assert stored(config) == []


async def test_confidence_passed_to_pydantic_is_a_validation_error(config: Config) -> None:
    """عدد غیرقابل‌تبدیل به float با پیام خوانا رد می‌شود (نه crash)."""
    result = await tool("memory_write", config).run({"content": "note", "confidence": "very-sure"})
    assert not result.success
    assert "confidence" in (result.error or "").lower()


async def test_search_requires_query(config: Config) -> None:
    """جست‌وجوی بی‌کوئری معنادار نیست."""
    result = await tool("memory_search", config).run({"query": ""})
    assert not result.success and result.error_code == "invalid_input"


async def test_limits_are_bounded(config: Config) -> None:
    """limitهای اغراق‌آمیز رد می‌شوند (پیام خوانا) و limitهای مجاز رعایت می‌شوند."""
    writer = tool("memory_write", config)
    for index in range(6):
        await writer.run({"content": f"observation number {index}", "kind": "note"})
    too_big = await tool("memory_read", config).run({"limit": 10_000})
    assert not too_big.success and too_big.error_code == "invalid_input"
    assert "between 1 and 50" in (too_big.error or "")
    read = await tool("memory_read", config).run({"limit": 4})
    assert read.success and read.data["count"] == 4
    search = await tool("memory_search", config).run({"query": "observation number", "limit": 2})
    assert search.success and search.data["count"] <= 2


async def test_unknown_ids_report_not_found(config: Config) -> None:
    """id ناموجود در read و forget هر دو not_found می‌دهند."""
    read = await tool("memory_read", config).run({"id": "zzzzzz"})
    assert not read.success and read.error_code == "not_found"
    forget = await tool("memory_forget", config).run({"id": "zzzzzz"})
    assert not forget.success and forget.error_code == "not_found"


async def test_pinned_record_needs_force_to_forget(config: Config) -> None:
    """ترجیح چسبانِ کاربر با یک «فراموش کن» ساده پاک نمی‌شود."""
    written = await tool("memory_write", config).run(
        {"content": "never lose this", "kind": "preference", "pin": True}
    )
    refused = await tool("memory_forget", config).run({"id": written.data["id"]})
    assert not refused.success and "pinned" in (refused.error or "")
    forced = await tool("memory_forget", config).run({"id": written.data["id"], "force": True})
    assert forced.success
    assert stored(config) == []


# ---------------------------------------------------------------------------
# حالت خاموش بودن حافظه
# ---------------------------------------------------------------------------
async def test_tools_explain_when_memory_is_disabled(workspace: Path) -> None:
    """با MEMORY_ENABLED=false ابزارها خطای روشن می‌دهند، نه استثنا."""
    disabled = Config(
        openai_api_key="sk-test",
        project_root=workspace,
        allowed_directories=[str(workspace)],
        memory_enabled=False,
    )
    cases: dict[str, dict[str, Any]] = {
        "memory_write": {"content": "anything"},
        "memory_read": {},
        "memory_search": {"query": "anything"},
        "memory_forget": {"id": "abc"},
    }
    for name, arguments in cases.items():
        result = await tool(name, disabled).run(arguments)
        assert not result.success, name
        assert result.error_code == "disabled", name
        assert "MEMORY_ENABLED" in (result.error or ""), name


async def test_tools_work_when_memory_dir_is_a_file(tmp_path: Path) -> None:
    """``MEMORY_DIR`` می‌تواند مستقیم مسیر فایل باشد (کلیدها مستقل‌اند)."""
    config = Config(
        openai_api_key="sk-test",
        project_root=tmp_path,
        memory_dir=str(tmp_path / "notes.jsonl"),
        memory_enabled=True,
    )
    written = await tool("memory_write", config).run({"content": "direct file target", "kind": "note"})
    assert written.success
    assert (tmp_path / "notes.jsonl").exists()
    assert stored(config) == ["direct file target"]

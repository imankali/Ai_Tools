"""تست ابزارهای فایل‌سیستم (:mod:`src.tools.filesystem`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.config import Config
from src.core.base_tool import ToolContext
from src.tools.filesystem import (
    DeleteFileTool,
    ListDirectoryTool,
    MoveFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from src.utils.validators import ValidationError

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# خواندن
# ---------------------------------------------------------------------------
async def test_read_file(config: Config, workspace: Path) -> None:
    """خواندن فایل متنی با آمار خطوط."""
    result = await ReadFileTool(config).run({"path": "sample.txt"}, context=ToolContext(config=config))
    assert result.success
    data = result.data
    assert data["content"] == "alpha\nbeta\ngamma"
    assert data["returned_lines"] == data["total_lines"] == 3
    assert "1 | alpha" in data["numbered"]
    assert data["size"] == "17 B"  # "alpha\nbeta\ngamma\n"
    assert not result.truncated


async def test_read_file_line_range(config: Config, workspace: Path) -> None:
    """خواندن محدوده‌ی خط."""
    result = await ReadFileTool(config).run(
        {"path": "sample.txt", "start_line": 2, "end_line": 3}, context=ToolContext(config=config)
    )
    assert result.data["content"] == "beta\ngamma"
    assert result.data["note"] == "lines 2-3"
    assert "2 | beta" in result.data["numbered"]


async def test_read_bad_range(config: Config, workspace: Path) -> None:
    """start_line > end_line خطا می‌دهد."""
    result = await ReadFileTool(config).run(
        {"path": "sample.txt", "start_line": 3, "end_line": 1}, context=ToolContext(config=config)
    )
    assert not result.success and "start_line must be <= end_line" in (result.error or "")


async def test_read_missing_file(config: Config) -> None:
    """فایل ناموجود."""
    result = await ReadFileTool(config).run({"path": "nope.txt"}, context=ToolContext(config=config))
    assert not result.success and "does not exist" in (result.error or "")


async def test_read_directory_is_rejected(config: Config, workspace: Path) -> None:
    """خواندن دایرکتوری به‌عنوان فایل."""
    result = await ReadFileTool(config).run({"path": "nested"}, context=ToolContext(config=config))
    assert not result.success


async def test_read_binary_file(config: Config, workspace: Path) -> None:
    """فایل باینری با پیام مناسب رد می‌شود."""
    blob = workspace / "pixel.png"
    blob.write_bytes(b"\x89PNG\x00\x01\x02")
    result = await ReadFileTool(config).run({"path": "pixel.png"}, context=ToolContext(config=config))
    assert not result.success and result.error_code == "binary_file"


async def test_read_respects_max_bytes(config: Config, workspace: Path) -> None:
    """سقف حجم: فایل بزرگ‌تر بریده می‌شود."""
    big = workspace / "big.txt"
    big.write_text("y" * 5000, encoding="utf-8")
    result = await ReadFileTool(config).run({"path": "big.txt", "max_bytes": 100}, context=ToolContext(config=config))
    assert result.success and result.data["truncated"] and len(result.data["content"]) == 100
    assert result.data["size_bytes"] == 5000


async def test_read_outside_sandbox(config: Config) -> None:
    """خواندن بیرون از دایرکتوری‌های مجاز."""
    result = await ReadFileTool(config).run({"path": "/etc/hostname"}, context=ToolContext(config=config))
    assert not result.success and "outside the allowed directories" in (result.error or "")


async def test_read_env_file_is_blocked(config: Config, workspace: Path) -> None:
    """فایل dotenv هرگز خوانده نمی‌شود."""
    (workspace / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    result = await ReadFileTool(config).run({"path": ".env"}, context=ToolContext(config=config))
    assert not result.success and "hunter2" not in str(result.data) + str(result.error)


# ---------------------------------------------------------------------------
# نوشتن
# ---------------------------------------------------------------------------
async def test_write_creates_and_overwrites(config: Config, workspace: Path) -> None:
    """ساخت و بازنویسی فایل."""
    ctx = ToolContext(config=config, confirm=lambda request: True)
    first = await WriteFileTool(config).run({"path": "out/hello.txt", "content": "one"}, context=ctx)
    assert first.success and first.data["created"] is True
    assert (workspace / "out" / "hello.txt").read_text(encoding="utf-8") == "one"
    second = await WriteFileTool(config).run({"path": "out/hello.txt", "content": "two"}, context=ctx)
    assert second.success and second.data["created"] is False and second.metadata["overwritten"]
    assert (workspace / "out" / "hello.txt").read_text(encoding="utf-8") == "two"


async def test_write_append_mode(config: Config, workspace: Path) -> None:
    """حالت append یک newline انتهایی اضافه می‌کند."""
    ctx = ToolContext(config=config, confirm=lambda request: True)
    target = workspace / "log.txt"
    target.write_text("line1\n", encoding="utf-8")
    await WriteFileTool(config).run({"path": "log.txt", "content": "line2", "mode": "append"}, context=ctx)
    assert target.read_text(encoding="utf-8") == "line1\nline2\n"


async def test_write_create_mode_refuses_existing(config: Config, workspace: Path) -> None:
    """mode=create روی فایل موجود خطا می‌دهد."""
    ctx = ToolContext(config=config, confirm=lambda request: True)
    result = await WriteFileTool(config).run({"path": "sample.txt", "content": "x", "mode": "create"}, context=ctx)
    assert not result.success and "already exists" in (result.error or "")
    assert (workspace / "sample.txt").read_text(encoding="utf-8").startswith("alpha")


async def test_write_unknown_mode(config: Config) -> None:
    """حلت ناشناخته رد می‌شود."""
    result = await WriteFileTool(config).run(
        {"path": "x.txt", "content": "y", "mode": "teleport"}, context=ToolContext(config=config)
    )
    assert not result.success and "overwrite, append, create" in (result.error or "")


async def test_write_requires_confirmation(config: Config, workspace: Path) -> None:
    """بدون تأیید کاربر، نوشتن انجام نمی‌شود."""
    ctx = ToolContext(config=config, confirm=lambda request: False)
    result = await WriteFileTool(config).run({"path": "forbidden.txt", "content": "x"}, context=ctx)
    assert not result.success and result.error_code == "declined"
    assert not (workspace / "forbidden.txt").exists()


async def test_write_rejects_oversized_content(config: Config) -> None:
    """محتوای بزرگ‌تر از سقف config رد می‌شود."""
    small = config.model_copy(update={"max_file_bytes": 1024})
    result = await WriteFileTool(small).run(
        {"path": "big.txt", "content": "z" * 5000}, context=ToolContext(config=small, confirm=lambda r: True)
    )
    assert not result.success and "too large" in (result.error or "")


async def test_write_blocks_system_paths(config: Config) -> None:
    """نوشتن در مسیر سیستمی (حتی با دسترسی آزاد) رد می‌شود."""
    permissive = config.model_copy(update={"unrestricted_filesystem": True})
    result = await WriteFileTool(permissive).run(
        {"path": "/etc/evil.conf", "content": "x"},
        context=ToolContext(config=permissive, confirm=lambda r: True),
    )
    assert not result.success and "protected system directory" in (result.error or "")


# ---------------------------------------------------------------------------
# فهرست
# ---------------------------------------------------------------------------
async def test_list_directory(config: Config, workspace: Path) -> None:
    """فهرست سطح‌یکم با اندازه و نوع."""
    result = await ListDirectoryTool(config).run({"path": "."}, context=ToolContext(config=config))
    names = [entry["name"] for entry in result.data["entries"]]
    assert "nested" in names and "sample.txt" in names
    entry = next(item for item in result.data["entries"] if item["name"] == "sample.txt")
    assert entry["type"] == "file" and entry["size_bytes"] == 17 and entry["modified"]
    assert result.data["truncated"] is False


async def test_list_with_pattern_and_depth(config: Config, workspace: Path) -> None:
    """فیلتر glob و عمق بازگشتی."""
    (workspace / "a.py").write_text("", encoding="utf-8")
    py_files = await ListDirectoryTool(config).run(
        {"path": ".", "pattern": "*.py", "depth": 2}, context=ToolContext(config=config)
    )
    names = sorted(entry["name"] for entry in py_files.data["entries"])
    assert names == ["a.py", "nested/deep.py"]
    single = await ListDirectoryTool(config).run(
        {"path": "nested", "pattern": "*.py"}, context=ToolContext(config=config)
    )
    assert [entry["name"] for entry in single.data["entries"]] == ["deep.py"]


async def test_list_limit_marks_truncation(config: Config, workspace: Path) -> None:
    """سقف تعداد با علامت truncated."""
    for index in range(5):
        (workspace / f"file{index}.txt").write_text("x", encoding="utf-8")
    result = await ListDirectoryTool(config).run({"path": ".", "limit": 3}, context=ToolContext(config=config))
    assert result.data["count"] == 3 and result.data["truncated"] is True


async def test_list_invalid_depth(config: Config, workspace: Path) -> None:
    """depth خارج از بازه."""
    result = await ListDirectoryTool(config).run({"path": ".", "depth": 99}, context=ToolContext(config=config))
    assert not result.success and "between 1 and 8" in (result.error or "")


async def test_list_missing_directory(config: Config) -> None:
    """دایرکتوری ناموجود."""
    result = await ListDirectoryTool(config).run({"path": "nope"}, context=ToolContext(config=config))
    assert not result.success and "does not exist" in (result.error or "")


# ---------------------------------------------------------------------------
# حذف و انتقال
# ---------------------------------------------------------------------------
async def test_delete_file_with_confirmation(config: Config, workspace: Path) -> None:
    """حذف با تأیید کاربر."""
    target = workspace / "temp.txt"
    target.write_text("bye", encoding="utf-8")
    tool = DeleteFileTool(config)
    assert tool.requires_confirmation
    approved = ToolContext(config=config, confirm=lambda request: True)
    result = await tool.run({"path": "temp.txt"}, context=approved)
    assert result.success and result.data["action"] == "deleted" and not target.exists()


async def test_delete_declined_keeps_file(config: Config, workspace: Path) -> None:
    """رد تأیید → فایل می‌ماند."""
    result = await DeleteFileTool(config).run(
        {"path": "sample.txt"}, context=ToolContext(config=config, confirm=lambda request: False)
    )
    assert not result.success and result.error_code == "declined"
    assert (workspace / "sample.txt").exists()


async def test_delete_directory_needs_recursive(config: Config, workspace: Path) -> None:
    """حذف دایرکتوری فقط با recursive."""
    refused = await DeleteFileTool(config).run(
        {"path": "nested"}, context=ToolContext(config=config, confirm=lambda r: True)
    )
    assert not refused.success and "recursive=true" in (refused.error or "")
    removed = await DeleteFileTool(config).run(
        {"path": "nested", "recursive": True}, context=ToolContext(config=config, confirm=lambda r: True)
    )
    assert removed.success and removed.data["entries"] == 1
    assert not (workspace / "nested").exists()


async def test_delete_dry_run(config: Config, workspace: Path) -> None:
    """پیش‌نمایش بدون حذف."""
    result = await DeleteFileTool(config).run(
        {"path": "sample.txt", "dry_run": True}, context=ToolContext(config=config, confirm=lambda r: True)
    )
    assert result.success and result.data["action"] == "would delete"
    assert (workspace / "sample.txt").exists()


async def test_move_and_copy(config: Config, workspace: Path) -> None:
    """جابه‌جایی و کپی."""
    ctx = ToolContext(config=config, confirm=lambda r: True)
    moved = await MoveFileTool(config).run({"source": "sample.txt", "destination": "moved.txt"}, context=ctx)
    assert moved.success and moved.data["operation"] == "moved"
    assert not (workspace / "sample.txt").exists() and (workspace / "moved.txt").is_file()

    copied = await MoveFileTool(config).run(
        {"source": "moved.txt", "destination": "copy.txt", "copy": True}, context=ctx
    )
    assert copied.success and (workspace / "copy.txt").is_file() and (workspace / "moved.txt").is_file()


async def test_move_refuses_overwrite(config: Config, workspace: Path) -> None:
    """بازنویسی مقصد نیازمند overwrite است."""
    result = await MoveFileTool(config).run(
        {"source": "sample.txt", "destination": "nested/deep.py"},
        context=ToolContext(config=config, confirm=lambda r: True),
    )
    assert not result.success and "already exists" in (result.error or "")
    forced = await MoveFileTool(config).run(
        {"source": "sample.txt", "destination": "nested/deep.py", "overwrite": True},
        context=ToolContext(config=config, confirm=lambda r: True),
    )
    assert forced.success and (workspace / "nested" / "deep.py").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_move_same_source_destination(config: Config) -> None:
    """مبدأ و مقصد یکسان."""
    with pytest.raises(ValidationError, match="identical"):
        MoveFileTool(config).validate_input(source="a.txt", destination="a.txt")


async def test_move_outside_sandbox_blocked(config: Config, workspace: Path) -> None:
    """انتقال به بیرون از sandbox رد می‌شود."""
    result = await MoveFileTool(config).run(
        {"source": "sample.txt", "destination": "/tmp/elsewhere.txt"},
        context=ToolContext(config=config, confirm=lambda r: True),
    )
    assert not result.success and "outside the allowed" in (result.error or "")
    assert (workspace / "sample.txt").exists()


# ---------------------------------------------------------------------------
# جست‌وجو
# ---------------------------------------------------------------------------
async def test_search_by_name(config: Config, workspace: Path) -> None:
    """جست‌وجوی نام فایل."""
    result = await SearchFilesTool(config).run({"query": "deep"}, context=ToolContext(config=config))
    assert result.success and result.data["count"] == 1
    assert result.data["matches"][0]["path"] == str(Path("nested/deep.py"))
    assert result.data["mode"] == "name"


async def test_search_in_content_with_regex(config: Config, workspace: Path) -> None:
    """جست‌وجوی محتوا با regex."""
    result = await SearchFilesTool(config).run(
        {"query": r"VALUE\s*=\s*(\d+)", "in_content": True, "regex": True, "glob": "**/*.py"},
        context=ToolContext(config=config),
    )
    assert result.success and result.data["matches"][0]["line"] == 1
    assert "VALUE = 42" in result.data["matches"][0]["match"]


async def test_search_case_sensitivity(config: Config, workspace: Path) -> None:
    """تفاوت بزرگ/کوچک حروف."""
    ignore = await SearchFilesTool(config).run(
        {"query": "ALPHA", "in_content": True}, context=ToolContext(config=config)
    )
    strict = await SearchFilesTool(config).run(
        {"query": "ALPHA", "in_content": True, "ignore_case": False}, context=ToolContext(config=config)
    )
    assert ignore.data["count"] == 1 and strict.data["count"] == 0


async def test_search_bad_regex_and_empty_query(config: Config) -> None:
    """regex خراب و query تهی."""
    bad = await SearchFilesTool(config).run({"query": "(unclosed", "regex": True}, context=ToolContext(config=config))
    assert not bad.success and "invalid regex" in (bad.error or "")
    empty = await SearchFilesTool(config).run({"query": "  "}, context=ToolContext(config=config))
    assert not empty.success and "query must not be empty" in (empty.error or "")


async def test_search_skips_large_files(config: Config, workspace: Path) -> None:
    """فایل‌های بزرگ‌تر از سقف، بررسی محتوایی نمی‌شوند."""
    huge = workspace / "huge.txt"
    huge.write_text("NEEDLE " + "z" * 2000, encoding="utf-8")
    cfg = config.model_copy(update={"max_file_bytes": 1024})
    result = await SearchFilesTool(cfg).run({"query": "NEEDLE", "in_content": True}, context=ToolContext(config=cfg))
    assert result.success and result.data["count"] == 0


# ---------------------------------------------------------------------------
# قراردادها
# ---------------------------------------------------------------------------
def test_schemas_and_infos(config: Config) -> None:
    """اسکیمای همه‌ی ابزارهای فایل و اطلاعات نمایشی."""
    for tool in (
        ReadFileTool(config),
        WriteFileTool(config),
        ListDirectoryTool(config),
        DeleteFileTool(config),
        MoveFileTool(config),
        SearchFilesTool(config),
    ):
        schema = tool.get_schema()["function"]
        assert schema["parameters"]["additionalProperties"] is False
        for name in schema["parameters"]["required"]:
            assert name in schema["parameters"]["properties"]
        info: dict[str, Any] = tool.to_info().model_dump()
        assert info["category"] == "filesystem"
    assert DeleteFileTool(config).to_info().requires_confirmation
    assert not ReadFileTool(config).to_info().requires_confirmation


async def test_path_argument_validation(config: Config) -> None:
    """پارامتر path تهی/ناشناخته."""
    missing = await ReadFileTool(config).run({}, context=ToolContext(config=config))
    assert not missing.success and "missing required parameter(s): path" in (missing.error or "")
    empty = await ReadFileTool(config).run({"path": "  "}, context=ToolContext(config=config))
    assert not empty.success and "empty path" in (empty.error or "")
    weird = await ReadFileTool(config).run({"path": "sample.txt", "extra": 1}, context=ToolContext(config=config))
    assert not weird.success and "unknown parameter(s): extra" in (weird.error or "")

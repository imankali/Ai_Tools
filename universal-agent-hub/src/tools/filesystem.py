"""ابزارهای مدیریت فایل‌سیستم (Filesystem tools).

همه‌ی ابزارهای این ماژول از :class:`FilesystemTool` ارث می‌برند که کار مشترکِ
«resolve + سیاست ایمنی مسیر» را انجام می‌دهد:

* باز کردن ``~`` و نرمال‌سازی ``..``
* رد کردن مسیرهای خارج از دایرکتوری‌های مجاز
* رد کردن فایل‌های حساس (``.ssh``, ``.env``, کلیدها…)
* علامت‌گذاری مسیرهای فقط‌خواندنی

عملیات نوشتن/حذف/انتقال همگی نیازمند تأیید کاربر هستند (مگر اینکه
``ENABLE_CONFIRMATION=false`` باشد).
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.helpers import format_size
from src.utils.safety import SafetyDecision
from src.utils.validators import ValidationError, bounded_int

__all__ = [
    "DeleteFileTool",
    "FilesystemTool",
    "ListDirectoryTool",
    "MoveFileTool",
    "ReadFileTool",
    "SearchFilesTool",
    "WriteFileTool",
]


class FilesystemTool(BaseTool):
    """پایه‌ی مشترک ابزارهای فایل (resolve + safety policy)."""

    category: ClassVar[ToolCategory] = ToolCategory.FILESYSTEM
    #: عملیات مورد استفاده برای تصمیم ایمنی: read/write/delete/move/list
    path_action: ClassVar[str] = "read"
    #: پارامترهایی که مسیر هستند و باید بررسی ایمنی شوند
    path_parameters: ClassVar[tuple[str, ...]] = ("path",)

    def resolve(
        self,
        raw: str,
        *,
        action: str | None = None,
        must_exist: bool = True,
        context: ToolContext | None = None,
    ) -> Path:
        """تبدیل مسیر خام به :class:`Path` معتبر از نظر سیاست ایمنی.

        Args:
            raw: مسیر ورودی (نسبی/مطلق/``~``).
            action: نوع عملیات (read/write/delete/move/list). پیش‌فرض از کلاس.
            must_exist: برای عملیات نوشتن می‌تواند False باشد (ایجاد فایل جدید).
            context: زمینه‌ی اجرا برای استفاده از guard مشترک ایجنت.

        Returns:
            مسیر resolve‌شده.

        Raises:
            ValidationError: مسیر خالی، ممنوع، خارج از sandbox یا موجود نیست.
        """
        text = str(raw or "").strip()
        if not text:
            raise ValidationError("path must not be empty")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            base = Path(str(getattr(self.config, "project_root", Path.cwd()))).resolve(strict=False)
            candidate = base / candidate
        resolved = candidate.resolve(strict=False)
        if must_exist and not resolved.exists():
            raise ValidationError(f"path does not exist: {resolved}")
        guard = self.safety_guard(context)
        if guard is not None:
            decision = guard.assess_path(resolved, action=action or self.path_action)
            if not decision.allowed:
                raise ValidationError(f"blocked by safety guard: {decision.reason_text}")
        return resolved

    async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
        """ارزیابی ایمنی همه‌ی پارامترهای مسیریِ درخواست."""
        guard = self.safety_guard(context)
        decision = SafetyDecision(
            allowed=True,
            requires_confirmation=self.requires_confirmation,
            risk=self.risk_level,
            reasons=["filesystem operation"],
        )
        if guard is None:
            return decision
        for key in self.path_parameters:
            raw = kwargs.get(key)
            if raw in (None, ""):
                continue
            probe = guard.assess_path(str(raw), action=self.path_action)
            if not probe.allowed:
                decision.allowed = False
                decision.risk = RiskLevel.CRITICAL
                decision.reasons.append(f"{key}: {probe.reason_text}")
            else:
                decision.risk = probe.risk
                if probe.requires_confirmation:
                    decision.requires_confirmation = True
                    decision.reasons.extend(probe.reasons)
        return decision

    def _confirmation_summary(self, kwargs: dict[str, Any]) -> str:
        """پیام تأیید خوانا برای عملیات فایل."""
        parts = [f"{key}={kwargs.get(key)}" for key in self.path_parameters if kwargs.get(key)]
        return f"{self.name} ({', '.join(parts)}) — this may change data on disk"


# ----------------------------------------------------------------------
# خواندن
# ----------------------------------------------------------------------
@register_tool
class ReadFileTool(FilesystemTool):
    """خواندن محتوای یک فایل متنی با سقف حجم."""

    name: ClassVar[str] = "read_file"
    description: ClassVar[str] = (
        "Read a UTF-8 text file inside the allowed directories and return its content. "
        "Large files are truncated; ask for a byte range if you need the rest. "
        "Credential files (.env, ~/.ssh, ...) are never readable."
    )
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    required_parameters: ClassVar[tuple[str, ...]] = ("path",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("max_bytes", "encoding", "start_line", "end_line")
    path_action: ClassVar[str] = "read"
    path_parameters: ClassVar[tuple[str, ...]] = ("path",)

    def validate_input(self, **kwargs: Any) -> bool:
        """اعتبارسنجی محدوده‌ی خط و حجم."""
        super().validate_input(**kwargs)
        bounded_int(
            kwargs.get("max_bytes"), name="max_bytes", minimum=64, maximum=8_000_000, default=self._default_max()
        )
        for key in ("start_line", "end_line"):
            if kwargs.get(key) is not None:
                bounded_int(kwargs.get(key), name=key, minimum=1, maximum=10_000_000)
        return True

    def _default_max(self) -> int:
        """سقف حجم پیش‌فرض از config."""
        return int(getattr(self.config, "max_file_bytes", 512 * 1024) or 512 * 1024)

    async def execute(  # type: ignore[override]
        self,
        path: str,
        *,
        max_bytes: int | None = None,
        encoding: str = "utf-8",
        start_line: int | None = None,
        end_line: int | None = None,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """خواندن فایل (خط‌محور یا بایت‌محور).

        Args:
            path: مسیر فایل.
            max_bytes: سقف حجم خواندن.
            encoding: کدگذاری متن (پیش‌فرض utf-8).
            start_line: شماره‌ی خط شروع (۱-محور) برای خواندن قسمتی از فایل.
            end_line: شماره‌ی خط پایان (مشمول).

        Returns:
            dict شامل ``content``, ``lines``, ``size``, ``truncated``.
        """
        target = self.resolve(path, context=context)

        def _read() -> tuple[bytes, int, bool]:
            size = target.stat().st_size
            limit = max_bytes or self._default_max()
            with target.open("rb") as handle:
                blob = handle.read(limit)
            return blob, size, size > limit

        raw, size, truncated = await asyncio_to_thread(_read)
        if b"\x00" in raw[:4096]:
            return ToolResult.fail(
                f"'{target}' looks like a binary file (NUL byte found); read_file only handles text",
                tool=self.name,
                error_code="binary_file",
                metadata={"path": str(target), "size_bytes": size},
            )
        text = raw.decode(encoding, errors="replace")
        lines = text.splitlines()
        total_lines = len(lines)
        slice_note = ""
        if start_line or end_line:
            first = max(1, int(start_line or 1))
            last = min(total_lines, int(end_line or total_lines))
            if first > last:
                raise ValidationError("start_line must be <= end_line")
            lines = lines[first - 1 : last]
            slice_note = f"lines {first}-{last}"
        preview = "\n".join(lines)
        first_line = int(start_line or 1)
        numbered = "\n".join(f"{index + first_line:>5} | {line}" for index, line in enumerate(lines))
        return ToolResult.ok(
            {
                "content": preview,
                "numbered": numbered,
                "path": str(target),
                "size_bytes": size,
                "size": format_size(size),
                "returned_lines": len(lines),
                "total_lines": total_lines,
                "truncated": truncated,
                "note": slice_note or None,
            },
            tool=self.name,
            metadata={"path": str(target), "truncated": truncated},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("path",),
            properties={
                "path": {
                    "type": "string",
                    "description": "Path to the file (relative to the project root or absolute).",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to read (default from config).",
                    "minimum": 64,
                },
                "encoding": {
                    "type": "string",
                    "description": "Text encoding, e.g. 'utf-8' or 'latin-1'.",
                    "default": "utf-8",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional 1-based first line to return.",
                    "minimum": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional 1-based last line (inclusive).",
                    "minimum": 1,
                },
            },
        )


# ----------------------------------------------------------------------
# نوشتن
# ----------------------------------------------------------------------
@register_tool
class WriteFileTool(FilesystemTool):
    """ساخت/بازنویسی/ضمیمه‌کردن یک فایل متنی (نوشتن اتمیک)."""

    name: ClassVar[str] = "write_file"
    description: ClassVar[str] = (
        "Create or overwrite a UTF-8 text file. Parent directories are created automatically. "
        "Writes are atomic (temp file + rename). Prefer mode='append' for logs and "
        "mode='create' if you must not overwrite an existing file."
    )
    requires_confirmation: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM
    required_parameters: ClassVar[tuple[str, ...]] = ("path", "content")
    optional_parameters: ClassVar[tuple[str, ...]] = ("mode",)
    path_action: ClassVar[str] = "write"
    path_parameters: ClassVar[tuple[str, ...]] = ("path",)

    def validate_input(self, **kwargs: Any) -> bool:
        """بررسی وجود محتوا و حالت نوشتن معتبر."""
        super().validate_input(**kwargs)
        if kwargs.get("content") is None:
            raise ValidationError("content must be a string (use empty string to clear a file)")
        mode = str(kwargs.get("mode") or "overwrite").lower()
        if mode not in {"overwrite", "append", "create"}:
            raise ValidationError("mode must be one of: overwrite, append, create")
        limit = int(getattr(self.config, "max_file_bytes", 512 * 1024) or 512 * 1024)
        if len(str(kwargs["content"]).encode("utf-8")) > limit:
            raise ValidationError(f"content is too large ({len(str(kwargs['content']))} chars > limit {limit} bytes)")
        return True

    async def execute(  # type: ignore[override]
        self,
        path: str,
        content: str,
        *,
        mode: str = "overwrite",
        context: ToolContext | None = None,
    ) -> ToolResult:
        """نوشتن اتمیک محتوا روی دیسک.

        Args:
            path: مسیر فایل مقصد.
            content: متن نهایی.
            mode: ``overwrite`` | ``append`` | ``create``.
            context: زمینه‌ی اجرا.

        Returns:
            metadata شامل مسیر، بایت نوشته‌شده و وضعیت فایل قبلی.

        Raises:
            ValidationError: فایل در حالت ``create`` از قبل وجود داشته باشد.
        """
        target = self.resolve(path, must_exist=False, context=context)
        payload = str(content)
        mode = str(mode or "overwrite").lower()

        def _write() -> dict[str, Any]:
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            if mode == "create" and existed:
                raise ValidationError(f"file already exists: {target}")
            if mode == "append":
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(payload if payload.endswith("\n") or not payload else payload + "\n")
            else:
                temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
                temp.write_text(payload, encoding="utf-8")
                temp.replace(target)
            return {"existed": existed, "bytes": target.stat().st_size, "mode": mode}

        info = await asyncio_to_thread(_write)
        return ToolResult.ok(
            {
                "path": str(target),
                "written_bytes": info["bytes"],
                "created": not info["existed"],
                "mode": info["mode"],
                "size": format_size(info["bytes"]),
            },
            tool=self.name,
            metadata={"path": str(target), "overwritten": info["existed"]},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("path", "content"),
            properties={
                "path": {"type": "string", "description": "Destination file path."},
                "content": {
                    "type": "string",
                    "description": "Full text to write (include the final newline yourself).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "create"],
                    "description": "overwrite = replace, append = add at end, create = fail if the file exists.",
                    "default": "overwrite",
                },
            },
        )


# ----------------------------------------------------------------------
# فهرست
# ----------------------------------------------------------------------
@register_tool
class ListDirectoryTool(FilesystemTool):
    """فهرست کردن محتوای یک دایرکتوری (with size/mtime)."""

    name: ClassVar[str] = "list_directory"
    description: ClassVar[str] = (
        "List files and directories under a path, optionally filtered by a glob pattern "
        "(e.g. '*.py') and recursed with 'depth'. Returns name, type, size and modified time."
    )
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    required_parameters: ClassVar[tuple[str, ...]] = ()
    optional_parameters: ClassVar[tuple[str, ...]] = ("path", "pattern", "depth", "limit")
    path_action: ClassVar[str] = "list"
    path_parameters: ClassVar[tuple[str, ...]] = ("path",)

    async def execute(  # type: ignore[override]
        self,
        path: str = ".",
        *,
        pattern: str = "*",
        depth: int = 1,
        limit: int = 400,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """پیمایش دایرکتوری و ساخت گزارش.

        Args:
            path: دایرکتوری مبدأ (پیش‌فرض: ریشه‌ی پروژه).
            pattern: فیلتر glob.
            depth: عمق پیمایش (۱ = فقط همان پوشه).
            limit: حداکثر تعداد رکورد.

        Returns:
            dict با کلیدهای ``entries``, ``count``, ``truncated``, ``path``.
        """
        base = self.resolve(path or ".", context=context)
        depth = bounded_int(depth, name="depth", minimum=1, maximum=8, default=1)
        limit = bounded_int(limit, name="limit", minimum=1, maximum=5000, default=400)
        glob = str(pattern or "*")

        def _walk() -> tuple[list[dict[str, Any]], bool]:
            entries: list[dict[str, Any]] = []
            truncated = False
            paths: list[Path] = []
            if depth == 1:
                try:
                    paths = sorted(base.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
                except OSError as exc:
                    raise ValidationError(f"cannot list '{base}': {exc}") from exc
            else:
                paths = sorted(base.glob("**/" + glob), key=lambda item: (not item.is_dir(), str(item)))
            for item in paths:
                if depth > 1 and not fnmatch.fnmatch(item.name, glob):
                    continue
                if depth == 1 and glob != "*" and not fnmatch.fnmatch(item.name, glob):
                    continue
                try:
                    stat = item.stat()
                    entry = {
                        "name": str(item.relative_to(base)) if item.is_relative_to(base) else str(item),
                        "type": "dir" if item.is_dir() else ("link" if item.is_symlink() else "file"),
                        "size": format_size(stat.st_size) if item.is_file() else "-",
                        "size_bytes": stat.st_size if item.is_file() else 0,
                        "modified": _iso(stat.st_mtime),
                    }
                except OSError:  # pragma: no cover - فایل حذف‌شده حین پیمایش
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    truncated = True
                    break
            return entries, truncated

        entries, truncated = await asyncio_to_thread(_walk)
        return ToolResult.ok(
            {
                "path": str(base),
                "count": len(entries),
                "entries": entries,
                "truncated": truncated,
                "pattern": glob,
                "depth": depth,
            },
            tool=self.name,
            metadata={"entries": len(entries), "truncated": truncated},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=(),
            properties={
                "path": {"type": "string", "description": "Directory to list (default: project root)."},
                "pattern": {
                    "type": "string",
                    "description": "Glob filter applied to entry names, e.g. '*.py'.",
                    "default": "*",
                },
                "depth": {
                    "type": "integer",
                    "description": "1 = this directory only; up to 8 for recursion.",
                    "minimum": 1,
                    "maximum": 8,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of entries to return.",
                    "minimum": 1,
                    "maximum": 5000,
                },
            },
        )


# ----------------------------------------------------------------------
# حذف
# ----------------------------------------------------------------------
@register_tool
class DeleteFileTool(FilesystemTool):
    """حذف فایل یا دایرکتوری (همیشه نیازمند تأیید)."""

    name: ClassVar[str] = "delete_file"
    description: ClassVar[str] = (
        "Delete a single file, or a directory tree when recursive=true. This is irreversible: "
        "the tool always asks for user confirmation and refuses protected system paths."
    )
    requires_confirmation: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.HIGH
    required_parameters: ClassVar[tuple[str, ...]] = ("path",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("recursive", "dry_run")
    path_action: ClassVar[str] = "delete"
    path_parameters: ClassVar[tuple[str, ...]] = ("path",)

    async def execute(  # type: ignore[override]
        self,
        path: str,
        *,
        recursive: bool = False,
        dry_run: bool = False,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """حذف امن با گزینه‌ی پیش‌نمایش.

        Args:
            path: مسیر فایل/دایرکتوری.
            recursive: حذف درختی دایرکتوری‌ها.
            dry_run: فقط گزارش آنچه حذف می‌شود، بدون حذف.

        Returns:
            dict شامل مسیر حذف‌شده و تعداد/حجم اقلام.
        """
        target = self.resolve(path, context=context)

        def _delete() -> dict[str, Any]:
            if target.is_dir():
                if not recursive:
                    raise ValidationError(f"'{target}' is a directory; pass recursive=true to delete the whole tree")
                items = list(target.rglob("*"))
                size = sum(item.stat().st_size for item in items if item.is_file())
                report = {"kind": "directory", "path": str(target), "entries": len(items), "bytes": size}
                if not dry_run:
                    shutil.rmtree(target)
                return report
            stat = target.stat()
            report = {"kind": "file", "path": str(target), "entries": 1, "bytes": stat.st_size}
            if not dry_run:
                target.unlink()
            return report

        report = await asyncio_to_thread(_delete)
        verb = "would delete" if dry_run else "deleted"
        return ToolResult.ok(
            {
                "action": verb,
                "kind": report["kind"],
                "path": report["path"],
                "entries": report["entries"],
                "size": format_size(report["bytes"]),
                "dry_run": dry_run,
            },
            tool=self.name,
            metadata={"dry_run": dry_run, "freed_bytes": report["bytes"]},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("path",),
            properties={
                "path": {"type": "string", "description": "File or directory to delete."},
                "recursive": {
                    "type": "boolean",
                    "description": "Required to delete a directory (removes the whole tree).",
                    "default": False,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Report what would be deleted without deleting it.",
                    "default": False,
                },
            },
        )


# ----------------------------------------------------------------------
# انتقال
# ----------------------------------------------------------------------
@register_tool
class MoveFileTool(FilesystemTool):
    """جابه‌جایی/تغییر نام فایل یا دایرکتوری (نیازمند تأیید)."""

    name: ClassVar[str] = "move_file"
    description: ClassVar[str] = (
        "Move or rename a file/directory from 'source' to 'destination'. Refuses to overwrite an "
        "existing destination unless overwrite=true, and refuses protected system paths."
    )
    requires_confirmation: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.HIGH
    required_parameters: ClassVar[tuple[str, ...]] = ("source", "destination")
    optional_parameters: ClassVar[tuple[str, ...]] = ("overwrite", "copy")
    path_action: ClassVar[str] = "move"
    path_parameters: ClassVar[tuple[str, ...]] = ("source", "destination")

    def validate_input(self, **kwargs: Any) -> bool:
        """بررسی متفاوت بودن مبدأ و مقصد."""
        super().validate_input(**kwargs)
        if str(kwargs.get("source", "")).strip() == str(kwargs.get("destination", "")).strip():
            raise ValidationError("source and destination are identical")
        return True

    async def execute(  # type: ignore[override]
        self,
        source: str,
        destination: str,
        *,
        overwrite: bool = False,
        copy: bool = False,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """جابه‌جایی (یا کپی) یک مسیر.

        Args:
            source: مبدأ.
            destination: مقصد.
            overwrite: اجازه‌ی بازنویسی مقصد موجود.
            copy: کپی کن و مبدأ را نگه دار.
            context: زمینه‌ی اجرا.

        Returns:
            dict با مسیر جدید و وضعیت.
        """
        src = self.resolve(source, context=context)
        dst = self.resolve(destination, must_exist=False, context=context)

        def _move() -> dict[str, Any]:
            if dst.exists():
                if not overwrite:
                    raise ValidationError(f"destination already exists: {dst} (pass overwrite=true to replace)")
                if dst.is_dir() and not copy:
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            if copy:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
            return {"destination": str(dst), "exists": dst.exists(), "kind": "dir" if dst.is_dir() else "file"}

        info = await asyncio_to_thread(_move)
        return ToolResult.ok(
            {"operation": "copied" if copy else "moved", **info},
            tool=self.name,
            metadata={"source": str(src), "destination": info["destination"]},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("source", "destination"),
            properties={
                "source": {"type": "string", "description": "Existing file or directory."},
                "destination": {"type": "string", "description": "New path (parents are created)."},
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace the destination if it already exists.",
                    "default": False,
                },
                "copy": {
                    "type": "boolean",
                    "description": "Copy instead of moving (keeps the source).",
                    "default": False,
                },
            },
        )


# ----------------------------------------------------------------------
# جست‌وجو
# ----------------------------------------------------------------------
@register_tool
class SearchFilesTool(FilesystemTool):
    """جست‌وجوی نام فایل و محتوای متنی در یک درخت."""

    name: ClassVar[str] = "search_files"
    description: ClassVar[str] = (
        "Search file names (glob) and optionally file contents (substring or regex) under a "
        "directory. Binary files and files larger than max_file_bytes are skipped."
    )
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    required_parameters: ClassVar[tuple[str, ...]] = ("query",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("path", "glob", "in_content", "regex", "limit", "ignore_case")
    path_action: ClassVar[str] = "list"
    path_parameters: ClassVar[tuple[str, ...]] = ("path",)

    async def execute(  # type: ignore[override]
        self,
        query: str,
        *,
        path: str = ".",
        glob: str = "**/*",
        in_content: bool = False,
        regex: bool = False,
        limit: int = 60,
        ignore_case: bool = True,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """اجرای جست‌وجو.

        Args:
            query: رشته یا الگوی regex.
            path: ریشه‌ی جست‌وجو.
            glob: الگوی فیلتر فایل‌ها.
            in_content: جست‌وجو داخل محتوای فایل‌ها.
            regex: تفسیر query به‌عنوان regex.
            limit: سقف نتایج.
            ignore_case: بی‌توجهی به بزرگی/کوچکی حروف.
            context: زمینه‌ی اجرا.

        Returns:
            dict با فهرست نتایج (path, line, match).
        """
        text = str(query or "").strip()
        if not text:
            raise ValidationError("query must not be empty")
        limit = bounded_int(limit, name="limit", minimum=1, maximum=500, default=60)
        base = self.resolve(path or ".", context=context)
        max_bytes = int(getattr(self.config, "max_file_bytes", 512 * 1024) or 512 * 1024)
        flags = re.IGNORECASE if ignore_case else 0
        try:
            needle: Any = re.compile(text if regex else re.escape(text), flags)
        except re.error as exc:
            raise ValidationError(f"invalid regex '{text}': {exc}") from exc
        plain = "" if regex else text.lower() if ignore_case else text

        def _search() -> tuple[list[dict[str, Any]], bool]:
            results: list[dict[str, Any]] = []
            truncated = False
            candidates = [item for item in sorted(base.glob(glob)) if item.is_file()]
            for item in candidates:
                name_hit = (
                    bool(needle.search(item.name))
                    if regex
                    else ((plain in item.name.lower()) if ignore_case else (text in item.name))
                )
                matched = False
                if not in_content:
                    # فقط نام فایل‌ها را جست‌وجو می‌کنیم
                    if name_hit:
                        results.append({"path": _rel(item, base), "match": item.name})
                        matched = True
                else:
                    try:
                        if item.stat().st_size > max_bytes:
                            continue
                        with item.open("r", encoding="utf-8", errors="replace") as handle:
                            for line_no, line in enumerate(handle, start=1):
                                if (
                                    needle.search(line)
                                    if regex
                                    else ((plain in line.lower()) if ignore_case else (text in line))
                                ):
                                    results.append(
                                        {"path": _rel(item, base), "line": line_no, "match": line.strip()[:240]}
                                    )
                                    matched = True
                                    break
                            if not matched and name_hit:
                                results.append(
                                    {"path": _rel(item, base), "match": item.name, "note": "filename match"}
                                )
                                matched = True
                    except OSError:  # pragma: no cover - دسترسی/فایل حذف‌شده
                        continue
                if matched and len(results) >= limit:
                    truncated = True
                    break
                if len(results) >= limit:
                    truncated = True
                    break
            return results, truncated

        results, truncated = await asyncio_to_thread(_search)
        return ToolResult.ok(
            {
                "query": text,
                "root": str(base),
                "mode": "content+name" if in_content else "name",
                "matches": results,
                "count": len(results),
                "truncated": truncated,
            },
            tool=self.name,
            metadata={"count": len(results), "truncated": truncated},
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("query",),
            properties={
                "query": {"type": "string", "description": "Text or regex to look for."},
                "path": {"type": "string", "description": "Directory to search in (default: project root)."},
                "glob": {
                    "type": "string",
                    "description": "Only consider files matching this glob, e.g. '**/*.py'.",
                    "default": "**/*",
                },
                "in_content": {
                    "type": "boolean",
                    "description": "Search inside file contents instead of only file names.",
                    "default": False,
                },
                "regex": {"type": "boolean", "description": "Treat query as a regular expression.", "default": False},
                "limit": {
                    "type": "integer",
                    "description": "Maximum matches to return.",
                    "minimum": 1,
                    "maximum": 500,
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive matching.", "default": True},
            },
        )


# ----------------------------------------------------------------------
# کمکی‌ها
# ----------------------------------------------------------------------
async def asyncio_to_thread(func: Any) -> Any:
    """اجرای تابع blocking در thread (wrapper کوتاه و قابل monkeypatch برای تست)."""
    from src.utils.helpers import run_in_thread

    return await run_in_thread(func)


def _rel(path: Path, base: Path) -> str:
    """نسبی‌سازی امن مسیر برای نمایش."""
    try:
        return str(path.relative_to(base))
    except ValueError:  # pragma: no cover - مسیر symlink خارج از root
        return str(path)


def _iso(epoch: float) -> str:
    """تبدیل timestamp به تاریخ/ساعت خوانا."""
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

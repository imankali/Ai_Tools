"""ساخت یک ابزار سفارشی در زمان اجرا و دادنش به ایجنت.

این همان مسیری است که ایجنت برای «خودابزاری» استفاده می‌کند، اما این‌جا دستی و
قابل‌بازبینی است. ابزار ثبت‌شده با `@register_tool` در همان پروسه در دسترس است؛
برای ماندن در باینلی/سایر پروسه‌ها، فایل را در پوشه‌ی `AGENT_HUB_TOOL_DIRS` بگذارید.

    python examples/02_custom_tool.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_config
from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import ToolRegistry, discover_tools, register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.validators import clean_text


@register_tool
class WordCountTool(BaseTool):
    """شمارش واژه/خط یک فایل متنی (فقط‌خواندنی، بی‌ریسک)."""

    name: ClassVar[str] = "word_count"
    description: ClassVar[str] = "Count words, lines and characters of a text file inside the project."
    category: ClassVar[ToolCategory] = ToolCategory.DEVELOPER
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    required_parameters: ClassVar[tuple[str, ...]] = ("path",)

    def _inside_project(self, path: str) -> tuple[Path | None, str | None]:
        """مسیر را به ریشه‌ی پروژه قفل می‌کند (sync: عملیات path نباید در async باشد)."""
        base = Path(str(getattr(self.config, "project_root", Path.cwd()))).resolve(strict=False)
        target = Path(base / path).resolve(strict=False)
        if not str(target).startswith(str(base)):
            return None, "path escapes the project root"
        return target, None

    async def execute(  # type: ignore[override]
        self, path: str = "", *, context: ToolContext | None = None
    ) -> ToolResult:
        """خواندن فایل و برگرداندن شمارش‌ها (بدون محتوای فایل)."""
        target = clean_text(path, max_length=500)
        if not target:
            return ToolResult.fail("path must not be empty", tool=self.name, error_code="invalid_input")
        file, escape = self._inside_project(target)
        if file is None:
            return ToolResult.fail(escape or "invalid path", tool=self.name, error_code="blocked")
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.fail(f"cannot read {target}: {exc}", tool=self.name, error_code="tool_error")
        return ToolResult.ok(
            {"path": target, "words": len(text.split()), "lines": text.count("\n") + 1, "chars": len(text)},
            tool=self.name,
        )

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("path",),
            properties={"path": {"type": "string", "description": "Path relative to the project root."}},
        )


async def main() -> None:
    """اول ابزار را مستقیم اجرا می‌کنیم، بعد در یک ایجنت نشان می‌دهیم."""
    discover_tools()
    assert "word_count" in ToolRegistry.names()

    tool = ToolRegistry.get("word_count", config=get_config())
    assert tool is not None, "the tool was not registered"
    result = await tool.run({"path": "README.md"})
    print("direct call:", result.success, result.data or result.error)

    bad = await tool.run({"path": "/etc/passwd"})
    print("guarded call:", bad.success, bad.error_code, "-", bad.error)

    from src.agent import UniversalAgent  # noqa: PLC0415 - فقط برای نمایش

    print("\nagent can now use it — expose it like any other tool:")
    print(list(UniversalAgent(config=get_config(), tools=["word_count"]).tool_names))


if __name__ == "__main__":
    asyncio.run(main())

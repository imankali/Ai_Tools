"""Runtime hook: درست‌کردن مسیرها در باینلی فریزشده.

دو کار انجام می‌دهد:

1. ``PROJECT_ROOT`` را به پوشه‌ی اجرای کاربر قفل می‌کند (نه `_MEIPASS`)؛ در غیر
   این صورت `.env` و ابزارهای کاربر داخل پوشه‌ی موقت سرچ می‌شوند.
2. اگر کاربر کنار باینلی پوشه‌ی `tools/` بگذارد، همان را به مسیر import اضافه
   می‌کند (افزونه‌نویسی بدون بازسازی باینلی).
"""

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):  # pragma: no cover - فقط داخل باینلی اجرا می‌شود
    exe_dir = Path(sys.executable).resolve().parent
    os.environ.setdefault("PROJECT_ROOT", str(Path.cwd()))
    plugins = exe_dir / "tools"
    if plugins.is_dir():
        sys.path.insert(0, str(plugins))
        # ToolRegistry.load_plugins() همین متغیر را می‌خواند و فایل‌های *.py را import می‌کند
        existing = os.environ.get("AGENT_HUB_TOOL_DIRS", "")
        os.environ["AGENT_HUB_TOOL_DIRS"] = f"{plugins}{os.pathsep}{existing}" if existing else str(plugins)

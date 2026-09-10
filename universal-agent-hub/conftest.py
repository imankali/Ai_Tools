"""pytest bootstrap for the project.

The package lives in ``src/`` (as specified by the project layout), so tests are
run with the project root on ``sys.path`` and ``src`` is importable. This file
also neutralises a real ``.env`` during testing so local secrets and paths never
leak into unit tests. Shared fixtures live in :mod:`tests.conftest`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# ------------------------------------------------------------ test isolation
# 1) هیچ فایل .env واقعی بارگذاری نشود  2) تنظیمات پیش‌فرضِ پایدار و آفلاین
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-000000")
os.environ["MODEL_NAME"] = "gpt-6-astra"
os.environ["MODEL_FALLBACKS"] = "gpt-4o-mini,gpt-4o"
os.environ["LOG_FILE"] = ""
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["RICH_CONSOLE"] = "false"
os.environ["SEARCH_BACKEND"] = "offline"
os.environ["ENABLE_CONFIRMATION"] = "true"
os.environ["ALLOWED_DIRECTORIES"] = ""
os.environ["UNRESTRICTED_FILESYSTEM"] = "false"
os.environ["EVENTS_LOG_FILE"] = ""
# حافظه و گزارش‌ها روی home کاربر می‌نویسند؛ در تست‌ها پیش‌فرض خاموش‌اند تا
# هیچ آزمونی ~/.universal-agent-hub را لمس نکند (fixtures مخصوص، tmp_path می‌دهند)
os.environ["MEMORY_ENABLED"] = "0"
os.environ["REPORTS_ENABLED"] = "0"
os.environ["MODEL_NAME"] = "gpt-6-astra"  # تا .env کاربر هرگز وارد تست‌ها نشود
os.environ["TAVILY_API_KEY"] = ""
os.environ.pop("OPENAI_BASE_URL", None)

"""شروع سریع: یک ایجنت با همه‌ی ابزارها، یک سؤال، یک پاسخ.

اجرا (از ریشه‌ی پروژه، بعد از `make install` و تنظیم `OPENAI_API_KEY` در `.env`):

    python examples/01_quickstart.py

اگر کلید تنظیم نباشد، مثال بدون crash می‌گوید چه چیزی کم است.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # اجرای مستقیم از هر پوشه‌ای کار کند
    sys.path.insert(0, str(ROOT))


from src.config import get_config
from src.core.agent_factory import AgentFactory
from src.core.tool_registry import discover_tools


async def main() -> int:
    """ساخت ایجنت، پرسیدن یک سؤال واقعی و چاپ نتیجه‌ی کامل."""
    config = get_config()
    if not config.openai_api_key:
        print("OPENAI_API_KEY تنظیم نشده — `cp .env.example .env` و کلید را بگذارید.")
        return 1
    discover_tools()
    agent = AgentFactory.create("generalist", config=config)
    print(f"model: {agent.model_info['model']} · tools: {len(agent.tool_names)}")

    result = await agent.ask("what operating system am I running on? use the os_info tool.")
    print("\n--- answer ---")
    print(result.text or f"(no text) error={result.error}")
    print(
        f"\niterations={result.iterations} tools={result.tool_names} "
        f"tokens={result.usage.total_tokens} duration={result.duration_ms}ms"
    )
    for record in result.tool_calls:
        ok = "ok" if (record.result and record.result.success) else "failed"
        print(f"  {record.tool}: {ok} ({record.duration_ms}ms)")

    # ایجنت حالا یادش هست؛ دفعه‌ی بعد لازم نیست دوباره os_info بزند
    stats = agent.memory_summary()
    if stats.get("enabled"):
        print(f"\nmemory: {stats['records']} records at {stats['path']}")
    await agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

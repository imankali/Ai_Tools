"""حرف زدن با سرور اپ (همان مسیری که گوشی/پنل می‌روند).

اول سرور را روشن کنید:

    agent-hub --serve --port 8765            # loopback
    # یا با توکن:  agent-hub --serve --lan --print-token

سپس:

    BASE_URL=http://127.0.0.1:8765 TOKEN=… python examples/05_server_client.py

اگر سرور روشن نباشد، مثال پیام خوانا می‌دهد (نه traceback).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("TOKEN", "")


def headers() -> dict[str, str]:
    """هدرهای درخواست (توکن فقط وقتی لازم است)."""
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


async def get(session: Any, path: str) -> Any:
    """GET و بازگرداندن JSON (خطا → dict با همان ساختار سرور)."""
    async with session.get(BASE_URL + path, headers=headers()) as response:
        text = await response.text()
        try:
            return {"status": response.status, **json.loads(text)}
        except json.JSONDecodeError:
            return {"status": response.status, "raw": text[:400]}


async def main() -> int:
    """کشف، اجرا، و خواندن گزارش از سرور."""
    try:
        import aiohttp
    except ImportError:  # pragma: no cover
        print("aiohttp لازم است: pip install aiohttp")
        return 1

    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            status = await get(session, "/api/status")
        except aiohttp.ClientError as exc:
            print(f"سرور در {BASE_URL} در دسترس نیست ({type(exc).__name__}: {exc})")
            print("اول `agent-hub --serve` را اجرا کنید.")
            return 1
        if status.get("status") == 401:
            print("توکن لازم است — TOKEN=… را تنظیم کنید.")
            return 1

        print(
            f"server: ready={status.get('ready')} model={status.get('model')} "
            f"tools={status.get('tool_count')} sessions={len(status.get('sessions') or [])}"
        )
        if not status.get("ready"):
            print("  hint:", status.get("ready_hint"))

        report = await get(session, "/api/reports?days=7")
        if report.get("text"):
            print("\n" + report["text"])
        elif report.get("error"):
            print("\nreports unavailable:", report["error"])

        memory = await get(session, "/api/memory?limit=5")
        records = memory.get("records") or []
        print(f"\nmemory: {len(records)} records")
        for item in records[:5]:
            print(f"  {item['id']} [{item['kind']}] {item['content'][:80]}")

        run = await session.post(
            BASE_URL + "/api/run",
            json={"prompt": "list the three largest files in the project", "profile": "generalist"},
            headers={**headers(), "Content-Type": "application/json"},
        )
        payload = await run.json()
        print(f"\nrun: status={run.status} ok={payload.get('ok')} session={payload.get('session_id')}")
        print((payload.get("text") or payload.get("error") or "")[:700])
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

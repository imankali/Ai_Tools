"""حافظه‌ی بلندمدت: نوشتن، خواندن، جست‌وجو، export/import.

مثال روی یک فایل موقت کار می‌کند (تا home کاربر دست‌نخورده بماند). برای حالت واقعی
همین `Config(memory_dir=…)` را بردارید: پیش‌فرض `~/.universal-agent-hub/memory/memory.jsonl`.

    python examples/03_memory_notes.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Config
from src.core.memory import MEMORY_KINDS, AgentMemory, reset_memory_cache


def show(memory: Any, title: str) -> None:
    """چاپ رکوردها + آمار، به‌شکلی که در پنل گزارش هم استفاده می‌شود."""
    print(f"\n{title}")
    for item in memory.all():
        pin = " ·pinned" if item.pinned else ""
        tags = (" ·" + ",".join(item.tags)) if item.tags else ""
        print(f"  {item.id} [{item.kind}]{tags}{pin} :: {item.content[:90]}")
    stats = memory.stats()
    print(
        f"  → records={stats['records']} pinned={stats['pinned']} hits={stats['total_hits']} by_kind={stats['by_kind']}"
    )


def main() -> None:
    """یک دور کامل روی حافظه."""
    folder = Path(tempfile.mkdtemp(prefix="agent-hub-memory-"))
    config = Config(openai_api_key="sk-example-only", memory_dir=folder, memory_max_records=200)
    reset_memory_cache()
    memory = AgentMemory.for_config(config)
    assert memory is not None

    memory.add("answer in Persian, keep it short", kind="preference", tags=["lang:fa"], pin=True)
    memory.add(
        "deploy = run scripts/deploy.sh on host prod1 after pytest is green",
        kind="procedure",
        tags=["project:webshop"],
    )
    memory.add("chose Caddy as the TLS terminator (single binary, auto https)", kind="decision")
    memory.add(
        "apply for a payment gateway: gather commercial code + tax file first", kind="plan", tags=["area:payments"]
    )
    memory.add("next: ask the user which gateway brand they prefer", kind="plan")
    # تکرارِ عین‌متن رکورد تازه نمی‌سازد، همان را تقویت می‌کند
    memory.add("answer in Persian, keep it short", kind="preference", confidence=0.95)
    # کلید API هرگز ذخیره نمی‌شود (redaction خودکار)
    memory.add("gateway api key is sk-abcdefghijklmnopqrstuvwxyz123456", kind="note")
    show(memory, "stored notes")

    print("\nsearch('gateway documents') →", [item.id for item in memory.search("gateway documents")])
    print("plans →", [item.content[:60] for item in memory.plans()])

    block = memory.context_block(max_chars=900)
    print("\nsystem prompt block that the agent will receive:\n" + block)

    export = folder / "memory-export.json"
    print(f"\nexported {memory.export_json(export)} records → {export.name}")
    other = AgentMemory(folder / "second" / "memory.jsonl")
    print(
        f"imported into a new store: {other.import_json(export)} records; second import adds {other.import_json(export)} (dedupe)"
    )
    try:
        Path("/etc/definitely-not-writable.json").write_text("")
    except OSError as exc:
        print(f"\n(writable check demo: OSError as expected → {type(exc).__name__})")
    print("\nkinds:", ", ".join(MEMORY_KINDS))
    reset_memory_cache()


if __name__ == "__main__":
    main()

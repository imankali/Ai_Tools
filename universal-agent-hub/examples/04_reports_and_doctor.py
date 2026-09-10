"""خودسنجی محیط + گزارش فعالیت، بدون تماس با مدل.

python examples/04_reports_and_doctor.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cli import doctor_checks
from src.config import Config
from src.core.memory import AgentMemory, reset_memory_cache
from src.core.reports import ActivityRecorder, build_report, render_report_text
from src.utils.safety import SafetyGuard


def print_checks(config: Any) -> int:
    """همان ردیف‌هایی که `agent-hub --doctor` چاپ می‌کند (بدون Rich)."""
    failed = 0
    for name, status, detail in doctor_checks(config):
        failed += status == "fail"
        print(f"  {name:<16} {status:<5} {detail}")
    return 1 if failed else 0


def main() -> None:
    """doctor روی config واقعی، بعد یک گزارش ساختگی روی tmp."""
    from src.config import get_config

    print("doctor (real config):")
    code = print_checks(get_config())

    folder = Path(tempfile.mkdtemp(prefix="agent-hub-report-"))
    config = Config(
        openai_api_key="sk-example-only",
        project_root=folder,
        memory_dir=folder / "memory",
        activity_log_file=folder / "activity.jsonl",
    )
    reset_memory_cache()
    ActivityRecorder.clear_cache()

    recorder = ActivityRecorder.for_config(config)
    assert recorder is not None
    recorder.record(
        {
            "kind": "run",
            "ok": True,
            "iterations": 3,
            "tools": ["read_file", "terminal_run", "memory_write"],
            "calls": [
                {"tool": "read_file", "ok": True, "code": "", "duration_ms": 12},
                {"tool": "terminal_run", "ok": False, "code": "declined", "duration_ms": 90},
                {"tool": "memory_write", "ok": True, "code": "", "duration_ms": 4},
            ],
            "duration_ms": 14300,
            "tokens": 8123,
            "model": config.model_name,
            "profile": "developer",
            "prompt": "find dead code and remove it",
        }
    )
    memory = AgentMemory.for_config(config)
    assert memory is not None
    memory.add("user denied `rm` without seeing the file list — ask first", kind="preference", pin=True)
    memory.add("finish dead-code sweep: check scripts/ too", kind="plan", tags=["project:cleanup"])

    report = build_report(config, days=7)
    print("\n" + render_report_text(report, title="Example activity report"))
    print("\nsummary dict:")
    for key in ("runs", "safety", "plans", "warnings", "files"):
        print(f"  {key}: {report[key]}")

    guard = SafetyGuard.from_config(config)
    print("\nsafety verdicts the guard would give:")
    for command in ("echo hi", "git reset --hard HEAD~1", "rm -rf /"):
        decision = guard.assess_command(command)
        verdict = "blocked" if decision.blocked else ("confirm" if decision.requires_confirmation else "allowed")
        print(f"  {command:<28} {verdict:<8} risk={decision.risk.value}")
    print(f"\ndoctor exit code would have been: {code}")
    reset_memory_cache()


if __name__ == "__main__":
    main()

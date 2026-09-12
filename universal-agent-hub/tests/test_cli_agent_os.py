"""تست دستورهای CLI مربوط به زیرسیستم‌های Agent-OS.

اینها تست *entry point واقعی* هستند (``main([...])``)، نه فراخوانی مستقیم توابع —
تا مسیر argparse → dispatch → سرویس واقعاً اجرا شود.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.cli import main


@pytest.fixture
def env(config: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """تنظیمات با مسیرهای state ایزوله."""
    (tmp_path / "memory").mkdir(exist_ok=True)
    for key, value in {
        "ROUTINES_FILE": str(tmp_path / "routines.json"),
        "NOTIFICATIONS_FILE": str(tmp_path / "notes.jsonl"),
        "AUDIT_FILE": str(tmp_path / "audit.jsonl"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "SKILLS_DIRS": str(tmp_path / "skills"),
        "MEMORY_DIR": str(tmp_path / "memory"),
        # conftest ریشه عمداً MEMORY_ENABLED=0 می‌گذارد تا هیچ تستی به
        # ~/.universal-agent-hub دست نزند. اینجا حافظه را روی tmp_path روشن
        # می‌کنیم تا مسیر بکاپِ state واقعاً پوشش داده شود.
        "MEMORY_ENABLED": "1",
    }.items():
        monkeypatch.setenv(key, value)
    from src.config import reset_config

    reset_config()
    yield tmp_path
    reset_config()


def test_routines_empty(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--routines"]) == 0
    assert "no routines defined" in capsys.readouterr().out


def test_schedule_requires_trigger(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--schedule", "x", "--prompt", "p"]) == 2
    assert "needs one of" in capsys.readouterr().out


def test_schedule_requires_prompt(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--schedule", "x", "--cron", "* * * * *"]) == 2
    assert "needs --prompt" in capsys.readouterr().out


def test_schedule_invalid_cron(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--schedule", "x", "--cron", "bad", "--prompt", "p"]) == 2
    assert "error:" in capsys.readouterr().out


def test_schedule_cron_then_list(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--schedule", "nightly", "--cron", "0 22 * * *", "--prompt", "report"]) == 0
    assert "created routine 'nightly'" in capsys.readouterr().out
    assert main(["--routines"]) == 0
    out = capsys.readouterr().out
    assert "nightly" in out and "cron" in out


def test_schedule_updates_existing(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--schedule", "n", "--every", "60", "--prompt", "a"])
    capsys.readouterr()
    assert main(["--schedule", "n", "--every", "120", "--prompt", "b"]) == 0
    assert "updated routine 'n'" in capsys.readouterr().out


def test_schedule_webhook_prints_curl(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--schedule", "hook", "--webhook", "deploy", "--prompt", "p"]) == 0
    assert "/hooks/deploy" in capsys.readouterr().out


def test_unschedule(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--schedule", "gone", "--every", "60", "--prompt", "p"])
    capsys.readouterr()
    assert main(["--unschedule", "gone"]) == 0
    assert "removed routine 'gone'" in capsys.readouterr().out
    assert main(["--unschedule", "gone"]) == 1
    assert "no routine named" in capsys.readouterr().out


def test_notifications_empty(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--notifications"]) == 0
    assert "no notifications" in capsys.readouterr().out


def test_notifications_clear(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--clear-notifications"]) == 0
    assert "cleared" in capsys.readouterr().out


def test_skills_empty(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--skills"]) == 0
    assert "no skills installed" in capsys.readouterr().out


def test_skills_listed(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = env / "skills" / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: demo\ndescription: D\n---\nbody", encoding="utf-8")
    assert main(["--skills"]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "1/1 enabled" in out


def test_backup_create_and_list(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """فایل حافظه *بعد از* ساخت hub نوشته می‌شود و باید همچنان بکاپ شود."""
    memory = env / "memory" / "memory.jsonl"
    memory.write_text('{"id":"m1"}\n', encoding="utf-8")
    assert main(["--backup"]) == 0
    assert "created backup" in capsys.readouterr().out
    assert main(["--backups"]) == 0
    assert "backup(s)" in capsys.readouterr().out


def test_backup_picks_up_late_files(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """رگرسیون: منابع در لحظه‌ی create بازخوانی می‌شوند، نه فقط در __init__."""
    from src.config import get_config
    from src.core.services import ServiceHub

    hub = ServiceHub(get_config())
    assert "memory" not in hub.backup.sources  # هنوز فایلی وجود ندارد
    (env / "memory" / "memory.jsonl").write_text('{"id":"late"}\n', encoding="utf-8")
    info = hub.backup.create(note="late file")
    assert "memory" in info.members


def test_backups_empty(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--backups"]) == 0
    assert "no backups" in capsys.readouterr().out


def test_diagnostics(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--diagnostics"]) == 0
    out = capsys.readouterr().out
    assert "audit chain" in out
    assert "injection rules" in out
    assert "state files" in out


def test_diagnostics_json(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert main(["--diagnostics", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["audit"]["chain"]["ok"] is True

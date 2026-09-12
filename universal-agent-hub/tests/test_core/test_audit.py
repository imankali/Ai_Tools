"""تست لاگ ممیزی زنجیره‌هش‌شده."""

from __future__ import annotations

import json
from pathlib import Path

from src.core.audit import GENESIS_SHA, AuditDecision, AuditEntry, AuditLog, compute_sha


def make_log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


def test_new_log_is_empty_and_valid(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    assert len(log) == 0
    assert log.head == GENESIS_SHA
    assert log.verify_chain().ok is True


def test_record_appends_and_chains(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    first = log.record("agent", "tool.terminal_run", "ls", AuditDecision.ALLOWED)
    second = log.record("user", "tool.write_file", "/tmp/a", "confirmed")
    assert first.prev_sha == GENESIS_SHA
    assert second.prev_sha == first.sha
    assert log.head == second.sha
    assert len(log) == 2
    assert log.verify_chain().ok is True


def test_unknown_decision_falls_back_to_info(tmp_path: Path) -> None:
    entry = make_log(tmp_path).record("a", "b", decision="nonsense")
    assert entry.decision is AuditDecision.INFO


def test_secrets_are_redacted_in_detail(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    entry = log.record("agent", "x", api_key="sk-secret", nested={"password": "hunter2"}, other="visible")
    assert entry.detail["api_key"] == "***redacted***"
    assert entry.detail["nested"]["password"] == "***redacted***"
    assert entry.detail["other"] == "visible"
    assert "hunter2" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_verify_detects_payload_edit(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("agent", "a")
    log.record("agent", "b")
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["action"] = "hacked"
    lines[0] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    verdict = AuditLog(path).verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at == 0
    assert "sha mismatch" in verdict.reason


def test_verify_detects_removed_entry(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for index in range(4):
        log.record("agent", f"action-{index}")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n", encoding="utf-8")
    verdict = AuditLog(path).verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at == 1


def test_verify_detects_unparsable_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record("agent", "a")
    path.write_text("not json at all\n", encoding="utf-8")
    verdict = AuditLog(path).verify_chain()
    assert verdict.ok is False
    assert "unparsable" in verdict.reason


def test_malformed_line_skipped_on_resume(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("agent", "a")
    path.write_text(path.read_text(encoding="utf-8") + "garbage\n", encoding="utf-8")
    reopened = AuditLog(path)
    assert len(reopened) == 1
    assert reopened.head != GENESIS_SHA


def test_entries_and_query(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record("agent", "tool.a", decision="allowed")
    log.record("user", "tool.b", decision="denied")
    log.record("agent", "tool.c", decision="denied")
    assert len(log.entries()) == 3
    assert log.entries()[0].action == "tool.c"  # tail-first
    assert len(log.entries(limit=2)) == 2
    denied = log.query(decision="denied")
    assert {e.action for e in denied} == {"tool.b", "tool.c"}
    assert len(log.query(actor="agent")) == 2
    assert len(log.query(action="tool.b")) == 1


def test_stats(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record("agent", "tool.a", decision="allowed")
    log.record("agent", "tool.a", decision="denied")
    stats = log.stats()
    assert stats["entries"] == 2
    assert stats["by_decision"]["allowed"] == 1
    assert stats["top_actions"]["tool.a"] == 2
    assert stats["bytes"] > 0


def test_rotation_resets_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=4096)
    for index in range(200):
        log.record("agent", f"action-{index}", "x" * 200)
    assert (tmp_path / "audit.jsonl.1").exists()
    assert log.verify_chain().ok is True


def test_export_json(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.record("agent", "a")
    count = log.export_json(tmp_path / "out.json")
    assert count == 1
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert data["entries"][0]["action"] == "a"


def test_compute_sha_is_stable() -> None:
    entry = AuditEntry(index=0, ts="2026-01-01T00:00:00", actor="a", action="b")
    assert compute_sha(entry) == compute_sha(entry.model_copy())
    other = entry.model_copy(update={"action": "c"})
    assert compute_sha(entry) != compute_sha(other)


def test_missing_file_verifies_ok(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "missing" / "audit.jsonl")
    path = log.path
    path.unlink(missing_ok=True)
    assert log.verify_chain().ok is True
    assert log.verify_chain().entries == 0


def test_index_continues_correctly_after_malformed_line(tmp_path: Path) -> None:
    """رگرسیون: یک خط خراب نباید index رکورد بعدی را جابه‌جا کند.

    خودِ ``verify_chain`` باید روی خط غیرقابل‌پارس خطا بدهد — این درست است،
    چون یک خط خراب یعنی یا خرابی یا دستکاری. چیزی که نباید بشکند،
    پیوستگی ``index`` رکوردهای *معتبر* است.
    """
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record("agent", "a")
    path.write_text(path.read_text(encoding="utf-8") + "garbage\n", encoding="utf-8")
    reopened = AuditLog(path)
    assert len(reopened) == 1
    third = reopened.record("agent", "c")
    assert third.index == 1
    assert reopened.verify_chain().ok is False  # خط خراب هنوز آنجاست
    # رکوردهای معتبر، زنجیره‌ی خودشان را درست نگه داشته‌اند:
    valid = list(reopened.iter_entries())
    assert [entry.index for entry in valid] == [0, 1]
    assert valid[1].prev_sha == valid[0].sha


def test_clean_log_verifies_after_rotation(tmp_path: Path) -> None:
    """بعد از چرخش، زنجیره از genesis شروع می‌شود و معتبر است."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=4096)
    for index in range(150):
        log.record("agent", f"a-{index}", "y" * 300)
    log.record("agent", "final")
    verdict = log.verify_chain()
    assert verdict.ok is True, verdict.reason

"""تست مرکز اعلان‌ها."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.event_bus import EventBus
from src.core.notifications import Notification, NotificationCenter, NotificationSeverity


@pytest.fixture
def center(tmp_path: Path) -> NotificationCenter:
    return NotificationCenter(tmp_path / "notes.jsonl", max_records=50)


def test_push_and_list(center: NotificationCenter) -> None:
    note = center.push("routine", "Nightly done", "all good", severity="success", source="scheduler")
    assert note.id
    assert note.severity is NotificationSeverity.SUCCESS
    listed = center.list()
    assert [n.id for n in listed] == [note.id]
    assert center.unread_count() == 1


def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "notes.jsonl"
    NotificationCenter(path).push("system", "hello")
    reopened = NotificationCenter(path)
    assert len(reopened.list()) == 1
    assert reopened.list()[0].title == "hello"


def test_mark_read(center: NotificationCenter) -> None:
    note = center.push("system", "a")
    assert center.mark_read(note.id) is True
    assert center.mark_read(note.id) is False  # قبلاً خوانده شده
    assert center.mark_read("nope") is False
    assert center.unread_count() == 0


def test_mark_all_read(center: NotificationCenter) -> None:
    center.push("system", "a")
    center.push("system", "b")
    assert center.mark_all_read() == 2
    assert center.mark_all_read() == 0


def test_clear(center: NotificationCenter) -> None:
    center.push("system", "a")
    note = center.push("system", "b")
    center.mark_read(note.id)
    assert center.clear(read_only=True) == 1
    assert len(center.list()) == 1
    assert center.clear() == 1
    assert center.list() == []


def test_filters(center: NotificationCenter) -> None:
    center.push("routine", "a", severity="error")
    center.push("tool", "b", severity="info")
    assert len(center.list(severity="error")) == 1
    assert len(center.list(kind="tool")) == 1
    assert len(center.list(limit=1)) == 1


def test_max_records_evicts(tmp_path: Path) -> None:
    small = NotificationCenter(tmp_path / "n.jsonl", max_records=20)
    for index in range(30):
        small.push("system", f"n{index}")
    assert len(small.list(limit=100)) == 20


def test_unknown_severity_falls_back(center: NotificationCenter) -> None:
    note = center.push("system", "x", severity="nonsense")
    assert note.severity is NotificationSeverity.INFO


def test_secrets_redacted(center: NotificationCenter) -> None:
    note = center.push("system", "key sk-abcdefghijklmnopqrstuvwxyz123456")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in note.title


def test_sink_receives_and_errors_are_swallowed(center: NotificationCenter) -> None:
    seen: list[str] = []
    center.add_sink(lambda note: seen.append(note.title))

    def broken(_: Notification) -> None:
        raise RuntimeError("boom")

    center.add_sink(broken)
    center.push("system", "ok")
    assert seen == ["ok"]


def test_webhook_failure_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    center = NotificationCenter(tmp_path / "n.jsonl", webhook_url="https://example.test/hook")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("no network")

    monkeypatch.setattr("src.core.notifications.requests.post", boom)
    assert center.push("system", "x").title == "x"


def test_webhook_posts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    center = NotificationCenter(tmp_path / "n.jsonl", webhook_url="https://example.test/hook")
    sent: list[dict] = []

    class Response:
        status_code = 200

    monkeypatch.setattr(
        "src.core.notifications.requests.post",
        lambda url, json=None, timeout=None, headers=None: sent.append(json) or Response(),
    )
    center.push("system", "hello")
    assert sent and sent[0]["title"] == "hello"


def test_stats(center: NotificationCenter) -> None:
    center.push("routine", "a", severity="error")
    center.push("tool", "b", severity="info")
    stats = center.stats()
    assert stats["total"] == 2
    assert stats["unread"] == 2
    assert stats["by_kind"]["routine"] == 1
    assert stats["webhook"] is False


def test_bus_bridge(tmp_path: Path) -> None:
    center = NotificationCenter(tmp_path / "n.jsonl")
    bus = EventBus()
    assert center.attach_bus(bus) is not None
    bus.publish_nowait("tool.blocked", {"tool": "terminal_run"}, source="test")
    titles = [n.title for n in center.list()]
    assert any("terminal_run" in title for title in titles)
    assert center.detach_bus(bus) is True
    assert center.detach_bus(bus) is False


def test_bus_bridge_ignores_unmapped_events(tmp_path: Path) -> None:
    center = NotificationCenter(tmp_path / "n.jsonl")
    bus = EventBus()
    center.attach_bus(bus)
    bus.publish_nowait("some.unknown.event", {}, source="test")
    assert center.list() == []


def test_attach_bus_without_subscribe_returns_none(tmp_path: Path) -> None:
    center = NotificationCenter(tmp_path / "n.jsonl")
    assert center.attach_bus(object()) is None
    assert center.attach_bus(None) is None


def test_malformed_line_skipped(tmp_path: Path) -> None:
    path = tmp_path / "n.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    assert NotificationCenter(path).list() == []


def test_notification_rank_and_dict() -> None:
    note = Notification(title="t", severity=NotificationSeverity.CRITICAL)
    assert note.rank == 4
    assert note.as_dict()["severity"] == "critical"

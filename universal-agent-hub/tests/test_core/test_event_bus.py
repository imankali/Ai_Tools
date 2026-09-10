"""تست EventBus (Observer pattern)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from src.core.event_bus import EVENTS, Event, EventBus, install_default_observers


async def test_sync_and_async_handlers_receive_events() -> None:
    """هم هندلر sync و هم async صدا زده می‌شوند."""
    bus = EventBus()
    seen: list[str] = []

    def sync_handler(event: Event) -> None:
        seen.append(f"sync:{event.kind}:{event.payload['n']}")

    async def async_handler(event: Event) -> None:
        await asyncio.sleep(0)
        seen.append(f"async:{event.kind}")

    bus.subscribe("job.done", sync_handler)
    bus.subscribe("job.done", async_handler, name="second")
    await bus.emit("job.done", {"n": 1})
    assert sorted(seen) == ["async:job.done", "sync:job.done:1"]
    assert bus.published_count == 1


def test_wildcard_patterns() -> None:
    """الگوهای wildcard و ``#``."""
    bus = EventBus()
    hits: list[str] = []
    bus.subscribe("tool.*", lambda e: hits.append(e.kind))
    bus.subscribe("#", lambda e: hits.append(f"all:{e.kind}"))
    bus.publish_nowait("tool.completed", {"tool": "x"})
    bus.publish_nowait("agent.started", {})
    assert hits == ["tool.completed", "all:tool.completed", "all:agent.started"]


def test_unsubscribe_and_listeners() -> None:
    """لغو اشتراک و فهرست هندلرها."""
    bus = EventBus()
    token = bus.subscribe("a", lambda e: None, name="mine")
    assert bus.listeners("a") == ["a:mine"]
    assert bus.unsubscribe("a", token)
    assert not bus.unsubscribe("a", token)
    assert bus.listeners() == []


def test_observer_errors_are_isolated() -> None:
    """خطای یک observer نباید انتشار را متوقف کند."""
    bus = EventBus()
    collected: list[str] = []

    def boom(event: Event) -> None:  # noqa: ARG001
        raise RuntimeError("observer exploded")

    bus.subscribe("x", boom)
    bus.subscribe("x", lambda e: collected.append("ok"))
    bus.publish_nowait("x", {})
    assert collected == ["ok"]


async def test_async_emit_isolates_handler_errors() -> None:
    """در emit هم خطا مدیریت می‌شود."""
    bus = EventBus()

    async def bad(event: Event) -> None:  # noqa: ARG001
        raise ValueError("nope")

    bus.subscribe("x", bad)
    event = await bus.emit("x", {})
    assert event.kind == "x"
    assert bus.published_count == 1


def test_history_and_recent() -> None:
    """تاریخچه‌ی محدود و فیلتر recent."""
    bus = EventBus(history_size=3)
    for index in range(5):
        bus.publish_nowait("counter.tick", {"i": index})
    assert len(bus.history) == 3
    assert [e.payload["i"] for e in bus.history] == [2, 3, 4]
    assert bus.recent("counter.*", limit=2)[-1].payload["i"] == 4
    assert bus.recent("nothing.*") == []


def test_persistence_jsonl(tmp_path: Any) -> None:
    """نوشتن رویدادها در فایل JSONL."""
    from json import loads

    target = tmp_path / "events.jsonl"
    bus = EventBus(persist_path=target)
    bus.publish_nowait("a.b", {"x": 1})
    bus.publish_nowait("c.d", {"y": "متن فارسی"})
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert loads(lines[0])["payload"] == {"x": 1}
    assert "متن فارسی" in lines[1]


def test_disabled_bus_is_silent(tmp_path: Any) -> None:
    """حالت غیرفعال: نه تاریخچه، نه فایل."""
    target = tmp_path / "nope.jsonl"
    bus = EventBus(persist_path=target, enabled=False)
    bus.publish_nowait("x", {})
    assert bus.history == ()
    assert bus.published_count == 0
    assert not target.exists()


async def test_wait_for_helper_and_timeout() -> None:
    """``wait_for`` (blocking، مناسب تست/اسکریپت) و TimeoutError."""
    bus = EventBus()

    def fire_later() -> None:
        time.sleep(0.02)
        bus.publish_nowait("done", {"value": 7})

    threading.Thread(target=fire_later, daemon=True).start()
    # wait_for عمداً در نخ جدا صدا زده می‌شود تا loop تست بلوکه نشود
    event = await asyncio.to_thread(bus.wait_for, "done", timeout=2.0)
    assert event.payload["value"] == 7
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(lambda: EventBus().wait_for("never", timeout=0.05))


def test_event_serialization() -> None:
    """Event به dict/JSON تبدیل می‌شود."""
    event = Event(kind="k", payload={"n": 1, "obj": object()}, source="test")
    data = event.to_dict()
    assert data["kind"] == "k" and data["source"] == "test" and len(event.event_id) == 12
    assert '"n": 1' in event.to_json()
    assert event.elapsed_seconds >= 0


def test_subscribe_rejects_non_callable() -> None:
    """هندلر باید callable باشد."""
    with pytest.raises(TypeError):
        EventBus().subscribe("x", "not-callable")  # type: ignore[arg-type]


def test_clear_and_constants() -> None:
    """پاک‌سازی و ثابت‌های نام رویداد."""
    bus = EventBus()
    bus.subscribe("a", lambda e: None)
    bus.publish_nowait("a", {})
    bus.clear()
    assert bus.history == () and bus.listeners() == []
    assert EVENTS.TOOL_COMPLETED == "tool.completed"
    assert EVENTS.AGENT_STARTED == "agent.started"


def test_install_default_observers() -> None:
    """وصل‌کردن observerهای پیش‌فرض (لاگ + سفارشی)."""
    bus = EventBus()
    collected: list[str] = []
    tokens = install_default_observers(bus, log=True, extra=[("custom.event", lambda e: collected.append(e.kind))])
    assert len(tokens) == 2
    bus.publish_nowait("custom.event", {})
    assert collected == ["custom.event"]

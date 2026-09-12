"""تست heartbeat و watchdog."""

from __future__ import annotations

import asyncio
import time

from src.core.heartbeat import CheckResult, Heartbeat, Watchdog


# -------------------------------------------------------------------- watchdog
class TestWatchdog:
    def test_begin_and_end(self) -> None:
        dog = Watchdog()
        dog.begin("s1", label="ask")
        assert len(dog.active) == 1
        assert dog.end("s1") is True
        assert dog.end("s1") is False
        assert dog.active == []

    def test_sweep_releases_only_expired(self) -> None:
        """فقط موردی که از مهلت *خودش* گذشته آزاد می‌شود."""
        dog = Watchdog(default_timeout=10)
        dog.begin("s1", label="stuck", timeout=10)
        dog.begin("s2", label="fresh", timeout=600)
        now = time.time()
        assert dog.sweep(now=now + 11) == ["s1"]
        assert [item["key"] for item in dog.active] == ["s2"]
        assert dog.expired()[0]["label"] == "stuck"
        # s2 هنوز زنده است و با مهلت بلندش آزاد نمی‌شود:
        assert dog.sweep(now=now + 11) == []

    def test_default_timeout_applies(self) -> None:
        dog = Watchdog(default_timeout=10)
        dog.begin("s1")
        assert dog.sweep(now=time.time() + 11) == ["s1"]

    def test_sweep_callback(self) -> None:
        seen: list[tuple[str, str]] = []
        dog = Watchdog(default_timeout=1, on_timeout=lambda key, label: seen.append((key, label)))
        dog.begin("s1", label="x")
        dog.sweep(now=time.time() + 5)
        assert seen == [("s1", "x")]

    def test_sweep_callback_error_swallowed(self) -> None:
        def broken(key: str, label: str) -> None:
            raise RuntimeError("boom")

        dog = Watchdog(default_timeout=1, on_timeout=broken)
        dog.begin("s1")
        assert dog.sweep(now=time.time() + 5) == ["s1"]

    def test_sync_context_manager(self) -> None:
        dog = Watchdog()
        with dog.track("k", label="job"):
            assert len(dog.active) == 1
        assert dog.active == []

    def test_async_context_manager(self) -> None:
        async def scenario() -> None:
            dog = Watchdog()
            async with dog.track("k", label="job"):
                assert len(dog.active) == 1
            assert dog.active == []

        asyncio.run(scenario())

    def test_start_stop(self) -> None:
        async def scenario() -> None:
            dog = Watchdog(sweep_seconds=0.05)
            assert await dog.start() is True
            assert await dog.start() is False
            await asyncio.sleep(0.1)
            assert await dog.stop() is True
            assert await dog.stop() is False

        asyncio.run(scenario())

    def test_describe(self) -> None:
        dog = Watchdog(default_timeout=30)
        dog.begin("s1", label="ask")
        info = dog.describe()
        assert info["default_timeout"] == 30
        assert info["active"][0]["label"] == "ask"


# -------------------------------------------------------------------- heartbeat
class TestHeartbeat:
    def test_default_disk_check(self) -> None:
        beat = Heartbeat(disk_min_free_mb=0)
        results = asyncio.run(beat.run_checks())
        assert results[0].name == "disk_space"
        assert results[0].ok is True

    def test_disk_check_warns(self) -> None:
        beat = Heartbeat(disk_min_free_mb=10**9)  # غیرممکن
        results = asyncio.run(beat.run_checks())
        assert results[0].ok is False
        assert results[0].severity == "warning"

    def test_custom_check_sync_and_async(self) -> None:
        beat = Heartbeat()
        beat.add_check("sync", lambda: CheckResult("sync", ok=True))

        async def async_check() -> CheckResult:
            return CheckResult("async", ok=True)

        beat.add_check("async", async_check)
        names = {r.name for r in asyncio.run(beat.run_checks())}
        assert {"sync", "async"} <= names

    def test_failing_check_reported(self) -> None:
        beat = Heartbeat()

        def broken() -> CheckResult:
            raise RuntimeError("check exploded")

        beat.add_check("broken", broken)
        results = asyncio.run(beat.run_checks())
        bad = next(r for r in results if r.name == "broken")
        assert bad.ok is False
        assert bad.severity == "error"
        assert "exploded" in bad.message

    def test_bad_check_return(self) -> None:
        beat = Heartbeat()
        beat.add_check("weird", lambda: "not a CheckResult")  # type: ignore[arg-type,return-value]
        result = next(r for r in asyncio.run(beat.run_checks()) if r.name == "weird")
        assert result.ok is False

    def test_remove_check(self) -> None:
        beat = Heartbeat()
        asyncio.run(beat.run_checks())
        assert beat.remove_check("disk_space") is True
        assert beat.remove_check("nope") is False

    def test_tick_notifies_on_failure(self) -> None:
        notes: list[str] = []
        beat = Heartbeat(notify=lambda k, t, b: notes.append(t), disk_min_free_mb=10**9)
        asyncio.run(beat.tick())
        assert beat.healthy is False
        assert beat.degraded_ticks == 1
        assert notes and "issue" in notes[0]

    def test_tick_healthy(self) -> None:
        beat = Heartbeat(disk_min_free_mb=0)
        asyncio.run(beat.tick())
        assert beat.healthy is True
        assert beat.degraded_ticks == 0

    def test_notify_error_swallowed(self) -> None:
        def broken(kind: str, title: str, body: str) -> None:
            raise RuntimeError("down")

        beat = Heartbeat(notify=broken, disk_min_free_mb=10**9)
        assert asyncio.run(beat.tick())

    def test_publish_to_bus(self) -> None:
        class FakeBus:
            def __init__(self) -> None:
                self.events: list[str] = []

            def publish_nowait(self, kind: str, payload: dict, *, source: str = "") -> None:
                self.events.append(kind)

        bus = FakeBus()
        beat = Heartbeat(bus=bus, disk_min_free_mb=0)
        asyncio.run(beat.tick())
        assert "heartbeat.ok" in bus.events

    def test_publish_error_swallowed(self) -> None:
        class BadBus:
            def publish_nowait(self, kind: str, payload: dict, *, source: str = "") -> None:
                raise RuntimeError("nope")

        beat = Heartbeat(bus=BadBus(), disk_min_free_mb=0)
        assert asyncio.run(beat.tick())

    def test_start_stop(self) -> None:
        async def scenario() -> None:
            beat = Heartbeat(interval_seconds=0.05, disk_min_free_mb=0)
            assert await beat.start() is True
            assert await beat.start() is False
            await asyncio.sleep(0.12)
            assert await beat.stop() is True
            assert await beat.stop() is False
            assert beat.ticks >= 1

        asyncio.run(scenario())

    def test_describe(self) -> None:
        beat = Heartbeat(disk_min_free_mb=0)
        asyncio.run(beat.tick())
        info = beat.describe()
        assert info["ticks"] == 1
        assert info["healthy"] is True
        assert info["checks"]


def test_check_result_serializes() -> None:
    data = CheckResult("x", ok=False, severity="warning", message="m", value=1).as_dict()
    assert data["ok"] is False
    assert data["value"] == 1

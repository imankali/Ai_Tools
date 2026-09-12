"""تست زمان‌بند روتین‌ها."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.routines import (
    CronSchedule,
    Routine,
    RoutineRun,
    RoutineScheduler,
    TriggerKind,
    parse_cron_field,
)


# ------------------------------------------------------------------- cron parsing
class TestCronParsing:
    def test_star(self) -> None:
        assert parse_cron_field("*", 0, 59) == set(range(60))

    def test_single_and_list(self) -> None:
        assert parse_cron_field("5", 0, 59) == {5}
        assert parse_cron_field("1,2,3", 0, 59) == {1, 2, 3}

    def test_range_and_step(self) -> None:
        assert parse_cron_field("0-10", 0, 59) == set(range(11))
        assert parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}
        assert parse_cron_field("1-10/3", 0, 59) == {1, 4, 7, 10}

    def test_names(self) -> None:
        assert parse_cron_field("mon", 0, 6, names={"mon": 1, "tue": 2}) == {1}
        assert parse_cron_field("jan", 1, 12, names={"jan": 1}) == {1}

    def test_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            parse_cron_field("99", 0, 59)

    def test_bad_value(self) -> None:
        with pytest.raises(ValueError, match="bad cron value"):
            parse_cron_field("abc", 0, 59)

    def test_bad_step(self) -> None:
        with pytest.raises(ValueError, match="bad step"):
            parse_cron_field("*/x", 0, 59)
        with pytest.raises(ValueError, match="positive"):
            parse_cron_field("*/0", 0, 59)

    def test_inverted_range(self) -> None:
        with pytest.raises(ValueError, match="inverted"):
            parse_cron_field("10-1", 0, 59)

    def test_empty(self) -> None:
        with pytest.raises(ValueError, match="empty cron field"):
            parse_cron_field("", 0, 59)

    def test_empty_list_item(self) -> None:
        with pytest.raises(ValueError, match="empty cron list item"):
            parse_cron_field("1,,2", 0, 59)


class TestCronSchedule:
    def test_wrong_field_count(self) -> None:
        with pytest.raises(ValueError, match="needs 5 fields"):
            CronSchedule.parse("* * *")

    def test_matches_every_minute(self) -> None:
        schedule = CronSchedule.parse("* * * * *")
        assert schedule.matches(datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc)) is True

    def test_matches_specific_time(self) -> None:
        schedule = CronSchedule.parse("30 2 * * *")
        assert schedule.matches(datetime(2026, 9, 12, 2, 30, tzinfo=timezone.utc)) is True
        assert schedule.matches(datetime(2026, 9, 12, 2, 31, tzinfo=timezone.utc)) is False
        assert schedule.matches(datetime(2026, 9, 12, 3, 30, tzinfo=timezone.utc)) is False

    def test_weekday_mapping_sunday(self) -> None:
        """cron: Sunday=0 ؛ python: Monday=0. یکشنبه ۲۰۲۶-۰۹-۱۳ است."""
        schedule = CronSchedule.parse("0 0 * * 0")
        sunday = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
        assert sunday.weekday() == 6  # python: Sunday=6
        assert schedule.matches(sunday) is True
        assert schedule.matches(datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)) is False

    def test_dom_and_dow_are_ored(self) -> None:
        """سنت cron: اگر هر دو محدود باشند، «یا» است نه «و»."""
        schedule = CronSchedule.parse("0 0 1 * 1")
        assert schedule.dom_or_dow is True
        first = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)  # پنجشنبه
        assert schedule.matches(first) is True

    def test_next_after(self) -> None:
        schedule = CronSchedule.parse("0 3 * * *")
        base = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
        upcoming = schedule.next_after(base)
        assert upcoming is not None
        assert (upcoming.hour, upcoming.minute) == (3, 0)
        assert upcoming > base

    def test_next_after_wraps_year(self) -> None:
        schedule = CronSchedule.parse("0 0 1 1 *")
        base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        upcoming = schedule.next_after(base)
        assert upcoming is not None
        assert (upcoming.year, upcoming.month, upcoming.day) == (2027, 1, 1)

    def test_impossible_date_returns_none(self) -> None:
        schedule = CronSchedule.parse("0 0 30 2 *")  # ۳۰ فوریه وجود ندارد
        assert schedule.next_after(datetime(2026, 1, 1, tzinfo=timezone.utc), horizon_days=10) is None


# ------------------------------------------------------------------- model
class TestRoutineModel:
    def test_requires_name_and_prompt(self) -> None:
        with pytest.raises(ValueError):
            Routine(name="", prompt="x")
        with pytest.raises(ValueError):
            Routine(name="n", prompt="")

    def test_interval_expression(self) -> None:
        routine = Routine(name="n", trigger=TriggerKind.INTERVAL, expression="60", prompt="p")
        assert routine.validate_trigger() == "60.0"
        with pytest.raises(ValueError, match="at least 5"):
            Routine(name="n", trigger=TriggerKind.INTERVAL, expression="1", prompt="p").validate_trigger()
        with pytest.raises(ValueError, match="needs seconds"):
            Routine(name="n", trigger=TriggerKind.INTERVAL, expression="abc", prompt="p").validate_trigger()

    def test_cron_expression(self) -> None:
        routine = Routine(name="n", trigger=TriggerKind.CRON, expression="0  3 * * *", prompt="p")
        assert routine.validate_trigger() == "0 3 * * *"

    def test_once_expression(self) -> None:
        routine = Routine(name="n", trigger=TriggerKind.ONCE, expression="2030-01-01T00:00:00Z", prompt="p")
        assert routine.validate_trigger()
        with pytest.raises(ValueError, match="ISO-8601"):
            Routine(name="n", trigger=TriggerKind.ONCE, expression="tomorrow", prompt="p").validate_trigger()

    def test_webhook_token_is_path_safe(self) -> None:
        for bad in ("a/b", "a b", "x?y", "a#b", "ünicode"):
            with pytest.raises(ValueError, match="webhook token"):
                Routine(name="n", trigger=TriggerKind.WEBHOOK, expression=bad, prompt="p").validate_trigger()
        assert (
            Routine(name="n", trigger=TriggerKind.WEBHOOK, expression="deploy", prompt="p").validate_trigger()
            == "deploy"
        )

    def test_event_expression(self) -> None:
        assert Routine(name="n", trigger=TriggerKind.EVENT, expression="agent.run.*", prompt="p").validate_trigger()
        with pytest.raises(ValueError, match="event pattern"):
            Routine(name="n", trigger=TriggerKind.EVENT, expression="", prompt="p").validate_trigger()

    def test_timeout_clamped(self) -> None:
        assert Routine(name="n", prompt="p", timeout=1).timeout == 5.0
        assert Routine(name="n", prompt="p", timeout=99999).timeout == 3600.0


# ------------------------------------------------------------------- scheduler
@pytest.fixture
def runs() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def scheduler(tmp_path: Path, runs: list[tuple[str, str]]) -> RoutineScheduler:
    async def executor(prompt: str, profile: str) -> dict[str, object]:
        runs.append((prompt, profile))
        return {"text": f"done: {prompt[:20]}"}

    return RoutineScheduler(tmp_path / "routines.json", executor=executor, tick_seconds=1.0)


class TestScheduler:
    def test_add_and_persist(self, scheduler: RoutineScheduler, tmp_path: Path) -> None:
        routine = scheduler.add(
            Routine(name="nightly", trigger=TriggerKind.CRON, expression="0 22 * * *", prompt="report")
        )
        assert routine.next_run > 0
        reloaded = RoutineScheduler(tmp_path / "routines.json")
        assert reloaded.find_by_name("nightly") is not None

    def test_duplicate_name_rejected(self, scheduler: RoutineScheduler) -> None:
        scheduler.add(Routine(name="dup", prompt="p"))
        with pytest.raises(ValueError, match="already exists"):
            scheduler.add(Routine(name="dup", prompt="p"))

    def test_add_invalid_trigger(self, scheduler: RoutineScheduler) -> None:
        with pytest.raises(ValueError):
            scheduler.add(Routine(name="bad", trigger=TriggerKind.INTERVAL, expression="abc", prompt="p"))

    def test_remove(self, scheduler: RoutineScheduler) -> None:
        routine = scheduler.add(Routine(name="x", prompt="p"))
        assert scheduler.remove(routine.id) is True
        assert scheduler.remove(routine.id) is False

    def test_update(self, scheduler: RoutineScheduler) -> None:
        routine = scheduler.add(Routine(name="x", trigger=TriggerKind.INTERVAL, expression="60", prompt="p"))
        updated = scheduler.update(routine.id, expression="120", profile="read_only")
        assert updated is not None
        assert updated.expression == "120"
        assert updated.profile == "read_only"
        assert scheduler.update("nope", expression="1") is None

    def test_update_ignores_unknown_fields(self, scheduler: RoutineScheduler) -> None:
        routine = scheduler.add(Routine(name="x", prompt="p"))
        assert scheduler.update(routine.id, run_count=999) is not None
        assert scheduler.get(routine.id).run_count == 0

    def test_update_invalid_trigger_rejected(self, scheduler: RoutineScheduler) -> None:
        routine = scheduler.add(Routine(name="x", trigger=TriggerKind.INTERVAL, expression="60", prompt="p"))
        with pytest.raises(ValueError):
            scheduler.update(routine.id, expression="not-a-number")

    def test_set_enabled(self, scheduler: RoutineScheduler) -> None:
        routine = scheduler.add(Routine(name="x", prompt="p"))
        assert scheduler.set_enabled(routine.id, False) is not None
        assert scheduler.get(routine.id).enabled is False
        assert scheduler.set_enabled("nope", True) is None

    def test_due_and_tick(self, scheduler: RoutineScheduler, runs: list[tuple[str, str]]) -> None:
        routine = scheduler.add(Routine(name="soon", trigger=TriggerKind.INTERVAL, expression="5", prompt="hello"))
        routine.next_run = time.time() - 1
        due = scheduler.due()
        assert [r.id for r in due] == [routine.id]


def test_tick_executes_and_records(tmp_path: Path) -> None:
    calls: list[str] = []

    async def executor(prompt: str, profile: str) -> dict[str, object]:
        calls.append(prompt)
        return {"text": "ok"}

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=executor)
    routine = scheduler.add(Routine(name="t", trigger=TriggerKind.INTERVAL, expression="5", prompt="do it"))
    routine.next_run = time.time() - 1
    results = __import__("asyncio").run(scheduler.tick())
    assert len(results) == 1
    assert results[0].status == "ok"
    assert calls == ["do it"]
    assert scheduler.get(routine.id).run_count == 1
    assert scheduler.history(routine.id)[0].status == "ok"


def test_tick_error_increments_backoff(tmp_path: Path) -> None:
    async def executor(prompt: str, profile: str) -> dict[str, object]:
        raise RuntimeError("boom")

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=executor)
    routine = scheduler.add(Routine(name="t", trigger=TriggerKind.INTERVAL, expression="5", prompt="p"))
    routine.next_run = time.time() - 1
    results = __import__("asyncio").run(scheduler.tick())
    assert results[0].status == "error"
    assert "boom" in results[0].error
    assert scheduler.get(routine.id).fail_count == 1


def test_timeout_marks_error(tmp_path: Path) -> None:
    import asyncio

    async def slow(prompt: str, profile: str) -> dict[str, object]:
        await asyncio.sleep(5)
        return {"text": "late"}

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=slow)
    routine = scheduler.add(Routine(name="t", trigger=TriggerKind.INTERVAL, expression="5", prompt="p", timeout=5.0))
    routine.timeout = 5.0
    routine.next_run = time.time() - 1
    results = asyncio.run(scheduler.tick())
    assert results[0].status == "error"
    assert "timed out" in results[0].error


def test_no_executor_errors(tmp_path: Path) -> None:
    import asyncio

    scheduler = RoutineScheduler(tmp_path / "r.json")
    routine = scheduler.add(Routine(name="t", trigger=TriggerKind.INTERVAL, expression="5", prompt="p"))
    routine.next_run = time.time() - 1
    results = asyncio.run(scheduler.tick())
    assert results[0].status == "error"
    assert "no routine executor" in results[0].error


def test_max_runs_stops_firing(tmp_path: Path) -> None:
    import asyncio

    async def executor(prompt: str, profile: str) -> dict[str, object]:
        return {"text": "ok"}

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=executor)
    routine = scheduler.add(Routine(name="t", trigger=TriggerKind.INTERVAL, expression="5", prompt="p", max_runs=1))
    routine.next_run = time.time() - 1
    asyncio.run(scheduler.tick())
    routine.next_run = time.time() - 1
    assert scheduler.due() == []


def test_inflight_skips_second_fire(tmp_path: Path) -> None:
    import asyncio

    async def executor(prompt: str, profile: str) -> dict[str, object]:
        return {"text": "ok"}

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=executor)
    routine = scheduler.add(Routine(name="t", trigger=TriggerKind.INTERVAL, expression="5", prompt="p"))
    scheduler._inflight.add(routine.id)
    result = asyncio.run(scheduler.fire(routine))
    assert result.status == "skipped"


def test_once_disables_itself(tmp_path: Path) -> None:
    import asyncio

    async def executor(prompt: str, profile: str) -> dict[str, object]:
        return {"text": "ok"}

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=executor)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    routine = scheduler.add(Routine(name="one", trigger=TriggerKind.ONCE, expression=past, prompt="p"))
    asyncio.run(scheduler.tick())
    assert scheduler.get(routine.id).enabled is False


def test_webhook_fire(tmp_path: Path) -> None:
    import asyncio

    seen: list[dict] = []

    async def executor(prompt: str, profile: str) -> dict[str, object]:
        seen.append({"prompt": prompt})
        return {"text": "hooked"}

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=executor)
    routine = scheduler.add(
        Routine(name="hook", trigger=TriggerKind.WEBHOOK, expression="deploy", prompt="deploy now")
    )
    result = asyncio.run(scheduler.fire_webhook("deploy", payload={"ref": "main"}))
    assert result is not None
    assert result.status == "ok"
    assert "deploy now" in seen[0]["prompt"]
    assert "main" in seen[0]["prompt"]
    assert asyncio.run(scheduler.fire_webhook("unknown")) is None
    assert routine.trigger is TriggerKind.WEBHOOK


def test_webhook_disabled_returns_none(tmp_path: Path) -> None:
    import asyncio

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop)
    scheduler.add(Routine(name="h", trigger=TriggerKind.WEBHOOK, expression="d", prompt="p", enabled=False))
    assert asyncio.run(scheduler.fire_webhook("d")) is None


def test_event_fire(tmp_path: Path) -> None:
    import asyncio

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop)
    scheduler.add(Routine(name="on-run", trigger=TriggerKind.EVENT, expression="agent.run.*", prompt="react"))
    fired = asyncio.run(scheduler.fire_event("agent.run.completed"))
    assert len(fired) == 1
    assert asyncio.run(scheduler.fire_event("other.event")) == []


def test_webhook_routes(tmp_path: Path) -> None:
    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop)
    scheduler.add(Routine(name="h", trigger=TriggerKind.WEBHOOK, expression="deploy", prompt="p"))
    routes = scheduler.webhook_routes()
    assert routes[0]["token"] == "deploy"
    assert routes[0]["enabled"] == "true"


def test_notify_called(tmp_path: Path) -> None:
    import asyncio

    notes: list[str] = []
    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop, notify=lambda k, t, b: notes.append(t))
    routine = scheduler.add(Routine(name="n", trigger=TriggerKind.INTERVAL, expression="5", prompt="p"))
    asyncio.run(scheduler.fire(routine))
    assert notes and "n" in notes[0]


def test_notify_error_swallowed(tmp_path: Path) -> None:
    import asyncio

    def broken(kind: str, title: str, body: str) -> None:
        raise RuntimeError("sink down")

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop, notify=broken)
    routine = scheduler.add(Routine(name="n", trigger=TriggerKind.INTERVAL, expression="5", prompt="p"))
    assert asyncio.run(scheduler.fire(routine)).status == "ok"


def test_audit_called(tmp_path: Path) -> None:
    import asyncio

    from src.core.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.jsonl")
    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop, audit=audit)
    routine = scheduler.add(Routine(name="n", trigger=TriggerKind.INTERVAL, expression="5", prompt="p"))
    asyncio.run(scheduler.fire(routine))
    actions = [e.action for e in audit.entries()]
    assert "routine.create" in actions
    assert "routine.ok" in actions


def test_import_routines(tmp_path: Path) -> None:
    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop)
    items = [
        {"name": "a", "trigger": "interval", "expression": "60", "prompt": "p"},
        {"name": "bad", "trigger": "interval", "expression": "abc", "prompt": "p"},
    ]
    imported = scheduler.import_routines(items)
    assert [r.name for r in imported] == ["a"]
    assert scheduler.import_routines(items) == []  # بدون replace تکراری رد می‌شود
    assert len(scheduler.import_routines(items, replace=True)) == 1


def test_corrupt_store_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text("{not json", encoding="utf-8")
    assert RoutineScheduler(path).all() == []


def test_invalid_stored_routine_is_disabled(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps({"routines": [{"name": "bad", "trigger": "interval", "expression": "abc", "prompt": "p"}]}),
        encoding="utf-8",
    )
    assert RoutineScheduler(path).all() == []


def test_start_stop_loop(tmp_path: Path) -> None:
    import asyncio

    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop, tick_seconds=0.05)

    async def scenario() -> None:
        assert await scheduler.start() is True
        assert scheduler.running is True
        assert await scheduler.start() is False
        await asyncio.sleep(0.12)
        assert await scheduler.stop() is True
        assert scheduler.running is False
        assert await scheduler.stop() is False

    asyncio.run(scenario())
    assert scheduler.stats.ticks >= 1


def test_describe(tmp_path: Path) -> None:
    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_noop)
    scheduler.add(Routine(name="d", trigger=TriggerKind.INTERVAL, expression="60", prompt="p"))
    info = scheduler.describe()
    assert info["count"] == 1
    assert info["running"] is False
    assert info["routines"][0]["name"] == "d"
    assert "history" in info["routines"][0]


def test_routine_run_serializes() -> None:
    run = RoutineRun(routine_id="r", status="ok", summary="s")
    assert run.as_dict()["status"] == "ok"


async def _noop(prompt: str, profile: str) -> dict[str, object]:
    return {"text": "ok"}

"""روتین‌ها: زمان‌بندی، webhook و تریگر رویداد.

چرا این ماژول بزرگ‌ترین شکاف بود؟
---------------------------------
هر سه پروژه‌ی مرجع این را دارند و ما هیچ نداشتیم:

* OpenClaw → ``src/cron``
* Agent-Zero → ``task_scheduler`` + ۶ اندپوینت ``scheduler_task_*``
* IronClaw → «Routines — cron schedules, event triggers, webhook handlers»

بدون این، ایجنت فقط *واکنشی* است: تا کسی حرف نزند، کاری نمی‌کند. با این
ماژول، ایجنت می‌تواند هر شب گزارش بگیرد، هر ساعت دیسک را چک کند، و با یک
POST از GitHub بیدار شود.

قواعد طراحی
------------
1. **بدون وابستگی تازه.** پارسر cron را خودمان نوشتیم (زیرمجموعه‌ی ۵ فیلدی
   استاندارد). ``croniter`` روی Termux دردسر دارد.
2. **fail-closed.** روتینِ خراب غیرفعال می‌شود، نه اینکه هر تیک اجرا شود.
3. **backoff.** روتینی که پشت‌سرهم شکست می‌خورد، با تأخیر فزاینده دوباره
   تلاش می‌کند تا لاگ و هزینه پر نشود.
4. **audit + notify.** هر اجرا در لاگ ممیزی ثبت و در مرکز اعلان‌ها دیده می‌شود.
5. **تک‌اجرا.** یک روتین هرگز هم‌زمان دو بار اجرا نمی‌شود (``_inflight``).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from src.utils.logger import get_logger

__all__ = [
    "CronSchedule",
    "Routine",
    "RoutineRun",
    "RoutineScheduler",
    "TriggerKind",
    "parse_cron_field",
]

logger = get_logger("core.routines")

#: نوع اجراکننده: یک callable که prompt را می‌گیرد و خلاصه‌ی نتیجه را برمی‌گرداند.
RoutineExecutor = Callable[[str, str], Awaitable[dict[str, Any]]]


class TriggerKind(str, Enum):
    """نوع تریگر یک روتین."""

    INTERVAL = "interval"
    CRON = "cron"
    ONCE = "once"
    WEBHOOK = "webhook"
    EVENT = "event"


# --------------------------------------------------------------------------- cron
_WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_cron_field(text: str, low: int, high: int, *, names: dict[str, int] | None = None) -> set[int]:
    """یک فیلد cron را به مجموعه‌ی مقادیر مجاز تبدیل می‌کند.

    پشتیبانی: ``*``، ``a``، ``a-b``، ``a-b/step``، ``*/step``، ``a,b,c`` و نام‌ها
    (``mon``، ``jan``).

    Args:
        text: متن فیلد.
        low: کمینه‌ی مجاز.
        high: بیشینه‌ی مجاز.
        names: نگاشت نام → عدد (برای روز هفته/ماه).

    Returns:
        مجموعه‌ی اعداد مجاز.

    Raises:
        ValueError: اگر فیلد نامعتبر باشد.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty cron field")
    out: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError("empty cron list item")
        step = 1
        body = chunk
        if "/" in chunk:
            body, _, step_text = chunk.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(f"bad step in '{chunk}'") from exc
            if step <= 0:
                raise ValueError(f"step must be positive in '{chunk}'")
            body = body.strip() or "*"
        if body == "*":
            start, end = low, high
        elif "-" in body:
            left, _, right = body.partition("-")
            start = _parse_cron_value(left, low, high, names)
            end = _parse_cron_value(right, low, high, names)
            if start > end:
                raise ValueError(f"inverted range in '{chunk}'")
        else:
            start = end = _parse_cron_value(body, low, high, names)
        out.update(range(start, end + 1, step))
    if not out:
        raise ValueError(f"'{raw}' matches nothing")
    return out


def _parse_cron_value(text: str, low: int, high: int, names: dict[str, int] | None) -> int:
    """یک مقدار واحد (عدد یا نام) را پارس می‌کند."""
    token = str(text or "").strip().lower()
    if names and token in names:
        value = names[token]
    else:
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(f"bad cron value '{text}'") from exc
    if value < low or value > high:
        raise ValueError(f"cron value {value} out of range [{low}, {high}]")
    return value


@dataclass(frozen=True)
class CronSchedule:
    """یک عبارت cron پنج‌فیلدی پارس‌شده.

    فیلدها: ``minute hour day-of-month month day-of-week``.
    """

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: اگر day-of-month و day-of-week هر دو محدود باشند، cron سنتی «یا» می‌گیرد.
    dom_or_dow: bool = False

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        """پارس یک عبارت cron.

        Raises:
            ValueError: اگر تعداد فیلدها یا مقداری نامعتبر باشد.
        """
        raw = " ".join(str(expression or "").split())
        parts = raw.split(" ")
        if len(parts) != 5:
            raise ValueError(f"cron needs 5 fields, got {len(parts)}: '{expression}'")
        minutes = parse_cron_field(parts[0], 0, 59)
        hours = parse_cron_field(parts[1], 0, 23)
        days = parse_cron_field(parts[2], 1, 31)
        months = parse_cron_field(parts[3], 1, 12, names=_MONTHS)
        weekdays = parse_cron_field(parts[4], 0, 6, names=_WEEKDAYS)
        dom_limited = parts[2] != "*"
        dow_limited = parts[4] != "*"
        return cls(
            expression=raw,
            minutes=frozenset(minutes),
            hours=frozenset(hours),
            days=frozenset(days),
            months=frozenset(months),
            weekdays=frozenset(weekdays),
            dom_or_dow=dom_limited and dow_limited,
        )

    def matches(self, moment: datetime) -> bool:
        """آیا این لحظه با برنامه می‌خواند؟"""
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        dom_ok = moment.day in self.days
        # python: Monday=0 … Sunday=6 ؛ cron: Sunday=0 … Saturday=6
        dow = (moment.weekday() + 1) % 7
        dow_ok = dow in self.weekdays
        if self.dom_or_dow:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, moment: datetime, *, horizon_days: int = 400) -> datetime | None:
        """نزدیک‌ترین لحظه‌ی بعدی (با دقت دقیقه)."""
        cursor = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = moment + timedelta(days=max(1, horizon_days))
        while cursor <= limit:
            if cursor.month not in self.months:
                # پرش به اول ماه بعد
                if cursor.month == 12:
                    cursor = cursor.replace(year=cursor.year + 1, month=1, day=1, hour=0, minute=0)
                else:
                    cursor = cursor.replace(month=cursor.month + 1, day=1, hour=0, minute=0)
                continue
            if self.matches(cursor):
                return cursor
            cursor += timedelta(minutes=1)
        return None


# --------------------------------------------------------------------------- models
class RoutineRun(BaseModel):
    """یک اجرای روتین."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    routine_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    trigger: TriggerKind = TriggerKind.INTERVAL
    status: str = "ok"  # ok | error | skipped
    summary: str = ""
    error: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return self.model_dump(mode="json")


class Routine(BaseModel):
    """یک کار زمان‌بندی‌شده.

    Attributes:
        id: شناسه.
        name: نام خوانا.
        enabled: فعال؟
        trigger: نوع تریگر.
        expression: برای ``cron`` = عبارت cron؛ برای ``interval`` = ثانیه؛
            برای ``once`` = ISO-8601؛ برای ``event`` = الگوی رویداد؛
            برای ``webhook`` = توکن مسیر.
        prompt: متنی که به ایجنت داده می‌شود.
        profile: پروفایل ابزارها.
        max_runs: سقف اجرا (``0`` = نامحدود).
        timeout: سقف ثانیه‌ی هر اجرا.
        notify: نتیجه در مرکز اعلان‌ها برود؟
        run_count: تعداد اجراهای موفق.
        fail_count: تعداد شکست‌های پی‌درپی.
        last_run: آخرین زمان اجرا (epoch).
        next_run: زمان بعدی (epoch) برای cron/interval/once.
        created_at: زمان ساخت.
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str = ""
    enabled: bool = True
    trigger: TriggerKind = TriggerKind.INTERVAL
    expression: str = "3600"
    prompt: str = ""
    profile: str = "generalist"
    max_runs: int = 0
    timeout: float = 300.0
    notify: bool = True
    run_count: int = 0
    fail_count: int = 0
    last_run: float = 0.0
    next_run: float = 0.0
    created_at: float = Field(default_factory=time.time)

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> str:
        """نام خالی مجاز نیست."""
        text = str(value or "").strip()
        if not text:
            raise ValueError("routine name must not be empty")
        return text[:120]

    @field_validator("prompt", mode="before")
    @classmethod
    def _clean_prompt(cls, value: Any) -> str:
        """prompt خالی یعنی روتین بی‌فایده."""
        text = str(value or "").strip()
        if not text:
            raise ValueError("routine prompt must not be empty")
        return text[:8000]

    @field_validator("timeout", mode="after")
    @classmethod
    def _bound_timeout(cls, value: float) -> float:
        """سقف زمانی منطقی."""
        return max(5.0, min(float(value), 3600.0))

    def webhook_token(self) -> str:
        """توکن مسیر webhook (پایدار و بدون secret)."""
        return f"{self.id}-{self.trigger.value}"

    def validate_trigger(self) -> str:
        """سازگاری ``trigger`` و ``expression`` را چک می‌کند.

        Returns:
            عبارت نرمال‌شده.

        Raises:
            ValueError: اگر ناسازگار باشند.
        """
        expr = str(self.expression or "").strip()
        if self.trigger is TriggerKind.INTERVAL:
            try:
                seconds = float(expr)
            except ValueError as exc:
                raise ValueError(f"interval trigger needs seconds, got '{expr}'") from exc
            if seconds < 5:
                raise ValueError("interval must be at least 5 seconds")
            return str(seconds)
        if self.trigger is TriggerKind.CRON:
            return CronSchedule.parse(expr).expression
        if self.trigger is TriggerKind.ONCE:
            _parse_iso(expr)
            return expr
        if self.trigger is TriggerKind.EVENT:
            if not expr or len(expr) > 120:
                raise ValueError("event trigger needs an event pattern")
            return expr
        if self.trigger is TriggerKind.WEBHOOK:
            if not expr.isascii() or len(expr) > 64 or any(ch in expr for ch in "/?&# \t"):
                raise ValueError("webhook token must be short ascii without path characters")
            return expr
        raise ValueError(f"unsupported trigger '{self.trigger}'")


def _parse_iso(text: str) -> datetime:
    """پارس ISO-8601؛ در نبود timezone، UTC فرض می‌شود."""
    raw = str(text or "").strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 datetime: '{text}'") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _utc_now() -> datetime:
    """زمان حال UTC."""
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    """رشته‌ی ISO برای زمان حال یا یک لحظه‌ی مشخص."""
    return (moment or _utc_now()).isoformat(timespec="milliseconds")


@dataclass
class _SchedulerStats:
    """شمارنده‌های داخلی."""

    ticks: int = 0
    fired: int = 0
    errors: int = 0
    skipped: int = 0
    started_at: float = 0.0
    last_tick: float = 0.0


class RoutineScheduler:
    """زمان‌بند روتین‌ها.

    نمونه::

        scheduler = RoutineScheduler(store, executor=my_executor)
        scheduler.add(Routine(name="nightly", trigger="cron", expression="0 22 * * *", prompt="…"))
        await scheduler.start()
        …
        await scheduler.stop()
    """

    def __init__(
        self,
        path: str | Path,
        *,
        executor: RoutineExecutor | None = None,
        tick_seconds: float = 20.0,
        max_history: int = 200,
        notify: Callable[[str, str, str], None] | None = None,
        audit: Any = None,
        backoff_factor: float = 2.0,
        max_backoff: float = 3600.0,
    ) -> None:
        """Args:
        path: فایل JSON روتین‌ها.
        executor: ``async (prompt, profile) -> dict``.
        tick_seconds: فاصله‌ی حلقه‌ی زمان‌بندی.
        max_history: تعداد اجرای نگه‌داشته‌شده برای هر روتین.
        notify: ``callable(kind, title, body)`` برای مرکز اعلان‌ها.
        audit: یک :class:`~src.core.audit.AuditLog` (اختیاری).
        backoff_factor: ضریب تأخیر بعد از شکست.
        max_backoff: سقف تأخیر (ثانیه).
        """
        self.path = Path(path).expanduser().resolve()
        self.executor = executor
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.max_history = max(5, int(max_history))
        self._notify = notify
        self._audit = audit
        self.backoff_factor = max(1.0, float(backoff_factor))
        self.max_backoff = max(10.0, float(max_backoff))
        self._routines: dict[str, Routine] = {}
        self._history: dict[str, deque[RoutineRun]] = {}
        self._cron: dict[str, CronSchedule] = {}
        self._inflight: set[str] = set()
        self._lock = threading.RLock()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.stats = _SchedulerStats()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        """روتین‌ها را از دیسک می‌خواند؛ روتین خراب غیرفعال می‌شود (fail-closed)."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("routines: cannot read %s: %s", self.path, exc)
            return
        for item in data.get("routines", []) if isinstance(data, dict) else []:
            try:
                routine = Routine.model_validate(item)
                routine.validate_trigger()
            except Exception as exc:  # noqa: BLE001 - یک روتین خراب نباید بقیه را ببندد
                logger.warning("routines: disabling invalid routine %r: %s", item.get("name"), exc)
                continue
            self._routines[routine.id] = routine
            self._history[routine.id] = deque(maxlen=self.max_history)
            self._recompile(routine)
        for item in data.get("history", []) if isinstance(data, dict) else []:
            try:
                run = RoutineRun.model_validate(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("routines: skipping malformed history entry: %s", exc)
                continue
            bucket = self._history.setdefault(run.routine_id, deque(maxlen=self.max_history))
            bucket.append(run)

    def save(self) -> int:
        """روتین‌ها و تاریخچه را اتمیک می‌نویسد.

        Returns:
            تعداد روتین‌های نوشته‌شده.
        """
        payload = {
            "version": 1,
            "routines": [r.model_dump(mode="json") for r in self._routines.values()],
            "history": [run.model_dump(mode="json") for bucket in self._history.values() for run in bucket],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("routines: cannot write %s: %s", self.path, exc)
            tmp.unlink(missing_ok=True)
        return len(self._routines)

    def _recompile(self, routine: Routine) -> None:
        """عبارت cron را کامپایل و ``next_run`` را ست می‌کند."""
        self._cron.pop(routine.id, None)
        now = time.time()
        if routine.trigger is TriggerKind.CRON:
            schedule = CronSchedule.parse(routine.expression)
            self._cron[routine.id] = schedule
            upcoming = schedule.next_after(_utc_now())
            routine.next_run = upcoming.timestamp() if upcoming else 0.0
        elif routine.trigger is TriggerKind.INTERVAL:
            base = routine.last_run or now
            routine.next_run = base + float(routine.expression)
        elif routine.trigger is TriggerKind.ONCE:
            routine.next_run = _parse_iso(routine.expression).timestamp()
        else:
            routine.next_run = 0.0

    # ------------------------------------------------------------------ CRUD
    def add(self, routine: Routine) -> Routine:
        """یک روتین اضافه می‌کند.

        Raises:
            ValueError: اگر trigger/expression ناسازگار باشد یا نام تکراری باشد.
        """
        routine.validate_trigger()
        with self._lock:
            if any(existing.name == routine.name for existing in self._routines.values()):
                raise ValueError(f"routine named '{routine.name}' already exists")
            self._routines[routine.id] = routine
            self._history.setdefault(routine.id, deque(maxlen=self.max_history))
            self._recompile(routine)
            self.save()
        self._audit_record("routine.create", routine, "allowed")
        return routine

    def remove(self, routine_id: str) -> bool:
        """حذف یک روتین."""
        with self._lock:
            routine = self._routines.pop(routine_id, None)
            if routine is None:
                return False
            self._history.pop(routine_id, None)
            self._cron.pop(routine_id, None)
            self.save()
        self._audit_record("routine.remove", routine, "allowed")
        return True

    def get(self, routine_id: str) -> Routine | None:
        """یافتن با شناسه."""
        return self._routines.get(routine_id)

    def find_by_name(self, name: str) -> Routine | None:
        """یافتن با نام."""
        for routine in self._routines.values():
            if routine.name == name:
                return routine
        return None

    def all(self) -> list[Routine]:
        """همه‌ی روتین‌ها."""
        return list(self._routines.values())

    def set_enabled(self, routine_id: str, enabled: bool) -> Routine | None:
        """فعال/غیرفعال کردن."""
        routine = self._routines.get(routine_id)
        if routine is None:
            return None
        routine.enabled = bool(enabled)
        if routine.enabled:
            routine.fail_count = 0
            self._recompile(routine)
        self.save()
        return routine

    def update(self, routine_id: str, **changes: Any) -> Routine | None:
        """به‌روزرسانی فیلدها؛ trigger/expression دوباره اعتبارسنجی می‌شود."""
        routine = self._routines.get(routine_id)
        if routine is None:
            return None
        allowed = {"name", "enabled", "trigger", "expression", "prompt", "profile", "max_runs", "timeout", "notify"}
        payload = {k: v for k, v in changes.items() if k in allowed}
        if not payload:
            return routine
        candidate = routine.model_copy(update=payload)
        candidate.validate_trigger()  # ناسازگاری ⇒ استثنا، چیزی نوشته نمی‌شود
        for key, value in payload.items():
            setattr(routine, key, value)
        routine.fail_count = 0
        self._recompile(routine)
        self.save()
        self._audit_record("routine.update", routine, "allowed")
        return routine

    def history(self, routine_id: str, *, limit: int = 20) -> list[RoutineRun]:
        """تاریخچه‌ی اجراهای یک روتین، جدیدترین اول."""
        bucket = self._history.get(routine_id)
        if not bucket:
            return []
        items = list(bucket)
        items.reverse()
        return items[: max(1, limit)]

    # ------------------------------------------------------------------ scheduling
    @property
    def running(self) -> bool:
        """آیا حلقه‌ی زمان‌بندی فعال است؟"""
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """حلقه‌ی زمان‌بندی را در پس‌زمینه راه می‌اندازد.

        Returns:
            ``True`` اگر تازه شروع شد.
        """
        if self.running:
            return False
        if self.executor is None:
            logger.warning("routines: no executor configured; scheduler will not fire anything")
        self._stopping.clear()
        self.stats.started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="routine-scheduler")
        return True

    async def stop(self, *, timeout: float = 5.0) -> bool:
        """حلقه را متوقف می‌کند و کارهای در جریان را تمام می‌کند."""
        task = self._task
        self._task = None
        self._stopping.set()
        if task is None:
            return False
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("routines: loop exited with %s", exc)
        return True

    async def _loop(self) -> None:
        """حلقه‌ی اصلی."""
        try:
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # noqa: BLE001 - حلقه نباید بمیرد
                    self.stats.errors += 1
                    logger.warning("routines: tick failed: %s", exc)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        finally:
            self.save()

    def due(self, *, now: float | None = None) -> list[Routine]:
        """روتین‌هایی که الان باید اجرا شوند."""
        moment = now if now is not None else time.time()
        ready: list[Routine] = []
        for routine in self._routines.values():
            if not routine.enabled or routine.id in self._inflight:
                continue
            if routine.trigger in (TriggerKind.WEBHOOK, TriggerKind.EVENT):
                continue
            if routine.max_runs and routine.run_count >= routine.max_runs:
                continue
            if routine.next_run and routine.next_run <= moment:
                ready.append(routine)
        return ready

    async def tick(self, *, now: float | None = None) -> list[RoutineRun]:
        """یک گام زمان‌بندی: روتین‌های سررسیدشده را اجرا می‌کند.

        Returns:
            نتیجه‌ی اجراها.
        """
        self.stats.ticks += 1
        self.stats.last_tick = now if now is not None else time.time()
        runs: list[RoutineRun] = []
        for routine in self.due(now=now):
            runs.append(await self.fire(routine, trigger=routine.trigger))
        return runs

    def _backoff_delay(self, routine: Routine) -> float:
        """تأخیر فزاینده بعد از شکست."""
        if routine.fail_count <= 0:
            return 0.0
        delay = self.backoff_factor ** min(routine.fail_count, 8)
        return min(delay, self.max_backoff)

    async def fire(
        self, routine: Routine, *, trigger: TriggerKind | None = None, payload: dict[str, Any] | None = None
    ) -> RoutineRun:
        """یک روتین را (یک بار) اجرا می‌کند.

        Args:
            routine: روتین.
            trigger: تریگری که باعث اجرا شد.
            payload: داده‌ی webhook/رویداد که به prompt افزوده می‌شود.

        Returns:
            رکورد اجرا.
        """
        used = trigger or routine.trigger
        run = RoutineRun(routine_id=routine.id, started_at=_iso(), trigger=used)
        if routine.id in self._inflight:
            run.status = "skipped"
            run.summary = "previous run still in flight"
            self.stats.skipped += 1
            self._record_run(routine, run)
            return run

        self._inflight.add(routine.id)
        prompt = routine.prompt
        if payload:
            prompt = f"{prompt}\n\n[trigger payload]\n{json.dumps(payload, ensure_ascii=False, default=str)[:2000]}"
        started = time.monotonic()
        try:
            if self.executor is None:
                raise RuntimeError("no routine executor configured")
            result = await asyncio.wait_for(self.executor(prompt, routine.profile), timeout=routine.timeout)
            run.status = "ok"
            run.summary = str((result or {}).get("text") or (result or {}).get("summary") or "done")[:1000]
            routine.run_count += 1
            routine.fail_count = 0
            self.stats.fired += 1
        except asyncio.TimeoutError:
            run.status = "error"
            run.error = f"timed out after {routine.timeout:.0f}s"
            routine.fail_count += 1
            self.stats.errors += 1
        except asyncio.CancelledError:
            self._inflight.discard(routine.id)
            raise
        except Exception as exc:  # noqa: BLE001 - شکست یک روتین نباید بقیه را بگیرد
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"[:500]
            routine.fail_count += 1
            self.stats.errors += 1
        finally:
            self._inflight.discard(routine.id)

        run.finished_at = _iso()
        run.duration_ms = int((time.monotonic() - started) * 1000)
        routine.last_run = time.time()

        if routine.trigger is TriggerKind.CRON:
            schedule = self._cron.get(routine.id)
            upcoming = schedule.next_after(_utc_now()) if schedule else None
            routine.next_run = upcoming.timestamp() if upcoming else 0.0
        elif routine.trigger is TriggerKind.INTERVAL:
            routine.next_run = routine.last_run + float(routine.expression) + self._backoff_delay(routine)
        elif routine.trigger is TriggerKind.ONCE:
            routine.enabled = False
            routine.next_run = 0.0

        self._record_run(routine, run)
        return run

    def _record_run(self, routine: Routine, run: RoutineRun) -> None:
        """تاریخچه + ذخیره + اعلان + ممیزی."""
        self._history.setdefault(routine.id, deque(maxlen=self.max_history)).append(run)
        self.save()
        self._audit_record(
            f"routine.{run.status}",
            routine,
            "allowed" if run.status == "ok" else "failed",
            run_id=run.id,
            error=run.error,
        )
        if routine.notify and self._notify is not None:
            kind = "routine"
            title = f"routine '{routine.name}' {run.status}"
            body = run.summary or run.error
            try:
                self._notify(kind, title, body)
            except Exception as exc:  # noqa: BLE001
                logger.debug("routines: notify failed: %s", exc)

    def _audit_record(self, action: str, routine: Routine, decision: str, **detail: Any) -> None:
        """ثبت در لاگ ممیزی (اگر وصل باشد)."""
        if self._audit is None:
            return
        try:
            self._audit.record("scheduler", action, routine.name, decision, routine_id=routine.id, **detail)
        except Exception as exc:  # noqa: BLE001
            logger.debug("routines: audit failed: %s", exc)

    # ------------------------------------------------------------------ webhook / event
    async def fire_webhook(self, token: str, *, payload: dict[str, Any] | None = None) -> RoutineRun | None:
        """اجرای روتینی که توکن webhook آن منطبق است.

        Args:
            token: توکن مسیر.
            payload: بدنه‌ی درخواست.

        Returns:
            رکورد اجرا، یا ``None`` اگر توکن شناخته نشد.
        """
        for routine in self._routines.values():
            if routine.trigger is not TriggerKind.WEBHOOK:
                continue
            if routine.expression != token and routine.webhook_token() != token:
                continue
            if not routine.enabled:
                return None
            return await self.fire(routine, trigger=TriggerKind.WEBHOOK, payload=payload)
        return None

    async def fire_event(self, kind: str, *, payload: dict[str, Any] | None = None) -> list[RoutineRun]:
        """اجرای روتین‌هایی که الگوی رویدادشان با ``kind`` می‌خواند.

        الگو از ``fnmatch`` پیروی می‌کند (``agent.run.*``).
        """
        import fnmatch

        fired: list[RoutineRun] = []
        for routine in list(self._routines.values()):
            if routine.trigger is not TriggerKind.EVENT or not routine.enabled:
                continue
            if not fnmatch.fnmatch(kind, routine.expression):
                continue
            fired.append(
                await self.fire(routine, trigger=TriggerKind.EVENT, payload={"event": kind, **(payload or {})})
            )
        return fired

    def webhook_routes(self) -> list[dict[str, str]]:
        """فهرست توکن‌های webhook فعال (برای UI)."""
        return [
            {"id": r.id, "name": r.name, "token": r.expression, "enabled": str(r.enabled).lower()}
            for r in self._routines.values()
            if r.trigger is TriggerKind.WEBHOOK
        ]

    # ------------------------------------------------------------------ reporting
    def describe(self) -> dict[str, Any]:
        """خلاصه‌ی کامل برای ``/api/routines``."""
        now = time.time()
        return {
            "running": self.running,
            "tick_seconds": self.tick_seconds,
            "count": len(self._routines),
            "enabled": sum(1 for r in self._routines.values() if r.enabled),
            "inflight": sorted(self._inflight),
            "stats": {
                "ticks": self.stats.ticks,
                "fired": self.stats.fired,
                "errors": self.stats.errors,
                "skipped": self.stats.skipped,
                "uptime_s": round(now - self.stats.started_at, 1) if self.stats.started_at else 0.0,
            },
            "routines": [
                {
                    **r.model_dump(mode="json"),
                    "next_run_in_s": round(r.next_run - now, 1) if r.next_run else None,
                    "history": [run.as_dict() for run in self.history(r.id, limit=5)],
                }
                for r in self._routines.values()
            ],
        }

    def import_routines(self, items: Iterable[dict[str, Any]], *, replace: bool = False) -> list[Routine]:
        """وارد کردن چند روتین (برای API/بکاپ).

        Args:
            items: دیکشنری‌های روتین.
            replace: اگر ``True``، روتین هم‌نام بازنویسی می‌شود.

        Returns:
            روتین‌های پذیرفته‌شده.
        """
        imported: list[Routine] = []
        for item in items:
            try:
                routine = Routine.model_validate(item)
                routine.validate_trigger()
            except Exception as exc:  # noqa: BLE001
                logger.warning("routines: skipping invalid import %r: %s", item.get("name"), exc)
                continue
            existing = self.find_by_name(routine.name)
            if existing is not None:
                if not replace:
                    continue
                routine.id = existing.id
                self._history.setdefault(routine.id, deque(maxlen=self.max_history))
            self._routines[routine.id] = routine
            self._history.setdefault(routine.id, deque(maxlen=self.max_history))
            self._recompile(routine)
            imported.append(routine)
        if imported:
            self.save()
        return imported

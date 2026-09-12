"""Heartbeat و Watchdog: پایش پس‌زمینه و خودترمیمی.

دو چیزی که IronClaw صریحاً دارد و ما نداشتیم:

* «**Heartbeat System** — proactive background execution for monitoring»
* «**Self-repair** — automatic detection and recovery of stuck operations»

و Agent-Zero هم ``watchdog`` و ``state_monitor`` دارد.

تفاوت این دو در همین ماژول
--------------------------
* :class:`Heartbeat` — یک تیک دوره‌ای که *چک‌های سلامت* را اجرا می‌کند
  (فضای دیسک، اندازه‌ی حافظه، سلامت زنجیره‌ی ممیزی، وضعیت زمان‌بند) و نتیجه
  را به مرکز اعلان‌ها می‌دهد.
* :class:`Watchdog` — اجرای در جریان را دنبال می‌کند و اگر از مهلت گذشت،
  آن را *شکست‌خورده* علامت می‌زند و رویداد ``watchdog.recovered`` می‌دهد.
  بدون این، یک ``await`` گیرکرده یعنی یک session برای همیشه «در حال اجرا».

هیچ‌کدام خودسرانه چیزی را *حذف* نمی‌کنند؛ فقط گزارش می‌دهند و وضعیت را
سازگار نگه می‌دارند. حذف، کار کاربر است.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.utils.logger import get_logger

__all__ = ["CheckResult", "Heartbeat", "Watchdog"]

logger = get_logger("core.heartbeat")


@dataclass
class CheckResult:
    """نتیجه‌ی یک بررسی سلامت."""

    name: str
    ok: bool = True
    severity: str = "info"  # info | warning | error
    message: str = ""
    value: Any = None
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "checked_at": self.checked_at,
        }


#: یک بررسی سلامت: callable بدون آرگومان که ``CheckResult`` برمی‌گرداند
#: (همگام یا ناهمگام). ``Union`` عمداً به‌جای ``|`` است: این یک *انتساب*
#: سطح ماژول است، نه annotation، پس در زمان اجرا ارزیابی می‌شود و
#: ``GenericAlias | str`` در پایتون ۳.۱۰/۳.۱۱ TypeError می‌دهد.
HealthCheck = Callable[[], Awaitable[CheckResult] | CheckResult]


@dataclass
class _Tracked:
    """یک اجرای در حال پیگیری."""

    key: str
    label: str
    started_at: float
    deadline: float
    cancelled: bool = False


class Watchdog:
    """پیگیری اجرای در جریان و آزادسازی موارد گیرکرده.

    نمونه::

        watchdog = Watchdog(default_timeout=300)
        with watchdog.track("session-1", label="ask"):
            await agent.ask(...)
    """

    def __init__(
        self,
        *,
        default_timeout: float = 300.0,
        sweep_seconds: float = 15.0,
        on_timeout: Callable[[str, str], None] | None = None,
    ) -> None:
        """Args:
        default_timeout: مهلت پیش‌فرض هر اجرا (ثانیه).
        sweep_seconds: فاصله‌ی جاروی پس‌زمینه.
        on_timeout: ``(key, label)`` وقتی موردی منقضی می‌شود.
        """
        self.default_timeout = max(1.0, float(default_timeout))
        self.sweep_seconds = max(1.0, float(sweep_seconds))
        self.on_timeout = on_timeout
        self._tracked: dict[str, _Tracked] = {}
        self._expired: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ tracking
    def begin(self, key: str, *, label: str = "", timeout: float | None = None) -> _Tracked:
        """یک اجرا را ثبت می‌کند."""
        limit = self.default_timeout if timeout is None else max(1.0, float(timeout))
        tracked = _Tracked(key=key, label=label or key, started_at=time.time(), deadline=time.time() + limit)
        self._tracked[key] = tracked
        return tracked

    def end(self, key: str) -> bool:
        """پایان یک اجرا.

        Returns:
            ``True`` اگر واقعاً ثبت شده بود.
        """
        return self._tracked.pop(key, None) is not None

    def track(self, key: str, *, label: str = "", timeout: float | None = None) -> _TrackingContext:
        """context manager برای ثبت/پایان خودکار."""
        return _TrackingContext(self, key, label=label, timeout=timeout)

    @property
    def active(self) -> list[dict[str, Any]]:
        """موارد در جریان."""
        now = time.time()
        return [
            {
                "key": item.key,
                "label": item.label,
                "age_s": round(now - item.started_at, 1),
                "remaining_s": round(max(0.0, item.deadline - now), 1),
            }
            for item in self._tracked.values()
        ]

    def expired(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """موارد منقضی‌شده‌ی اخیر."""
        return list(self._expired[-max(1, limit) :])

    def sweep(self, *, now: float | None = None) -> list[str]:
        """موارد گذشته‌از‌مهلت را پیدا و آزاد می‌کند.

        Returns:
            کلید موارد آزادشده.
        """
        moment = now if now is not None else time.time()
        released: list[str] = []
        for key, item in list(self._tracked.items()):
            if item.deadline > moment:
                continue
            item.cancelled = True
            self._tracked.pop(key, None)
            released.append(key)
            record = {
                "key": key,
                "label": item.label,
                "age_s": round(moment - item.started_at, 1),
                "at": moment,
            }
            self._expired.append(record)
            if len(self._expired) > 100:
                self._expired = self._expired[-100:]
            logger.warning("watchdog: '%s' exceeded its deadline and was released", item.label)
            if self.on_timeout is not None:
                try:
                    self.on_timeout(key, item.label)
                except Exception as exc:  # noqa: BLE001 - callback نباید جارو را بشکند
                    logger.debug("watchdog: on_timeout failed: %s", exc)
        return released

    async def _loop(self) -> None:
        """حلقه‌ی جارو."""
        try:
            while True:
                self.sweep()
                await asyncio.sleep(self.sweep_seconds)
        except asyncio.CancelledError:
            raise

    async def start(self) -> bool:
        """جاروی پس‌زمینه راراه می‌اندازد می‌کند."""
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(self._loop(), name="watchdog-sweep")
        return True

    async def stop(self) -> bool:
        """جارو را متوقف می‌کند."""
        task = self._task
        self._task = None
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001 - در حال لغو است
            await task
        return True

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/diagnostics``."""
        return {
            "default_timeout": self.default_timeout,
            "active": self.active,
            "expired": self.expired(limit=5),
            "expired_total": len(self._expired),
        }


class _TrackingContext:
    """context manager ناهمگام برای :meth:`Watchdog.track`."""

    def __init__(self, watchdog: Watchdog, key: str, *, label: str, timeout: float | None) -> None:
        """Args:
        watchdog: نمونه‌ی watchdog.
        key: کلید یکتا.
        label: برچسب خوانا.
        timeout: مهلت.
        """
        self._watchdog = watchdog
        self._key = key
        self._label = label
        self._timeout = timeout

    def __enter__(self) -> _Tracked:
        """ورود همگام."""
        return self._watchdog.begin(self._key, label=self._label, timeout=self._timeout)

    def __exit__(self, *exc_info: Any) -> None:
        """خروج همگام."""
        self._watchdog.end(self._key)

    async def __aenter__(self) -> _Tracked:
        """ورود ناهمگام."""
        return self._watchdog.begin(self._key, label=self._label, timeout=self._timeout)

    async def __aexit__(self, *exc_info: Any) -> None:
        """خروج ناهمگام."""
        self._watchdog.end(self._key)


class Heartbeat:
    """تیک سلامت دوره‌ای.

    نمونه::

        beat = Heartbeat(notify=center.push)
        beat.add_check("disk", lambda: _disk_check())
        await beat.start()
    """

    def __init__(
        self,
        *,
        interval_seconds: float = 300.0,
        notify: Callable[[str, str, str], None] | None = None,
        disk_min_free_mb: float = 500.0,
        bus: Any = None,
    ) -> None:
        """Args:
        interval_seconds: فاصله‌ی تیک‌ها.
        notify: ``callable(kind, title, body)`` برای مرکز اعلان‌ها.
        disk_min_free_mb: آستانه‌ی هشدار فضای آزاد.
        bus: یک EventBus برای انتشار رویدادها.
        """
        self.interval_seconds = max(5.0, float(interval_seconds))
        self._notify = notify
        self.disk_min_free_mb = max(0.0, float(disk_min_free_mb))
        self._bus = bus
        self._checks: dict[str, HealthCheck] = {}
        self._results: dict[str, CheckResult] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.ticks = 0
        self.degraded_ticks = 0
        self.started_at = 0.0
        self.last_tick = 0.0
        self.add_check("disk_space", self._check_disk_space)

    # ------------------------------------------------------------------ checks
    def add_check(self, name: str, check: HealthCheck) -> None:
        """یک بررسی سلامت ثبت می‌کند."""
        self._checks[str(name)] = check

    def remove_check(self, name: str) -> bool:
        """حذف یک بررسی."""
        self._checks.pop(name, None)
        return self._results.pop(name, None) is not None

    async def _check_disk_space(self) -> CheckResult:
        """فضای آزاد دیسکِ پوشه‌ی کاری."""
        try:
            usage = shutil.disk_usage(".")
        except OSError as exc:
            return CheckResult("disk_space", ok=False, severity="warning", message=f"cannot stat: {exc}")
        free_mb = usage.free / (1024 * 1024)
        ok = free_mb >= self.disk_min_free_mb
        return CheckResult(
            "disk_space",
            ok=ok,
            severity="info" if ok else "warning",
            message=f"{free_mb:.0f} MB free" + ("" if ok else f" (below {self.disk_min_free_mb:.0f} MB)"),
            value=round(free_mb, 1),
        )

    async def run_checks(self) -> list[CheckResult]:
        """همه‌ی بررسی‌ها را اجرا می‌کند.

        Returns:
            نتایج؛ یک بررسی خراب بقیه را متوقف نمی‌کند.
        """
        results: list[CheckResult] = []
        for name, check in list(self._checks.items()):
            try:
                outcome = check()
                if asyncio.iscoroutine(outcome):
                    outcome = await outcome
                result = (
                    outcome
                    if isinstance(outcome, CheckResult)
                    else CheckResult(name, ok=False, message="bad check return")
                )
            except Exception as exc:  # noqa: BLE001 - یک check خراب نباید heartbeat را بگیرد
                result = CheckResult(name, ok=False, severity="error", message=f"{type(exc).__name__}: {exc}")
            result.name = name
            self._results[name] = result
            results.append(result)
        return results

    @property
    def healthy(self) -> bool:
        """آیا همه‌ی بررسی‌های اخیر سالم‌اند؟"""
        return all(result.ok for result in self._results.values()) if self._results else True

    def results(self) -> list[CheckResult]:
        """آخرین نتایج."""
        return list(self._results.values())

    # ------------------------------------------------------------------ loop
    @property
    def running(self) -> bool:
        """آیا حلقه فعال است؟"""
        return self._task is not None and not self._task.done()

    async def tick(self) -> list[CheckResult]:
        """یک تیک: اجرا، ارزیابی، اعلان."""
        self.ticks += 1
        self.last_tick = time.time()
        results = await self.run_checks()
        bad = [result for result in results if not result.ok]
        if bad:
            self.degraded_ticks += 1
            summary = "; ".join(f"{r.name}: {r.message}" for r in bad[:5])
            self._publish("heartbeat.degraded", {"issues": [r.as_dict() for r in bad]})
            if self._notify is not None:
                try:
                    self._notify("heartbeat", f"heartbeat: {len(bad)} issue(s)", summary)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("heartbeat: notify failed: %s", exc)
        else:
            self._publish("heartbeat.ok", {"checks": len(results)})
        return results

    def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        """یک رویداد روی bus منتشر می‌کند (اگر باشد)."""
        if self._bus is None:
            return
        emit = getattr(self._bus, "publish_nowait", None)
        if emit is None:
            return
        try:
            emit(kind, payload, source="heartbeat")
        except Exception as exc:  # noqa: BLE001
            logger.debug("heartbeat: publish failed: %s", exc)

    async def start(self) -> bool:
        """حلقه را در پس‌زمینه راه می‌اندازد.

        Returns:
            ``True`` اگر تازه شروع شد.
        """
        if self.running:
            return False
        self._stopping.clear()
        self.started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="heartbeat")
        return True

    async def stop(self) -> bool:
        """حلقه را متوقف می‌کند."""
        task = self._task
        self._task = None
        self._stopping.set()
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001 - در حال لغو است
            await task
        return True

    async def _loop(self) -> None:
        """حلقه‌ی اصلی."""
        try:
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("heartbeat: tick failed: %s", exc)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/diagnostics``."""
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "ticks": self.ticks,
            "degraded_ticks": self.degraded_ticks,
            "healthy": self.healthy,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "checks": [result.as_dict() for result in self.results()],
        }

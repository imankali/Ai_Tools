"""سیستم رویداد (Event bus) با الگوی Observer.

ایجنت و ابزارها هیچ وابستگی مستقیمی به لاگ، UI یا متریक्स ندارند؛ آن‌ها فقط
رویداد منتشر می‌کنند. هر کسی (CLI، لاگر، متریک، تست) می‌تواند subscribe کند.

ویژگی‌ها:

* هندلرهای sync و async
* تطبیق الگوی wildcard: ``tool.*``، ``#`` (همه)
* جداسازی خطا: خطای یک observer هرگز انتشار را متوقف نمی‌کند
* ثبت اختیاری رویدادها در فایل JSONL (برای ممیزی/audit)
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["EVENTS", "Event", "EventBus", "EventHandler"]

#: نوع هندلرهای مجاز (sync یا async)
EventHandler = Callable[[Any], None] | Callable[[Any], Awaitable[None]]


@dataclass(slots=True)
class Event:
    """یک رویداد منتشرشده روی باس.

    Attributes:
        kind: نام رویداد با namespace نقطه‌ای (مثلاً ``tool.completed``).
        payload: داده‌های آزاد رویداد.
        timestamp: زمان Unix وقوع.
        event_id: شناسه‌ی یکتا (برای ردیابی و تست).
        source: نام ماژول/ابزار تولیدکننده.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = "core"

    @property
    def elapsed_seconds(self) -> float:
        """ثانیه‌های سپری‌شده از وقوع رویداد (برای متریک و تست)."""
        return max(0.0, time.time() - self.timestamp)

    def to_dict(self) -> dict[str, Any]:
        """نسخه‌ی dict برای JSON/لاگ."""
        return asdict(self)

    def to_json(self) -> str:
        """خط JSON آماده‌ی نوشتن در فایل audit."""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


class _EventNames:
    """ثابت‌های نام رویداد (جلوگیری از typo در سراسر کد)."""

    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    LLM_REQUESTED = "llm.requested"
    LLM_RESPONDED = "llm.responded"
    LLM_FAILED = "llm.failed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_APPROVED = "tool.approved"
    TOOL_DENIED = "tool.denied"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    SAFETY_BLOCKED = "safety.blocked"
    SAFETY_CONFIRMATION = "safety.confirmation_requested"
    REGISTRY_CHANGED = "registry.changed"
    ERROR = "error"


EVENTS = _EventNames()


class EventBus:
    """باس رویداد سبک، thread-safe و سازگار با asyncio.

    مثال:
        >>> bus = EventBus()
        >>> seen: list[str] = []
        >>> _ = bus.subscribe("tool.*", lambda event: seen.append(event.kind))
        >>> import asyncio; asyncio.run(bus.emit("tool.completed", {"tool": "x"}))
        >>> seen
        ['tool.completed']
    """

    def __init__(
        self,
        *,
        history_size: int = 200,
        persist_path: str | Path | None = None,
        enabled: bool = True,
    ) -> None:
        """
        Args:
            history_size: تعداد رویدادی که برای بازبینی نگه داشته می‌شود.
            persist_path: فایل JSONL برای نوشتن همه‌ی رویدادها (اختیاری).
            enabled: اگر False باشد publish هیچ کاری نمی‌کند (بدون overhead).
        """
        self._subscribers: dict[str, dict[str, EventHandler]] = defaultdict(dict)
        self._lock = threading.RLock()
        self._history: deque[Event] = deque(maxlen=max(0, history_size))
        self._persist_path = Path(persist_path).expanduser() if persist_path else None
        self.enabled = enabled
        self.published_count = 0

    # ------------------------------------------------------------------
    # اشتراک
    # ------------------------------------------------------------------
    def subscribe(self, pattern: str, handler: EventHandler, *, name: str | None = None) -> str:
        """ثبت یک observer.

        Args:
            pattern: نام رویداد یا الگوی wildcard (``tool.*`` یا ``#``).
            handler: تابع sync یا async که یک :class:`Event` می‌گیرد.
            name: نام دلخواه برای هندلر (برای unsubscribe).

        Returns:
            شناسه‌ی اشتراک که به :meth:`unsubscribe` داده می‌شود.

        Raises:
            TypeError: هندلر callable نباشد.
        """
        if not callable(handler):
            raise TypeError("event handler must be callable")
        handler_name = name or getattr(handler, "__name__", None) or f"handler-{id(handler):x}"
        with self._lock:
            self._subscribers[pattern][handler_name] = handler
        return handler_name

    def unsubscribe(self, pattern: str, name: str) -> bool:
        """حذف یک observer.

        Returns:
            ``True`` اگر اشتراکی حذف شد.
        """
        with self._lock:
            handlers = self._subscribers.get(pattern)
            if not handlers or name not in handlers:
                return False
            handlers.pop(name, None)
            if not handlers:
                self._subscribers.pop(pattern, None)
            return True

    def listeners(self, pattern: str | None = None) -> list[str]:
        """فهرست ``pattern:handler`` های ثبت‌شده (برای دیباگ و تست)."""
        with self._lock:
            items = [
                f"{registered}:{handler_name}"
                for registered, handlers in self._subscribers.items()
                if pattern is None or registered == pattern
                for handler_name in handlers
            ]
        return sorted(items)

    # ------------------------------------------------------------------
    # انتشار
    # ------------------------------------------------------------------
    def _matching(self, kind: str) -> list[EventHandler]:
        """هندلرهای مرتبط با یک رویداد (با احتساب wildcard)."""
        with self._lock:
            handlers: list[EventHandler] = []
            for pattern, registered in list(self._subscribers.items()):
                if pattern == "#" or pattern == kind or fnmatch.fnmatchcase(kind, pattern):
                    handlers.extend(registered.values())
            return handlers

    def publish_nowait(self, kind: str, payload: dict[str, Any] | None = None, *, source: str = "core") -> Event:
        """ساخت و ثبت رویداد بدون await کردن هندلرهای async.

        هندلرهای sync فوراً اجرا می‌شوند و هندلرهای async روی loop جاری
        schedule می‌شوند (در صورت نبود loop، نادیده گرفته می‌شوند).
        """
        event = Event(kind=kind, payload=dict(payload or {}), source=source)
        self._record(event)
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for handler in self._matching(kind):
            try:
                if inspect.iscoroutinefunction(handler):
                    if loop is not None:
                        loop.create_task(self._invoke(handler, event))
                else:
                    handler(event)
            except Exception as exc:  # noqa: BLE001 - observer نباید سیستم را بشکند
                self._handle_observer_error(kind, handler, exc)
        return event

    async def emit(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "core",
        timeout: float | None = 10.0,
    ) -> Event:
        """انتشار رویداد و await کردن همه‌ی هندلرها (sync و async).

        Args:
            kind: نام رویداد.
            payload: داده‌ها.
            source: منبع (نام ابزار/ماژول).
            timeout: سقف زمانی هر هندلر به ثانیه (None = نامحدود).

        Returns:
            رویداد ساخته‌شده.
        """
        event = Event(kind=kind, payload=dict(payload or {}), source=source)
        self._record(event)
        handlers = self._matching(kind)
        if not handlers:
            return event
        coros = [self._invoke(handler, event, timeout=timeout) for handler in handlers]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, Exception):
                self._handle_observer_error(kind, handler, result)
        return event

    async def _invoke(self, handler: EventHandler, event: Event, *, timeout: float | None = 10.0) -> None:
        """اجرای یک هندلر با احترام به sync/async بودن و timeout."""
        try:
            if inspect.iscoroutinefunction(handler):
                awaitable = handler(event)
                if timeout:
                    await asyncio.wait_for(awaitable, timeout)
                else:
                    await awaitable
            else:
                result = handler(event)
                if inspect.isawaitable(result):
                    await (asyncio.wait_for(result, timeout) if timeout else result)
        except asyncio.TimeoutError as exc:  # pragma: no cover - وابسته به زمان
            raise TimeoutError(f"event handler timed out after {timeout}s") from exc

    def _record(self, event: Event) -> None:
        """ثبت در تاریخچه و (اختیاراً) نوشتن در فایل audit."""
        if not self.enabled:
            return
        self.published_count += 1
        self._history.append(event)
        if self._persist_path is not None:
            try:
                self._persist_path.parent.mkdir(parents=True, exist_ok=True)
                with self._persist_path.open("a", encoding="utf-8") as handle:
                    handle.write(event.to_json() + "\n")
            except OSError as exc:  # pragma: no cover - دیسک پر/دسترسی
                self._handle_observer_error("bus.persistence", None, exc)

    @staticmethod
    def _handle_observer_error(kind: str, handler: EventHandler | None, exc: Exception) -> None:
        """لاگ‌کردن خطای observer بدون شکستن جریان اصلی اجرا."""
        from src.utils.logger import get_logger

        name = getattr(handler, "__qualname__", repr(handler)) if handler else "persistence"
        get_logger("core.event_bus").warning("event '%s' handler %s failed: %s", kind, name, exc)

    # ------------------------------------------------------------------
    # بازبینی
    # ------------------------------------------------------------------
    @property
    def history(self) -> tuple[Event, ...]:
        """آخرین رویدادها (تازه‌ترین در انتها)."""
        return tuple(self._history)

    def recent(self, kind_pattern: str = "#", *, limit: int = 10) -> list[Event]:
        """فیلتر تاریخچه با الگو و سقف تعداد.

        Args:
            kind_pattern: نام رویداد یا wildcard.
            limit: حداکثر تعداد نتایج.

        Returns:
            فهرست رویدادها از قدیم به جدید.
        """
        if kind_pattern in ("#", "*"):
            # "#" و "*" به معنای «همه‌ی رویدادها»اند؛ fnmatch کاراکتر # را literal می‌شمارد
            events = list(self._history)
        else:
            events = [event for event in self._history if fnmatch.fnmatchcase(event.kind, kind_pattern)]
        return events[-limit:] if limit > 0 else events

    def wait_for(
        self,
        kind: str,
        *,
        timeout: float = 5.0,
        predicate: Callable[[Event], bool] | None = None,
    ) -> Event:
        """منتظر ماندن تا وقوع یک رویداد (**blocking**، فقط برای تست/اسکریپت).

        این متد نباید داخل event loop فعال صدا زده شود؛ برای کد async
        member‌های :attr:`history` / :meth:`recent` یا یک subscribe معمولی را
        به‌کار ببرید.

        Args:
            kind: نام رویداد مورد انتظار.
            timeout: حداکثر زمان انتظار به ثانیه.
            predicate: شرط اضافی روی payload.

        Returns:
            رویداد دریافت‌شده.

        Raises:
            TimeoutError: رویداد در مهلت مشخص رخ نداد.
        """
        waiter = _SimpleWaiter()

        def _on_event(event: Event) -> None:
            if predicate and not predicate(event):
                return
            if waiter.result is None:
                waiter.result = event
                waiter.done.set()

        subscription = self.subscribe(kind, _on_event, name=f"waiter-{time.time_ns()}")
        try:
            if not waiter.done.wait(timeout):
                raise TimeoutError(f"event '{kind}' was not emitted within {timeout}s")
            result = waiter.result
            assert result is not None  # برای mypy
            return result
        finally:
            self.unsubscribe(kind, subscription)

    def clear(self) -> None:
        """پاک کردن تاریخچه و اشتراک‌ها (برای تست)."""
        with self._lock:
            self._subscribers.clear()
            self._history.clear()
        self.published_count = 0


class _SimpleWaiter:
    """کمکیِ thread-based برای :meth:`EventBus.wait_for` بدون event loop."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Event | None = None


def install_default_observers(
    bus: EventBus,
    *,
    log: bool = True,
    extra: Iterable[tuple[str, EventHandler]] | None = None,
) -> list[str]:
    """وصل کردن observerهای پیش‌فرض (لاگر و شنونده‌های سفارشی).

    Args:
        bus: باس مقصد.
        log: اضافه کردن observer لاگ‌نویس.
        extra: جفت‌های ``(pattern, handler)`` اضافی.

    Returns:
        فهرست شناسه‌های subscription ساخته‌شده.
    """
    subscriptions: list[str] = []
    if log:
        from src.utils.logger import attach_event_bus

        subscriptions.append(attach_event_bus(bus))
    for pattern, handler in extra or []:
        subscriptions.append(bus.subscribe(pattern, handler))
    return subscriptions

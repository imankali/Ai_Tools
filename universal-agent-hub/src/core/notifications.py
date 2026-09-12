"""مرکز اعلان‌ها (Notification Center).

چرا؟
----
تا پیش از این، تنها خروجی «خودانگیخته‌ی» ایجنت یک *تأیید* (approval) بود که
منتظر پاسخ می‌ماند. اما یک Agent OS باید بتواند بدون پرسش چیزی بگوید:
روتین شبانه اجرا شد، heartbeat مشکل پیدا کرد، بکاپ ساخته شد، زنجیره‌ی ممیزی
شکست. این ماژول آن «فید» است.

طراحی:
* JSONL روی دیسک (``~/.universal-agent-hub/notifications.jsonl``) — مثل memory.
* پل روی :class:`~src.core.event_bus.EventBus`: رویدادهای ایجنت خودکار اعلان می‌شوند.
* sink اختیاری webhook (POST) برای اینکه گوشی/دسکتاپ خارج از LAN هم بفهمد.
* هیچ‌وقت secret در title/body نمی‌ماند (redact).
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from pydantic import BaseModel, Field

from src.utils.helpers import redact_secrets
from src.utils.logger import get_logger

__all__ = ["Notification", "NotificationCenter", "NotificationSeverity"]

logger = get_logger("core.notifications")


class NotificationSeverity(str, Enum):
    """شدت اعلان."""

    DEBUG = "debug"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


_SEVERITY_ORDER: dict[NotificationSeverity, int] = {
    NotificationSeverity.DEBUG: 0,
    NotificationSeverity.INFO: 1,
    NotificationSeverity.SUCCESS: 1,
    NotificationSeverity.WARNING: 2,
    NotificationSeverity.ERROR: 3,
    NotificationSeverity.CRITICAL: 4,
}


class Notification(BaseModel):
    """یک اعلان.

    Attributes:
        id: شناسه‌ی یکتا.
        ts: زمان ساخت (UTC ISO-8601).
        kind: دسته (``routine``/``heartbeat``/``tool``/``backup``/``security``/``system``).
        title: عنوان کوتاه.
        body: متن کامل‌تر.
        severity: شدت.
        read: خوانده شده؟
        source: منشأ (``scheduler``/``agent``/``watchdog``/…).
        payload: داده‌ی ساخت‌یافته برای UI.
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:16])
    ts: str = ""
    kind: str = "system"
    title: str = ""
    body: str = ""
    severity: NotificationSeverity = NotificationSeverity.INFO
    read: bool = False
    source: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def rank(self) -> int:
        """رتبه‌ی عددی شدت (برای مرتب‌سازی)."""
        return _SEVERITY_ORDER.get(self.severity, 1)

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return self.model_dump(mode="json")


#: نگاشت رویداد → (kind, severity). فقط رویدادهایی که ارزش اعلان دارند.
_EVENT_MAP: dict[str, tuple[str, NotificationSeverity]] = {
    "agent.run.failed": ("agent", NotificationSeverity.ERROR),
    "agent.model.fallback": ("agent", NotificationSeverity.WARNING),
    "tool.blocked": ("tool", NotificationSeverity.WARNING),
    "tool.failed": ("tool", NotificationSeverity.WARNING),
    "safety.blocked": ("security", NotificationSeverity.WARNING),
    "routine.failed": ("routine", NotificationSeverity.ERROR),
    "routine.fired": ("routine", NotificationSeverity.SUCCESS),
    "backup.created": ("backup", NotificationSeverity.SUCCESS),
    "audit.chain_broken": ("security", NotificationSeverity.CRITICAL),
    "heartbeat.degraded": ("heartbeat", NotificationSeverity.WARNING),
}


class NotificationCenter:
    """فید اعلان‌ها با ذخیره‌سازی JSONL و پل رویداد.

    نمونه::

        center = NotificationCenter(path)
        center.push("routine", "Nightly report ready", severity="success")
        print(center.unread_count())
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_records: int = 500,
        webhook_url: str = "",
        webhook_timeout: float = 5.0,
        sinks: list[Callable[[Notification], None]] | None = None,
    ) -> None:
        """Args:
        path: فایل JSONL اعلان‌ها.
        max_records: سقف رکورد نگه‌داشته‌شده در حافظه/دیسک.
        webhook_url: در صورت تنظیم، هر اعلان POST می‌شود (best-effort).
        webhook_timeout: ثانیه.
        sinks: توابع اضافی که هر اعلان به آن‌ها داده می‌شود.
        """
        self.path = Path(path).expanduser().resolve()
        self.max_records = max(20, int(max_records))
        self.webhook_url = webhook_url.strip()
        self.webhook_timeout = max(0.5, float(webhook_timeout))
        self._sinks: list[Callable[[Notification], None]] = list(sinks or [])
        self._lock = threading.RLock()
        self._items: deque[Notification] = deque(maxlen=self.max_records)
        self._sub_id: str | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        """خواندن رکوردهای موجود (بدون بازنویسی)."""
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._items.append(Notification.model_validate_json(line))
                    except Exception as exc:  # noqa: BLE001 - یک خط خراب نباید فید را ببندد
                        logger.debug("notifications: skipping malformed line: %s", exc)
                        continue
        except OSError as exc:
            logger.warning("notifications: cannot read %s: %s", self.path, exc)

    def _persist(self) -> None:
        """بازنویسی اتمیک کل فید (کوچک است؛ ساده و امن)."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                for item in self._items:
                    handle.write(item.model_dump_json() + "\n")
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("notifications: cannot write %s: %s", self.path, exc)
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ public API
    def push(
        self,
        kind: str,
        title: str,
        body: str = "",
        *,
        severity: NotificationSeverity | str = NotificationSeverity.INFO,
        source: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> Notification:
        """یک اعلان می‌سازد، ذخیره و پخش می‌کند.

        Args:
            kind: دسته‌ی اعلان.
            title: عنوان (secretها redact می‌شوند).
            body: متن.
            severity: شدت (رشته یا enum).
            source: منشأ.
            payload: داده‌ی ساخت‌یافته.

        Returns:
            اعلان ساخته‌شده.
        """
        if isinstance(severity, str):
            try:
                severity = NotificationSeverity(severity.lower())
            except ValueError:
                severity = NotificationSeverity.INFO
        note = Notification(
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            kind=str(kind or "system"),
            title=redact_secrets(str(title or ""))[:200],
            body=redact_secrets(str(body or ""))[:2000],
            severity=severity,
            source=str(source or "system"),
            payload=dict(payload or {}),
        )
        with self._lock:
            self._items.append(note)
            self._persist()
        self._deliver(note)
        return note

    def _deliver(self, note: Notification) -> None:
        """sinkها و webhook را صدا می‌زند؛ هیچ‌کدام نباید استثنا نشت دهند."""
        for sink in list(self._sinks):
            try:
                sink(note)
            except Exception as exc:  # noqa: BLE001 - sink خراب نباید فید را بشکند
                logger.debug("notifications: sink failed: %s", exc)
        if not self.webhook_url:
            return
        try:
            requests.post(
                self.webhook_url,
                json=note.as_dict(),
                timeout=self.webhook_timeout,
                headers={"User-Agent": "universal-agent-hub"},
            )
        except Exception as exc:  # noqa: BLE001 - شبکه نباید جریان اصلی را متوقف کند
            logger.debug("notifications: webhook failed: %s", exc)

    def add_sink(self, sink: Callable[[Notification], None]) -> None:
        """یک sink تازه اضافه می‌کند (مثلاً چاپ در CLI)."""
        self._sinks.append(sink)

    def list(
        self,
        *,
        unread_only: bool = False,
        severity: NotificationSeverity | str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Notification]:
        """اعلان‌ها، جدیدترین اول."""
        if isinstance(severity, str):
            want: str | None = severity.lower()
        elif severity is not None:
            want = severity.value
        else:
            want = None
        with self._lock:
            items = list(self._items)
        items.reverse()
        out: list[Notification] = []
        for item in items:
            if unread_only and item.read:
                continue
            if want and item.severity.value != want:
                continue
            if kind and item.kind != kind:
                continue
            out.append(item)
            if len(out) >= max(1, limit):
                break
        return out

    def unread_count(self) -> int:
        """تعداد اعلان‌های نخوانده."""
        with self._lock:
            return sum(1 for item in self._items if not item.read)

    def mark_read(self, note_id: str) -> bool:
        """یک اعلان را خوانده‌شده می‌کند."""
        with self._lock:
            for item in self._items:
                if item.id == note_id:
                    if item.read:
                        return False
                    item.read = True
                    self._persist()
                    return True
        return False

    def mark_all_read(self) -> int:
        """همه را خوانده‌شده می‌کند؛ تعداد تغییر یافته را برمی‌گرداند."""
        changed = 0
        with self._lock:
            for item in self._items:
                if not item.read:
                    item.read = True
                    changed += 1
            if changed:
                self._persist()
        return changed

    def clear(self, *, read_only: bool = False) -> int:
        """حذف اعلان‌ها.

        Args:
            read_only: اگر ``True`` فقط خوانده‌شده‌ها پاک می‌شوند.
        """
        with self._lock:
            if read_only:
                kept = [item for item in self._items if not item.read]
                removed = len(self._items) - len(kept)
                self._items.clear()
                self._items.extend(kept)
            else:
                removed = len(self._items)
                self._items.clear()
            self._persist()
        return removed

    def stats(self) -> dict[str, Any]:
        """خلاصه برای ``/api/diagnostics``."""
        with self._lock:
            items = list(self._items)
        by_sev: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for item in items:
            by_sev[item.severity.value] = by_sev.get(item.severity.value, 0) + 1
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        return {
            "total": len(items),
            "unread": sum(1 for item in items if not item.read),
            "by_severity": by_sev,
            "by_kind": by_kind,
            "webhook": bool(self.webhook_url),
        }

    # ------------------------------------------------------------------ event bridge
    def attach_bus(self, bus: Any) -> str | None:
        """مرکز را به یک :class:`EventBus` وصل می‌کند.

        رویدادهای نگاشت‌شده در :data:`_EVENT_MAP` خودکار اعلان می‌شوند.

        Args:
            bus: نمونه‌ی EventBus (duck-typed تا import حلقوی نشود).

        Returns:
            شناسه‌ی اشتراک، یا ``None`` اگر وصل نشد.
        """
        if bus is None or not hasattr(bus, "subscribe"):
            return None

        # عمدتاً sync: تا هم با ``emit`` (که await می‌کند) و هم با
        # ``publish_nowait`` (که هندلر async را فقط schedule می‌کند) کار کند.
        # خودِ ``push`` هم sync است، پس چیزی برای await وجود ندارد.
        def _handler(event: Any) -> None:
            mapping = _EVENT_MAP.get(getattr(event, "kind", ""))
            if mapping is None:
                return
            kind, severity = mapping
            payload = getattr(event, "payload", {}) or {}
            title = str(payload.get("title") or payload.get("error") or payload.get("tool") or event.kind)
            self.push(
                kind,
                title,
                body=json.dumps(payload, ensure_ascii=False, default=str)[:800],
                severity=severity,
                source="event-bus",
                payload={"event": event.kind},
            )

        try:
            self._sub_id = str(bus.subscribe("#", _handler, name="notification-center"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("notifications: cannot attach bus: %s", exc)
            return None
        return self._sub_id

    def detach_bus(self, bus: Any) -> bool:
        """اشتراک EventBus را لغو می‌کند."""
        if bus is None or self._sub_id is None:
            return False
        try:
            ok = bool(bus.unsubscribe("#", self._sub_id))
        except Exception:  # noqa: BLE001
            return False
        self._sub_id = None
        return ok

"""Session های ایجنت روی سرور: صف اجرا، رویدادها و پل تأیید.

یک :class:`AgentSession` یک ایجنت مستقل است (config + profile + ابزارهای خودش)
که از چند زبانه/چند دستگاه هم‌زمان قابل استفاده است:

* اجراها روی هر session **سریالی** می‌شوند (یک request در حال اجرا) تا تاریخچه‌ی
  گفت‌وگو قاطی نشود؛ برای کار موازی، session تازه بگیرید.
* رویدادهای ایجنت/ابزار در یک حلقه‌ی کوچک نگه داشته می‌شوند تا هم polling و هم
  WebSocket همان داده را ببینند.
* تأیید عملیات حساس (``safety_check`` → confirmation) به‌جای ترمینال، روی سیم
  می‌آید: :class:`ApprovalBroker` یک آينده ثبت می‌کند و تا پاسخ کاربر (یا
  پایان مهلت) صبر می‌کند؛ **پایان مهلت یعنی رد** — نه اجرا.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from src.config import Config
from src.core.base_tool import ConfirmationRequest
from src.core.event_bus import EventBus
from src.utils.helpers import redact_secrets, truncate_text
from src.utils.logger import get_logger

__all__ = ["AgentSession", "ApprovalBroker", "PendingApproval", "SessionRegistry"]

#: تسک‌های reset در انتظار (جلوگیری از آزادسازی زودهنگام — RUF006)
_PENDING_RESETS: set[asyncio.Future[Any]] = set()

#: فیلدهایی که اپ می‌تواند برای یک session بازنویسی کند (هرگز کلید API اینجا نیست)
SESSION_OVERRIDES: tuple[str, ...] = (
    "model_name",
    "temperature",
    "max_tool_iterations",
    "max_output_tokens",
    "enable_confirmation",
    "parallel_tool_calls",
    "max_output_chars",
    "dangerous_command_policy",
)
#: نام‌های پروفایل‌پذیر که در session نگه داشته می‌شوند (روی config اعمال نمی‌شوند)
SESSION_SETTINGS: tuple[str, ...] = ("profile", "tools", "system_note")


class PendingApproval:
    """یک درخواست تأیید در انتظار پاسخ."""

    def __init__(self, request_id: str, payload: dict[str, Any], future: asyncio.Future[bool]) -> None:
        self.request_id = request_id
        self.payload = payload
        self.created_at = time.time()
        self.future = future

    @property
    def resolved(self) -> bool:
        """آیا کاربر پاسخ داده است؟"""
        return self.future.done()

    def to_dict(self) -> dict[str, Any]:
        """نسخه‌ی JSON برای اپ (تخت، همراه با ``kind`` تا فریم WS خودتوضیح باشد)."""
        return {
            "kind": "approval_request",
            **self.payload,
            "request_id": self.request_id,
            "created_at": self.created_at,
        }


class ApprovalBroker:
    """پل میان «callback تأیید ایجنت» و «پیام‌های WebSocket/REST اپ»."""

    def __init__(self, *, timeout: float = 180.0, poll_grace: float = 30.0) -> None:
        self.timeout = float(timeout)
        self.poll_grace = float(poll_grace)
        self.last_poll = 0.0
        self._pending: dict[str, PendingApproval] = {}
        self._publisher: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._probe: Callable[[], bool] | None = None
        self.logger = get_logger("server.approvals")
        self.history: list[dict[str, Any]] = []

    def bind_publisher(
        self,
        publisher: Callable[[dict[str, Any]], Awaitable[None]] | None,
        *,
        probe: Callable[[], bool] | None = None,
    ) -> None:
        """تنظیم تابع ارسال پیام و «آشکارساز کلاینت» (``None`` = قطع اشتراک)."""
        self._publisher = publisher
        self._probe = probe

    def touch(self) -> None:
        """ثبت اینکه اپ از طریق REST وضعیت تأییدها را چک کرده (پنجره‌ی پاسخ)."""
        self.last_poll = time.time()

    @property
    def has_channel(self) -> bool:
        """آیا همین حالا کلاینتی هست که بتواند تأیید را ببیند؟

        اگر نه، درخواست تأیید **بی‌درنگ رد** می‌شود؛ ایجنت نباید ۱۸۰ ثانیه برای
        مخاطبی که وجود ندارد منتظر بماند (هم‌رفتار با CLI بدون TTY).
        """
        if self._publisher is None:
            return False
        if self._probe is not None and self._probe():
            return True
        return (time.time() - self.last_poll) < self.poll_grace

    @property
    def pending(self) -> list[dict[str, Any]]:
        """درخواست‌های بی‌پاسخ (برای replay هنگام اتصال تازه)."""
        return [item.to_dict() for item in self._pending.values()]

    def resolve(self, request_id: str, approved: bool, *, reason: str = "") -> bool:
        """ثبت پاسخ کاربر. نتیجه ``True`` است اگر درخواست هنوز باز بود."""
        record = self._pending.pop(str(request_id or ""), None)
        if record is None:
            return False
        self.history.append(
            {"request_id": record.request_id, "approved": bool(approved), "reason": reason[:200], "at": time.time()}
        )
        if not record.future.done():
            record.future.set_result(bool(approved))
        return True

    def cancel_all(self, *, reason: str = "session closed") -> int:
        """رد کردن همه‌ی درخواست‌های باز (مثلاً وقتی WS بسته می‌شود)."""
        count = 0
        for request_id in list(self._pending):
            if self.resolve(request_id, False, reason=reason):
                count += 1
        return count

    async def request(self, payload: dict[str, Any]) -> bool:
        """ارسال درخواست به اپ و منتظر پاسخ ماندن (پایان مهلت = رد)."""
        request_id = f"apv-{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        if not self.has_channel:
            self.logger.debug("approval %s auto-denied: no client channel to ask", request_id)
            return False
        record = PendingApproval(request_id, payload, future)
        self._pending[request_id] = record
        if self._publisher is not None:
            try:
                await self._publisher(record.to_dict())
            except Exception as exc:  # noqa: BLE001 - نرسیدن پیام یعنی کاربر نمی‌بیند
                self.logger.debug("approval publish failed: %s", exc)
        else:
            self.logger.debug("no client attached; approval %s will time out", request_id)
        try:
            return bool(await asyncio.wait_for(future, timeout=self.timeout))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        finally:
            self._pending.pop(request_id, None)

    async def confirm_callback(self, request: ConfirmationRequest) -> bool:
        """امضای مورد انتظار :class:`src.agent.UniversalAgent` (sync یا async)."""
        return await self.request(serialize_confirmation(request))

    def snapshot(self) -> dict[str, Any]:
        """وضعیت برای UI."""
        return {
            "pending": len(self._pending),
            "timeout_seconds": self.timeout,
            "decisions": len(self.history),
            "client_attached": self.has_channel,
        }


def serialize_confirmation(request: ConfirmationRequest) -> dict[str, Any]:
    """تبدیل درخواست تأیید به JSON قابل نمایش (مقادیر کوتاه و ماسک‌شده)."""
    details: dict[str, Any] = {}
    for key, value in dict(getattr(request, "details", {}) or {}).items():
        text = value if isinstance(value, str) else repr(value)
        details[str(key)] = truncate_text(redact_secrets(text), 900)[0]
    return {
        "kind": "approval_request",
        "request_id": "",
        "tool": getattr(request, "tool", "?"),
        "action": getattr(request, "action", ""),
        "summary": redact_secrets(str(getattr(request, "summary", "") or ""))[:600],
        "risk": getattr(getattr(request, "risk", None), "value", "medium"),
        "details": details,
    }


class AgentSession:
    """یک ایجنت + تاریخچه + صف رویداد، قابل اتصال از چند کلاینت."""

    def __init__(
        self,
        session_id: str,
        config: Config,
        *,
        profile: str = "generalist",
        tools: list[str] | None = None,
        system_note: str = "",
        max_events: int = 200,
        keystore: Any = None,
    ) -> None:
        self.id = session_id
        self.base_config = config
        self.keystore = keystore
        self.overrides: dict[str, Any] = {}
        self.profile = profile
        self.tools = list(tools) if tools else None
        self.system_note = system_note
        self.created_at = time.time()
        self.last_used = time.time()
        self.busy = False
        self.logger = get_logger(f"server.session.{session_id[:8]}")
        self.approvals = ApprovalBroker(timeout=float(getattr(config, "server_approval_timeout", 180) or 180))
        # پل تأیید به session تعلق دارد (نه به ایجنت): فراخوانی مستقیم ابزار هم
        # باید همان کلاینت‌های وصل‌شده را صدا بزند.
        self.approvals.bind_publisher(self._broadcast, probe=lambda: bool(self.subscribers))
        self.events: deque[dict[str, Any]] = deque(maxlen=max(20, int(max_events)))
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._agent: Any = None
        self._agent_config_hash: tuple[Any, ...] = ()
        self.run_count = 0
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # ایجنت
    # ------------------------------------------------------------------
    @property
    def config(self) -> Config:
        """تنظیمات مؤثر: config پایه + پروفایل فعال کلیدها + override های این session.

        کلید API فقط همین‌جا و فقط در حافظه وارد می‌شود؛ هیچ‌وقت از API بیرون
        نمی‌رود (``/api/config`` و ``/api/status`` نسخه‌ی ماسک‌شده‌ی base را می‌دهند).
        """
        updates: dict[str, Any] = {}
        if self.keystore is not None:
            updates.update(self.keystore.active_config_values())
        updates.update({key: value for key, value in self.overrides.items() if key in SESSION_OVERRIDES})
        if not updates:
            return self.base_config
        try:
            return self.base_config.model_copy(update=updates)
        except (ValueError, TypeError):  # pragma: no cover - محافظ در برابر فیلد نامعتبر
            self.logger.debug("ignored invalid session config updates: %s", sorted(updates))
            return self.base_config

    @property
    def agent(self) -> Any:
        """ایجنت (lazily ساخته می‌شود و با تغییر پروفایل/ابزارها بازسازی می‌شود)."""
        signature = (self.profile, tuple(self.tools or ()), tuple(sorted(self.overrides.items())))
        if self._agent is None or signature != self._agent_config_hash:
            self._agent = self._build_agent()
            self._agent_config_hash = signature
        return self._agent

    def _build_agent(self) -> Any:
        """ساخت ایجنت از کارخانه، با callback تأیید روی سیم."""
        from src.core.agent_factory import AgentFactory

        bus = EventBus(history_size=200, persist_path=self.config.event_log_path)
        bus.subscribe("#", self._on_event_sync)
        agent = AgentFactory.create(
            self.profile,
            config=self.config,
            tools=self.tools,
            event_bus=bus,
            log=False,
            confirm_callback=self.approvals.confirm_callback,
        )
        return agent

    def invalidate_agent(self) -> None:
        """اجبار به بازسازی ایجنت (پس از تغییر پروفایل/ابزارها)."""
        self._agent_config_hash = ()

    @property
    def context(self) -> Any:
        """زمینه‌ی اجرای ابزارهای این session (تأیید هم به همین broker می‌رود)."""
        return self.agent._context  # noqa: SLF001 - ایجنت متعلق به همین session است

    # ------------------------------------------------------------------
    # رویدادها
    # ------------------------------------------------------------------
    def _on_event_sync(self, event: Any) -> None:
        """ثبت رویداد باس در حلقه‌ی رویدادها و پخش به مشترکین."""
        payload = {
            "kind": "event",
            "event": getattr(event, "kind", str(event)),
            "source": getattr(event, "source", ""),
            "at": getattr(event, "timestamp", time.time()),
            "payload": {
                str(key): (truncate_text(redact_secrets(value), 400)[0] if isinstance(value, str) else value)
                for key, value in dict(getattr(event, "payload", {}) or {}).items()
            },
        }
        self.events.append(payload)
        for queue in list(self.subscribers):
            if queue.qsize() > 500:  # مشترک کند نباید حافظه را بترکاند
                continue
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        """پخش یک پیام آماد (approval) به همه‌ی مشترکین."""
        for queue in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({**message})

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """ثبت یک مشترک رویداد (WebSocket یا SSE)."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """لغو اشتراک (بی‌خطر اگر قبلاً حذف شده باشد)."""
        self.subscribers.discard(queue)

    def recent_events(self, *, limit: int = 40, since: float = 0.0) -> list[dict[str, Any]]:
        """رویدادهای اخیر (اختیاراً فقط جدیدتر از یک timestamp)."""
        items = (
            [event for event in self.events if float(event.get("at") or 0.0) > since] if since else list(self.events)
        )
        return items[-max(1, int(limit)) :]

    # ------------------------------------------------------------------
    # اجرا
    # ------------------------------------------------------------------
    async def run(self, prompt: str, *, timeout: float = 900.0) -> dict[str, Any]:
        """اجرای یک درخواست (سریالی روی همین session) و بازگرداندن نتیجه‌ی JSON."""
        text = str(prompt or "").strip()
        if not text:
            raise ValueError("prompt must not be empty")
        async with self._lock:
            self.busy = True
            self.last_used = time.time()
            current = asyncio.get_running_loop()
            started = time.monotonic()
            try:
                task = current.create_task(self._invoke(text))
                self._task = task
                result = await asyncio.wait_for(task, timeout=timeout)
                self.run_count += 1
                self.last_error = ""
                return {**result, "session_id": self.id, "duration_ms": int((time.monotonic() - started) * 1000)}
            except asyncio.TimeoutError:
                self.last_error = f"run timed out after {int(timeout)}s"
                self.logger.warning("%s", self.last_error)
                return {
                    "ok": False,
                    "text": "",
                    "error": self.last_error,
                    "tool_calls": [],
                    "iterations": 0,
                    "session_id": self.id,
                }
            except asyncio.CancelledError:
                self.last_error = "cancelled by client"
                return {
                    "ok": False,
                    "text": "",
                    "error": self.last_error,
                    "tool_calls": [],
                    "iterations": 0,
                    "session_id": self.id,
                }
            except Exception as exc:  # noqa: BLE001 - نتیجه باید به اپ برسد نه HTTP 500
                self.last_error = redact_secrets(f"{type(exc).__name__}: {exc}")[:600]
                self.logger.warning("run failed: %s", self.last_error)
                return {
                    "ok": False,
                    "text": "",
                    "error": self.last_error,
                    "tool_calls": [],
                    "iterations": 0,
                    "session_id": self.id,
                }
            finally:
                self._task = None
                self.busy = False
                self.last_used = time.time()

    async def _invoke(self, text: str) -> dict[str, Any]:
        """فراخوانی ایجنت و serialize کردن نتیجه."""
        note = self.system_note or f"connected via Universal Agent Hub server · session={self.id}"
        result = await self.agent.ask(text, system_note=note)
        dump: dict[str, Any] = result.model_dump(mode="json")
        dump["model"] = getattr(self.agent, "model", dump.get("model", ""))
        dump["session_id"] = self.id
        return dump

    def stop(self) -> bool:
        """لغو اجرای جاری (اگر چیزی در حال اجرا باشد)."""
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        self.approvals.cancel_all(reason="run cancelled")
        return True

    # ------------------------------------------------------------------
    # تاریخچه و تنظیمات
    # ------------------------------------------------------------------
    def history(self, *, limit: int = 40) -> list[dict[str, Any]]:
        """پیام‌های گفت‌وگو (برای نمایش در اپ)."""
        messages = list(getattr(self.agent.state, "messages", []) or [])
        return [
            {
                "role": message.role,
                "content": truncate_text(redact_secrets(str(message.content or "")), 1200)[0],
                "at": getattr(message, "timestamp", None),
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments}
                    for call in (getattr(message, "tool_calls", None) or [])
                ],
                "name": getattr(message, "name", None),
            }
            for message in messages[-max(1, int(limit)) :]
        ]

    def apply_updates(self, updates: dict[str, Any]) -> dict[str, Any]:
        """اعمال تغییرات مجاز (پروفایل/ابزارها/فیلدهای config) و بازگشت وضعیت جدید."""
        changed: dict[str, Any] = {}
        rebuild = False
        for key, value in dict(updates or {}).items():
            if key == "profile":
                name = str(value or "generalist").strip()
                from src.core.agent_factory import AgentFactory

                if name not in AgentFactory.profiles():
                    raise KeyError(
                        f"unknown profile '{name}'. Available: {', '.join(sorted(AgentFactory.profiles()))}"
                    )
                rebuild = rebuild or name != self.profile
                self.profile = name
                changed["profile"] = name
            elif key == "tools":
                wanted = (
                    None
                    if value in (None, "", [], "all")
                    else [str(item).strip() for item in value if str(item).strip()]
                )
                rebuild = rebuild or wanted != self.tools
                self.tools = wanted
                changed["tools"] = wanted
            elif key == "system_note":
                self.system_note = truncate_text(str(value or ""), 4000)[0]
                changed["system_note"] = self.system_note
            elif key in SESSION_OVERRIDES:
                if value is None:
                    self.overrides.pop(key, None)
                else:
                    self.overrides[key] = value
                rebuild = True
                changed[key] = value
            else:
                raise KeyError(f"cannot change '{key}' from the app")
        if rebuild:
            self.invalidate_agent()
        return changed

    def reset(self) -> None:
        """پاک کردن تاریخچه‌ی گفت‌وگو (ابزارها و تنظیمات می‌مانند)."""
        agent = self.agent
        reset = getattr(agent, "reset_history", None)
        if callable(reset):
            outcome = reset()
            if inspect.isawaitable(outcome):  # pragma: no cover - reset_history sync است
                # ایجنت‌های سفارشی ممکن است reset تازه async داشته باشند؛ ارجاع را نگه
                # می‌داریم تا زباله‌روب تسک را وسط کار آزاد نکند.
                task = asyncio.ensure_future(outcome)
                _PENDING_RESETS.add(task)
                task.add_done_callback(_PENDING_RESETS.discard)
        self.events.clear()

    def describe(self) -> dict[str, Any]:
        """خلاصه‌ی session برای UI."""
        info: dict[str, Any] = {
            "session_id": self.id,
            "profile": self.profile,
            "tools": list(self.tools) if self.tools else None,
            "overrides": dict(self.overrides),
            "busy": self.busy,
            "run_count": self.run_count,
            "last_error": self.last_error or None,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "idle_seconds": round(max(0.0, time.time() - self.last_used), 1),
            "subscribers": len(self.subscribers),
            "approvals": self.approvals.snapshot(),
            "events": len(self.events),
        }
        try:
            agent = self.agent
            info["model"] = getattr(agent, "model", "")
            info["model_info"] = agent.model_info
            info["active_tools"] = [item["name"] for item in agent.describe_tools()]
        except Exception as exc:  # noqa: BLE001 - توصیف نباید API را بشکند
            info["agent_error"] = redact_secrets(str(exc))[:200]
        return info

    @property
    def age_seconds(self) -> float:
        """عمر session از آخرین استفاده (برای TTL)."""
        return max(0.0, time.time() - self.last_used)

    def __repr__(self) -> str:
        """نمایش دیباگ."""
        return f"<AgentSession {self.id} profile={self.profile} busy={self.busy} runs={self.run_count}>"


class SessionRegistry:
    """ساخت/یافت/ضایع‌کردن session ها (TTL از config)."""

    def __init__(self, config: Config, *, ttl: float | None = None, keystore: Any = None) -> None:
        self.config = config
        self.keystore = keystore
        self.ttl = float(ttl if ttl is not None else getattr(config, "server_session_ttl", 3600) or 3600)
        self._sessions: dict[str, AgentSession] = {}
        self.logger = get_logger("server.sessions")

    def get(self, session_id: str, **kwargs: Any) -> AgentSession:
        """دریافت session موجود یا ساخت تازه (id تهی = ساخت تازه)."""
        key = str(session_id or "").strip()
        self.expire_stale()
        if key and key in self._sessions:
            session = self._sessions[key]
            if kwargs:
                session.apply_updates(kwargs)
            session.last_used = time.time()
            return session
        new_id = key or f"sess-{uuid.uuid4().hex[:10]}"
        session = AgentSession(
            new_id,
            self.config,
            profile=str(kwargs.pop("profile", None) or "generalist"),
            tools=kwargs.pop("tools", None),
            system_note=str(kwargs.pop("system_note", None) or ""),
            keystore=self.keystore,
        )
        if kwargs:
            session.apply_updates(kwargs)
        self._sessions[new_id] = session
        self.logger.debug("created session %s (profile=%s)", new_id, session.profile)
        return session

    def peek(self, session_id: str) -> AgentSession | None:
        """دریافت بدون ساخت؛ برای stop/describe."""
        return self._sessions.get(str(session_id or "").strip())

    def ids(self) -> list[str]:
        """همه‌ی شناسه‌های فعال."""
        return sorted(self._sessions)

    def all(self) -> list[AgentSession]:
        """همه‌ی session ها (برای ``/api/sessions``)."""
        return list(self._sessions.values())

    def remove(self, session_id: str) -> bool:
        """بستن و حذف یک session."""
        session = self._sessions.pop(str(session_id or "").strip(), None)
        if session is None:
            return False
        session.stop()
        session.approvals.cancel_all(reason="session removed")
        session.approvals.bind_publisher(None)
        for queue in list(session.subscribers):
            session.unsubscribe(queue)
        return True

    def expire_stale(self) -> int:
        """حذف session های بی‌کارِ قدیمی. تعداد حذف‌شده برمی‌گردد."""
        if self.ttl <= 0:
            return 0
        stale = [
            key for key, session in self._sessions.items() if not session.busy and session.age_seconds > self.ttl
        ]
        for key in stale:
            self.remove(key)
        if stale:
            self.logger.debug("expired sessions: %s", ", ".join(stale))
        return len(stale)

    async def aclose(self) -> None:
        """آزادسازی منابع همه‌ی session ها (برای shutdown سرور)."""
        for session in list(self._sessions.values()):
            session.stop()
            session.approvals.cancel_all(reason="server shutdown")
            with contextlib.suppress(Exception):
                await session.agent.close()
        self._sessions.clear()

    def __len__(self) -> int:
        """تعداد session ها."""
        return len(self._sessions)

    def __repr__(self) -> str:
        """نمایش دیباگ."""
        return f"<SessionRegistry {len(self._sessions)} sessions ttl={int(self.ttl)}s>"

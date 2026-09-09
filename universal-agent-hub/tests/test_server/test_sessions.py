"""تست session های ایجنت، پل تأیید و رجیستری."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from src.core.base_tool import ConfirmationRequest
from src.core.event_bus import Event
from src.server.keystore import ApiKeyProfile, KeyStore
from src.server.sessions import AgentSession, ApprovalBroker, SessionRegistry, serialize_confirmation


class TestSerializeConfirmation:
    """ساخت payload قابل نمایش برای اپ."""

    def test_shape(self) -> None:
        """همه‌ی فیلدهای لازم UI وجود دارند."""
        request = ConfirmationRequest(
            tool="terminal_run", action="terminal.run", summary="list files", details={"command": "ls -la"}
        )
        payload = serialize_confirmation(request)
        assert payload["kind"] == "approval_request"
        assert payload["tool"] == "terminal_run"
        assert payload["summary"] == "list files"
        assert payload["details"] == {"command": "ls -la"}
        assert payload["risk"] in {"safe", "low", "medium", "high", "critical"}
        assert payload["request_id"] == ""

    def test_secrets_masked_and_truncated(self) -> None:
        """کلیدها ماسک و متن‌های بلند بریده می‌شوند."""
        request = ConfirmationRequest(
            tool="browser_fill_form",
            action="form.fill",
            summary="x",
            details={"value": "sk-proj-abcdefghij123456", "blob": "y" * 5000},
        )
        payload = serialize_confirmation(request)
        assert "abcdefghij123456" not in json.dumps(payload)
        assert len(payload["details"]["blob"]) <= 901

    def test_non_string_details_are_reprd(self) -> None:
        """مقادیر غیرمتنی به repr تبدیل می‌شوند."""
        payload = serialize_confirmation(
            ConfirmationRequest(tool="t", action="a", details={"timeout": 30, "flags": ["a", "b"]})
        )
        assert payload["details"]["timeout"] == "30"
        assert "'a'" in payload["details"]["flags"]


class TestApprovalBroker:
    """پل تأیید (هسته‌ی کنترل انسانی)."""

    async def test_denies_without_channel(self) -> None:
        """کلاینتی نباشد → رد بی‌درنگ (نه انتظار ۱۸۰ ثانیه)."""
        broker = ApprovalBroker(timeout=60)
        assert broker.has_channel is False
        assert await broker.request({"tool": "terminal_run"}) is False
        assert broker.pending == []

    async def test_publishes_and_waits_for_answer(self) -> None:
        """پیام به اپ می‌رسد و نتیجه از پاسخ کاربر می‌آید."""
        broker = ApprovalBroker(timeout=5)
        sent: list[dict[str, Any]] = []

        async def publish(message: dict[str, Any]) -> None:
            sent.append(message)

        broker.bind_publisher(publish, probe=lambda: True)
        assert broker.has_channel is True
        task = asyncio.create_task(broker.request({"tool": "delete_file", "summary": "remove"}))
        for _ in range(600):
            if sent:
                break
            await asyncio.sleep(0.005)
        assert sent and sent[0]["tool"] == "delete_file"
        request_id = sent[0]["request_id"]
        assert broker.pending[0]["request_id"] == request_id
        assert broker.resolve(request_id, True) is True
        assert await task is True
        assert broker.pending == []
        assert broker.history[-1]["approved"] is True

    async def test_publish_failure_is_not_fatal(self) -> None:
        """خطای ارسال پیام باعث انفجار ایجنت نمی‌شود (فقط رد می‌شود)."""
        broker = ApprovalBroker(timeout=0.05)

        async def boom(_message: dict[str, Any]) -> None:
            raise RuntimeError("socket closed")

        broker.bind_publisher(boom, probe=lambda: True)
        assert await broker.request({"tool": "x"}) is False

    async def test_timeout_denies(self) -> None:
        """پایان مهلت یعنی رد (نه اجرا)."""
        broker = ApprovalBroker(timeout=0.05)
        broker.bind_publisher(asyncio.sleep if False else (lambda _m: asyncio.sleep(0)), probe=lambda: True)
        assert await broker.request({"tool": "x"}) is False

    async def test_resolve_unknown_or_twice(self) -> None:
        """پاسخ تکراری یا نامعلوم چیزی را تغییر نمی‌دهد."""
        broker = ApprovalBroker()
        assert broker.resolve("nope", True) is False
        assert broker.history == []

    async def test_cancel_all(self) -> None:
        """``cancel_all`` همه‌ی درخواست‌های باز را رد می‌کند."""
        broker = ApprovalBroker(timeout=5)
        broker.bind_publisher(lambda _m: asyncio.sleep(0), probe=lambda: True)
        first = asyncio.create_task(broker.request({"tool": "a"}))
        second = asyncio.create_task(broker.request({"tool": "b"}))
        for _ in range(600):
            if len(broker.pending) == 2:
                break
            await asyncio.sleep(0.005)
        assert broker.cancel_all(reason="client gone") == 2
        assert await first is False and await second is False
        assert broker.pending == []

    def test_rest_poll_opens_a_window(self) -> None:
        """اپ REST‌محور با polling هم شانس پاسخ دادن پیدا می‌کند."""
        broker = ApprovalBroker(timeout=5, poll_grace=30)
        broker.bind_publisher(lambda _m: asyncio.sleep(0))
        assert broker.has_channel is False
        broker.touch()
        assert broker.has_channel is True

    def test_snapshot(self) -> None:
        """خلاصه‌ی وضعیت برای UI."""
        broker = ApprovalBroker(timeout=42)
        snapshot = broker.snapshot()
        assert snapshot == {"pending": 0, "timeout_seconds": 42.0, "decisions": 0, "client_attached": False}

    async def test_callback_accepts_request_object(self) -> None:
        """امضای سازگار با ایجنت (ConfirmationRequest می‌گیرد)."""
        broker = ApprovalBroker(timeout=5)
        seen: list[dict[str, Any]] = []
        broker.bind_publisher(seen.append, probe=lambda: True)
        task = asyncio.create_task(
            broker.confirm_callback(ConfirmationRequest(tool="move_file", action="fs.move", summary="move"))
        )
        for _ in range(600):
            if seen:
                break
            await asyncio.sleep(0.005)
        broker.resolve(seen[0]["request_id"], False, reason="no thanks")
        assert await task is False
        assert seen[0]["tool"] == "move_file"


class TestAgentSession:
    """یک session کامل (config، رویدادها، اجرا)."""

    def test_config_applies_only_allowed_overrides(self, app_config: Any) -> None:
        """فیلدهای مجاز اعمال و بقیه نادیده گرفته می‌شوند."""
        session = AgentSession("s1", app_config)
        session.overrides.update({"temperature": 0.1, "openai_api_key": "sk-hacked-123456", "model_name": "tiny"})
        assert session.config.temperature == 0.1
        assert session.config.model_name == "tiny"
        assert session.config.openai_api_key == app_config.openai_api_key

    def test_keystore_profile_overlays_config(self, app_config: Any, tmp_path: Any) -> None:
        """پروفایل فعال کلید، مدل/base_url/کلید را روی session می‌گذارد."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("work", api_key="sk-work-123456789", base_url="http://gw:1/v1", model="gpt-x")
        session = AgentSession("s2", app_config, keystore=store)
        assert session.config.model_name == "gpt-x"
        assert session.config.openai_api_key == "sk-work-123456789"
        # override دستی بر keystore اولویت دارد
        session.overrides["model_name"] = "gpt-y"
        assert session.config.model_name == "gpt-y"

    def test_describe_without_running(self, app_config: Any) -> None:
        """describe همه‌ی اطلاعات UI‌محور را دارد و رازی بیرون نمی‌دهد."""
        session = AgentSession("s3", app_config, profile="read_only")
        info = session.describe()
        assert info["session_id"] == "s3"
        assert info["profile"] == "read_only"
        assert info["busy"] is False and info["run_count"] == 0
        assert info["model"] == app_config.model_name
        from src.core.agent_factory import AgentFactory  # noqa: PLC0415 - فقط برای محاسبه‌ی انتظار

        assert info["active_tools"] == AgentFactory.tools_for("read_only")
        assert "terminal_run" not in info["active_tools"]
        assert "openai_api_key" not in json.dumps(info)

    async def test_run_requires_prompt(self, app_config: Any) -> None:
        """پرامپت خالی یعنی خطای برنامه‌ی کاربر."""
        session = AgentSession("s4", app_config)
        with pytest.raises(ValueError, match="must not be empty"):
            await session.run("   ")

    async def test_run_serialises_result_and_events(self, app_config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """نتایج به JSON تبدیل و session_id افزوده می‌شود."""

        async def fake_invoke(self: AgentSession, text: str) -> dict[str, Any]:
            self._on_event_sync(Event(kind="tool.completed", payload={"tool": "os_info", "ok": True}))
            return {"text": f"echo {text}", "ok": True, "iterations": 2, "tool_calls": [], "usage": {}}

        monkeypatch.setattr(AgentSession, "_invoke", fake_invoke)
        session = AgentSession("s5", app_config)
        queue = session.subscribe()
        result = await session.run("hi")
        assert result["text"] == "echo hi" and result["session_id"] == "s5" and result["ok"] is True
        assert session.run_count == 1 and session.busy is False
        assert session.recent_events()[-1]["event"] == "tool.completed"
        assert queue.qsize() >= 1
        session.unsubscribe(queue)

    async def test_run_surfaces_agent_errors(self, app_config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """خطای ایجنت به‌شکل نتیجه‌ی ناموفق می‌آید، نه استثنا."""

        async def boom(self: AgentSession, text: str) -> dict[str, Any]:
            raise RuntimeError("APIConnectionError with key sk-live-abcdefghij123456")

        monkeypatch.setattr(AgentSession, "_invoke", boom)
        session = AgentSession("s6", app_config)
        result = await session.run("hi")
        assert result["ok"] is False
        assert "APIConnectionError" in result["error"]
        assert "abcdefghij123456" not in result["error"]
        assert "APIConnectionError" in session.last_error

    async def test_run_timeout_reports_error(self, app_config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """مهلت اجرا به پیام تمیز تبدیل می‌شود."""

        async def slow(self: AgentSession, text: str) -> dict[str, Any]:
            await asyncio.sleep(1)
            return {"text": "late", "ok": True}

        monkeypatch.setattr(AgentSession, "_invoke", slow)
        session = AgentSession("s7", app_config)
        result = await session.run("hi", timeout=0.05)
        assert result["ok"] is False and "timed out after 0s" in result["error"]

    def test_stop_without_run(self, app_config: Any) -> None:
        """توقف وقتی چیزی اجرا نمی‌شود، False است."""
        assert AgentSession("s8", app_config).stop() is False

    async def test_stop_cancels_running_task(self, app_config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """``stop`` اجرای جاری را لغو می‌کند و تأییدها را رد می‌کند."""

        async def forever(self: AgentSession, text: str) -> dict[str, Any]:
            await asyncio.sleep(5)
            return {"text": "", "ok": True}

        monkeypatch.setattr(AgentSession, "_invoke", forever)
        session = AgentSession("s9", app_config)
        task = asyncio.create_task(session.run("hi"))
        for _ in range(600):
            if session.busy:
                break
            await asyncio.sleep(0.005)
        assert session.stop() is True
        result = await task
        assert result["ok"] is False and result["error"] == "cancelled by client"

    def test_history_redacts_and_limits(self, app_config: Any) -> None:
        """تاریخچه از state ایجنت خوانده و ماسک می‌شود."""
        session = AgentSession("s10", app_config)
        session.agent.state.messages.append(  # type: ignore[attr-defined]
            type(
                "M",
                (),
                {
                    "role": "user",
                    "content": "key sk-proj-abcdefghij123456",
                    "tool_calls": [],
                    "name": None,
                    "timestamp": 1.0,
                },
            )()
        )
        first = session.history(limit=1)[0]
        assert first["role"] == "user"
        assert "abcdefghij123456" not in first["content"]

    def test_recent_events_window_and_since(self, app_config: Any) -> None:
        """حلقه‌ی رویدادها محدود است و ``since`` فیلتر می‌کند."""
        session = AgentSession("s11", app_config, max_events=3)
        for index in range(25):
            session._on_event_sync(Event(kind="tool.completed", payload={"i": index}, timestamp=100.0 + index))
        # کف حلقه ۲۰ رویداد است (حتی اگر max_events کوچک‌تر داده شود)
        kept = [event["payload"]["i"] for event in session.recent_events()]
        assert len(kept) == 20 and kept[-1] == 24
        assert [event["payload"]["i"] for event in session.recent_events(limit=2)] == [23, 24]
        assert [event["payload"]["i"] for event in session.recent_events(since=123.5)] == [24]

    async def test_slow_subscriber_is_skipped(self, app_config: Any) -> None:
        """مشترکِ پر، بقیه را نکشانده و خودش نادیده گرفته می‌شود."""
        session = AgentSession("s12", app_config)
        queue = session.subscribe()
        for _ in range(501):
            session._on_event_sync(Event(kind="tool.completed", payload={}))
        assert queue.qsize() == 500
        await session._broadcast({"kind": "x"})
        session.unsubscribe(queue)
        assert session.subscribers == set()

    def test_age_and_repr(self, app_config: Any) -> None:
        """متادهای دیباگ."""
        session = AgentSession("s13", app_config)
        assert session.age_seconds < 5
        assert "s13" in repr(session) and "profile=generalist" in repr(session)


class TestApplyUpdates:
    """تغییرات پیکربندی session از راه API."""

    def test_profile_and_tools(self, app_config: Any) -> None:
        """پروفایل و ابزارها agent را بازسازی می‌کنند."""
        session = AgentSession("s14", app_config)
        assert session.agent is session.agent  # کش‌شده
        changed = session.apply_updates({"profile": "read_only", "tools": ["read_file", "os_info"]})
        assert changed == {"profile": "read_only", "tools": ["read_file", "os_info"]}
        assert [item["name"] for item in session.agent.describe_tools()] == ["os_info", "read_file"]

    def test_unknown_profile_or_field(self, app_config: Any) -> None:
        """پروفایل ناشناخته و فیلد ممنوع → KeyError."""
        session = AgentSession("s15", app_config)
        with pytest.raises(KeyError, match="unknown profile"):
            session.apply_updates({"profile": "root_mode"})
        with pytest.raises(KeyError, match="cannot change"):
            session.apply_updates({"openai_api_key": "sk-x"})
        with pytest.raises(KeyError, match="cannot change"):
            session.apply_updates({"server_token": "nope"})

    def test_none_clears_override(self, app_config: Any) -> None:
        """``None`` یعنی بازگشت به مقدار پیش‌فرض."""
        session = AgentSession("s16", app_config)
        session.apply_updates({"temperature": 0.2})
        assert session.overrides["temperature"] == 0.2
        session.apply_updates({"temperature": None})
        assert "temperature" not in session.overrides

    def test_tools_all_means_profile(self, app_config: Any) -> None:
        """«all»/خالی یعنی بدون محدودسازی."""
        session = AgentSession("s17", app_config)
        for value in (None, "", [], "all"):
            session.apply_updates({"tools": value})
            assert session.tools is None

    def test_system_note_truncated(self, app_config: Any) -> None:
        """یادداشت سیستمی سقف دارد."""
        session = AgentSession("s18", app_config)
        session.apply_updates({"system_note": "z" * 9000})
        assert len(session.system_note) <= 4000


class TestSessionRegistry:
    """مدیریت عمر session ها."""

    def test_get_creates_and_reuses(self, registry: SessionRegistry) -> None:
        """id خالی → ساخت؛ id موجود → همان نمونه."""
        first = registry.get("")
        again = registry.get(first.id)
        assert again is first
        assert registry.peek("ghost") is None
        assert registry.ids() == [first.id]
        assert len(registry) == 1 and "1 sessions" in repr(registry)

    def test_kwargs_on_existing_session(self, registry: SessionRegistry) -> None:
        """پارامترها روی session موجود هم اعمال می‌شوند."""
        session = registry.get("")
        registry.get(session.id, profile="read_only")
        assert session.profile == "read_only"

    def test_remove_and_expire(self, app_config: Any) -> None:
        """حذف دستی و انقضای TTL."""
        registry = SessionRegistry(app_config, ttl=0.01)
        session = registry.get("")
        session.last_used = 0.0
        assert registry.expire_stale() == 1
        assert registry.all() == []
        registry.get("keep")
        assert registry.remove("keep") is True
        assert registry.remove("keep") is False

    def test_busy_sessions_are_not_expired(self, app_config: Any) -> None:
        """session در حال اجرا هرگز ضایع نمی‌شود."""
        registry = SessionRegistry(app_config, ttl=0.01)
        session = registry.get("")
        session.last_used = 0.0
        session.busy = True
        assert registry.expire_stale() == 0
        session.busy = False

    def test_ttl_zero_disables_expiry(self, app_config: Any) -> None:
        """``ttl=0`` یعنی «هرگز منقضی نشو»."""
        registry = SessionRegistry(app_config, ttl=0)
        session = registry.get("")
        session.last_used = 0.0
        assert registry.expire_stale() == 0

    async def test_aclose_clears_everything(self, registry: SessionRegistry) -> None:
        """بستن سرور همه‌ی session ها را آزاد می‌کند."""
        registry.get("")
        registry.get("")
        await registry.aclose()
        assert registry.all() == []

    def test_keystore_is_shared(self, app_config: Any, tmp_path: Any) -> None:
        """رجیستری keystore را به هر session تزریق می‌کند."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("work", api_key="sk-a-12345678901234", model="m-one")
        registry = SessionRegistry(app_config, keystore=store)
        session = registry.get("")
        assert session.config.model_name == "m-one"
        assert session.keystore is store


class TestApiKeyProfileDefaults:
    """سازگاری keystore با session."""

    def test_profile_with_no_key_is_ignored(self, app_config: Any, tmp_path: Any) -> None:
        """پروفایل بدون کلید، config را خراب نمی‌کند."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("empty", model="m-two", persist=False)
        session = AgentSession("s19", app_config, keystore=store)
        assert session.config.model_name == "m-two"
        assert session.config.openai_api_key == app_config.openai_api_key
        assert isinstance(store.get("empty"), ApiKeyProfile)

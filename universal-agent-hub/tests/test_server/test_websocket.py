"""تست کانال WebSocket: جریان زنده، تأیید روی سیم و کنترل اجرا."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient

from src.config import Config
from src.server import create_app
from src.server.app import APP_SESSIONS, APP_STATS
from src.server.sessions import AgentSession
from tests.test_server.conftest import start_client

SECRET = "sk-ws-secret-value-999"


async def receive(client_socket: Any, *, timeout: float = 5.0) -> dict[str, Any]:
    """دریافت پیام JSON بعدی (با timeout تا تست آویزان نماند)."""
    message = await asyncio.wait_for(client_socket.receive(timeout=timeout), timeout=timeout + 1)
    assert message.type is not WSMsgType.CLOSE, message
    return json.loads(message.data)


async def wait_for(client_socket: Any, kind: str, *, timeout: float = 5.0, extra: Any = None) -> dict[str, Any]:
    """پیام‌ها را می‌خواند تا یکی با ``kind`` خواسته‌شده بیاید."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        assert remaining > 0, f"timed out waiting for '{kind}'"
        message = await receive(client_socket, timeout=remaining)
        if extra is not None:
            extra.append(message)
        if message.get("kind") == kind:
            return message


async def hello(client_socket: Any) -> dict[str, Any]:
    """اولین پیام هر اتصال."""
    message = await receive(client_socket)
    assert message["kind"] == "hello"
    return message["payload"]


class TestHandshake:
    """اتصال و پیام خوش‌آمد."""

    async def test_hello_payload(self, client: TestClient) -> None:
        """hello شامل نسخه، وضعیت session و آمادگی است."""
        async with client.ws_connect("/ws") as socket:
            payload = await hello(socket)
            assert payload["version"]
            assert payload["ready"] is True
            assert payload["session"]["profile"] == "generalist"
            assert payload["recent_events"] == []
            assert payload["pending_approvals"] == []

    async def test_session_query_param_is_reused(self, client: TestClient) -> None:
        """``?session=`` باعث استفاده از همان نشست می‌شود."""
        created = await (await client.post("/api/sessions", json={"profile": "ops"})).json()
        sid = created["session_id"]
        async with client.ws_connect(f"/ws?session={sid}") as socket:
            payload = await hello(socket)
            assert payload["session"]["session_id"] == sid
            assert payload["session"]["profile"] == "ops"

    async def test_unknown_session_creates_new_one(self, client: TestClient) -> None:
        """شناسه‌ی نامعلوم → نشست تازه با همان id (رفتار رجیستری)."""
        async with client.ws_connect("/ws?session=sess-phone-9") as socket:
            payload = await hello(socket)
            assert payload["session"]["session_id"] == "sess-phone-9"
        assert client.app[APP_SESSIONS].peek("sess-phone-9") is not None

    async def test_token_required_for_ws(self, app_config: Config) -> None:
        """بدون توکن، دست‌دادن WS رد می‌شود (۴۰)."""
        guarded = await start_client(app_config, server_token="ws-token-123")
        try:
            with pytest.raises(Exception) as excinfo:  # WSServerHandshakeError
                async with guarded.ws_connect("/ws"):
                    pass  # pragma: no cover
            assert "401" in str(excinfo.value)
            async with guarded.ws_connect("/ws?token=ws-token-123") as socket:
                assert (await hello(socket))["ready"] is True
        finally:
            await guarded.close()

    async def test_pending_approvals_are_replayed(self, client: TestClient) -> None:
        """اگر کاربر صفحه را رفرش کند، درخواست‌های باز دوباره می‌آیند."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        session = client.app[APP_SESSIONS].get(sid)
        # کانالی نباشد auto-deny می‌شود؛ پس شنونده را خودمان وصل می‌کنیم تا درخواست باز بماند
        session.approvals.bind_publisher(lambda _m: asyncio.sleep(0), probe=lambda: True)
        task = asyncio.ensure_future(session.approvals.request({"tool": "write_file", "summary": "keep me waiting"}))
        for _ in range(600):
            if session.approvals.pending:
                break
            await asyncio.sleep(0.005)
        async with client.ws_connect(f"/ws?session={sid}") as socket:
            payload = await hello(socket)
            assert payload["pending_approvals"][0]["summary"] == "keep me waiting"
            session.approvals.resolve(payload["pending_approvals"][0]["request_id"], False)
        assert await asyncio.wait_for(task, timeout=5) is False


class TestMessages:
    """قرارداد پیام‌ها."""

    async def test_ping_pong(self, client: TestClient) -> None:
        """``ping`` → ``pong`` (برای نگه‌داشتن اتصال موبایل)."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "ping"})
            message = await receive(socket)
            assert message["kind"] == "pong" and message["payload"]["at"] > 0

    async def test_bad_json_and_unknown_kind(self, client: TestClient) -> None:
        """ورودی بی‌فهم یا kind ناشناخته → پیام خطای JSON (اتصال می‌ماند)."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_str("not json at all")
            error = await receive(socket)
            assert error["kind"] == "error" and "JSON" in error["payload"]["error"]
            await socket.send_json({"kind": "teleport"})
            error = await receive(socket)
            assert error["payload"]["error_code"] == "unknown_kind"
            assert "teleport" in error["payload"]["error"]
            await socket.send_json({"kind": ""})
            error = await receive(socket)
            assert error["payload"]["error_code"] == "invalid_message"
            await socket.send_json({"kind": "ping"})
            assert (await receive(socket))["kind"] == "pong"

    async def test_binary_message_is_decoded(self, client: TestClient) -> None:
        """فریم باینری هم پذیرفته می‌شود (برای کلاینت‌های محدود)."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_bytes(json.dumps({"kind": "ping", "payload": {}}).encode("utf-8"))
            assert (await receive(socket))["kind"] == "pong"
            await socket.send_bytes(b"\x00\x01not json")
            assert (await receive(socket))["kind"] == "error"

    async def test_update_message_changes_settings(self, client: TestClient) -> None:
        """``update`` پروفایل/دمای نشست را عوض می‌کند."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        async with client.ws_connect(f"/ws?session={sid}") as socket:
            await hello(socket)
            await socket.send_json({"kind": "update", "payload": {"profile": "read_only", "temperature": 0.7}})
            ack = await wait_for(socket, "ack")
            assert ack["payload"]["changed"] == {"profile": "read_only", "temperature": 0.7}
            assert ack["payload"]["session"]["profile"] == "read_only"

    async def test_update_invalid_reports_error(self, client: TestClient) -> None:
        """مقدار نامعتبر در update → error با code مناسب."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "update", "payload": {"temperature": 42}})
            error = await receive(socket)
            assert error["kind"] == "error" and error["payload"]["error_code"] == "invalid_update"
            await socket.send_json({"kind": "update", "payload": {"profile": "nope"}})
            error = await receive(socket)
            assert "unknown profile" in error["payload"]["error"]

    async def test_reset_and_subscribe_acks(self, client: TestClient) -> None:
        """``reset`` و ``subscribe`` هر دو ack می‌دهند."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "subscribe", "payload": {}})
            ack = await wait_for(socket, "ack")
            assert ack["payload"]["subscribed"] is True
            await socket.send_json({"kind": "reset"})
            assert (await wait_for(socket, "ack"))["payload"]["reset"]

    async def test_cancel_when_idle(self, client: TestClient) -> None:
        """``cancel`` وقتی چیزی اجرا نمی‌شود → cancelled=false."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "cancel"})
            ack = await wait_for(socket, "ack")
            assert ack["payload"]["cancelled"] is False


class TestRunOverSocket:
    """اجرای پرامپت روی WebSocket."""

    async def test_run_streams_events_then_result(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """ترتیب پیام‌ها: accepted → event → result."""

        async def fake_invoke(self: AgentSession, text: str) -> dict[str, Any]:
            self._on_event_sync(
                __import__("src.core.event_bus", fromlist=["Event"]).Event(
                    kind="tool.completed", payload={"tool": "os_info", "duration_ms": 12}
                )
            )
            await asyncio.sleep(0)
            return {
                "text": f"answer to {text}",
                "ok": True,
                "iterations": 1,
                "tool_calls": [],
                "usage": {},
                "model": "stub",
            }

        monkeypatch.setattr(AgentSession, "_invoke", fake_invoke)
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "run", "payload": {"prompt": "whoami"}})
            accepted = await wait_for(socket, "accepted")
            assert accepted["payload"]["prompt"] == "whoami"
            event = await wait_for(socket, "event")
            assert event["event"] == "tool.completed"
            result = await wait_for(socket, "result")
            assert result["payload"]["text"] == "answer to whoami" and result["payload"]["ok"] is True

    async def test_run_with_invalid_body(self, client: TestClient) -> None:
        """prompt خالی در run → invalid_request."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "run", "payload": {"prompt": "   "}})
            error = await receive(socket)
            assert error["payload"]["error_code"] == "invalid_request"

    async def test_run_without_api_key(self, app_config: Config) -> None:
        """کلید نباشد → error با راهنما (اتصال نمی‌شکند)."""
        guarded = await start_client(app_config.model_copy(update={"openai_api_key": ""}))
        try:
            async with guarded.ws_connect("/ws") as socket:
                payload = await hello(socket)
                assert payload["ready"] is False
                await socket.send_json({"kind": "ask", "payload": {"prompt": "hi"}})
                error = await receive(socket)
                assert error["payload"]["error_code"] == "missing_api_key"
                assert "Keys" in error["payload"]["error"]
        finally:
            await guarded.close()

    async def test_agent_exception_becomes_error_message(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """استثنای ایجنت → پیام error و اتصال سالم می‌ماند."""

        async def boom(self: AgentSession, text: str) -> dict[str, Any]:
            raise RuntimeError(f"model exploded with {SECRET}")

        monkeypatch.setattr(AgentSession, "_invoke", boom)
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "run", "payload": {"prompt": "hi"}})
            await wait_for(socket, "accepted")
            # خطای ایجنت به‌شکل نتیجه‌ی ناموفق می‌آید (شکل یکسان با /api/run)، نه شکستن اتصال
            result = await wait_for(socket, "result")
            assert result["payload"]["ok"] is False
            assert "model exploded" in result["payload"]["error"]
            assert SECRET not in json.dumps(result)
            await socket.send_json({"kind": "ping"})
            assert (await wait_for(socket, "pong"))["kind"] == "pong"

    async def test_two_clients_see_the_same_events(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """دو دستگاه همزمان یک نشست را می‌بینند (تله‌پرزنت)."""

        async def fake_invoke(self: AgentSession, text: str) -> dict[str, Any]:
            self._on_event_sync(
                __import__("src.core.event_bus", fromlist=["Event"]).Event(kind="llm.responded", payload={"n": 1})
            )
            return {"text": "ok", "ok": True}

        monkeypatch.setattr(AgentSession, "_invoke", fake_invoke)
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        async with (
            client.ws_connect(f"/ws?session={sid}") as first,
            client.ws_connect(f"/ws?session={sid}") as second,
        ):
            await hello(first)
            await hello(second)
            await first.send_json({"kind": "run", "payload": {"prompt": "shared"}})
            for socket in (first, second):
                event = await wait_for(socket, "event")
                assert event["event"] == "llm.responded"
            # نتیجه فقط به درخواست‌دهنده می‌رود (مشترکین دیگر جریان رویداد را می‌بینند)
            result = await wait_for(first, "result")
            assert result["payload"]["text"] == "ok"

    async def test_run_body_session_is_ignored_in_favour_of_socket(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """اگر run روی WS بدنه‌ی session دیگری بیاورد، نشست همان socket استفاده می‌شود."""
        stub = {"text": "ok", "ok": True}
        __import__("tests.test_server.conftest", fromlist=["stub_invoke"]).stub_invoke(monkeypatch, stub)
        async with client.ws_connect("/ws?session=sess-socket") as socket:
            await hello(socket)
            await socket.send_json({"kind": "run", "payload": {"prompt": "hello", "session_id": "sess-other"}})
            result = await wait_for(socket, "result")
            assert result["payload"]["session_id"] == "sess-socket"


class TestApprovalsOverSocket:
    """تأیید عملیات حساس روی سیم."""

    async def test_allow_roundtrip(self, client: TestClient) -> None:
        """اجازه → ایجنت ادامه می‌دهد و history ثبت می‌شود."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        session = client.app[APP_SESSIONS].get(sid)
        async with client.ws_connect(f"/ws?session={sid}") as socket:
            await hello(socket)
            task = asyncio.ensure_future(
                session.approvals.request({"tool": "terminal_run", "summary": "delete tmp", "risk": "high"})
            )
            request = await wait_for(socket, "approval_request")
            assert request["tool"] == "terminal_run" and request["risk"] == "high"
            assert request["request_id"]
            await socket.send_json(
                {"kind": "approve", "payload": {"request_id": request["request_id"], "approved": True}}
            )
            assert await task is True
            ack = await wait_for(socket, "ack")
            assert ack["payload"]["ok"] is True and ack["payload"]["resolved"] == request["request_id"]

    async def test_deny_and_stale_reply(self, client: TestClient) -> None:
        """رد → نتیجه False؛ پاسخ به درخواست قدیمی ok=false."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        session = client.app[APP_SESSIONS].get(sid)
        async with client.ws_connect(f"/ws?session={sid}") as socket:
            await hello(socket)
            task = asyncio.ensure_future(session.approvals.request({"tool": "delete_file", "summary": "rm -rf"}))
            request = await wait_for(socket, "approval_request")
            await socket.send_json(
                {
                    "kind": "approve",
                    "payload": {"request_id": request["request_id"], "approved": False, "reason": "nope"},
                }
            )
            assert await task is False
            await wait_for(socket, "ack")
            await socket.send_json({"kind": "approve", "payload": {"request_id": "apv-old", "approved": True}})
            ack = await wait_for(socket, "ack")
            assert ack["payload"]["ok"] is False

    async def test_approve_invalid_payload(self, client: TestClient) -> None:
        """بدنه‌ی نامعتبر approve → خطای JSON (اتصال می‌ماند)."""
        async with client.ws_connect("/ws") as socket:
            await hello(socket)
            await socket.send_json({"kind": "approve", "payload": {"approved": True}})
            error = await receive(socket)
            assert error["payload"]["error_code"] == "invalid_approval"

    async def test_disconnect_denies_pending(self, client: TestClient) -> None:
        """قطع اتصال، تأییدهای باز را رد می‌کند (ایجنت معلق نمی‌ماند)."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        session = client.app[APP_SESSIONS].get(sid)
        socket = await client.ws_connect(f"/ws?session={sid}")
        await hello(socket)
        task = asyncio.ensure_future(session.approvals.request({"tool": "write_file", "summary": "overwrite"}))
        request = await wait_for(socket, "approval_request")
        await socket.close()
        assert await asyncio.wait_for(task, timeout=5) is False
        assert not session.approvals.pending or request["request_id"] not in [
            item["request_id"] for item in session.approvals.pending
        ]

    async def test_run_declines_when_no_viewer(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """اجرای بدون شنونده، عملیات حساس را رد می‌کند (و نمی‌خوابد)."""
        calls: list[str] = []

        async def invoke(self: AgentSession, text: str) -> dict[str, Any]:
            approved = await self.approvals.request({"tool": "terminal_run", "summary": "rm"})
            calls.append("approved" if approved else "denied")
            return {"text": "handled", "ok": True}

        monkeypatch.setattr(AgentSession, "_invoke", invoke)
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        response = await client.post("/api/run", json={"prompt": "do it", "session_id": sid})
        body = await response.json()
        assert response.status == 200 and body["text"] == "handled"
        assert calls == ["denied"]


class TestSocketBookkeeping:
    """شمارنده‌ها و پاکسازی."""

    async def test_socket_counter_and_unsubscribe(self, client: TestClient) -> None:
        """اتصال/قطع، شمارنده و مشترکین را درست به‌روز می‌کند."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        session = client.app[APP_SESSIONS].get(sid)
        assert client.app[APP_STATS].sockets == 0
        socket = await client.ws_connect(f"/ws?session={sid}")
        await hello(socket)
        assert len(session.subscribers) == 1
        assert client.app[APP_STATS].sockets == 1
        await socket.close()
        for _ in range(600):
            if not session.subscribers:
                break
            await asyncio.sleep(0.01)
        assert session.subscribers == set()
        assert client.app[APP_STATS].sockets == 0

    async def test_run_and_approval_counters(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """آمار اجرا و تصمیم‌ها در ``/api/status`` دیده می‌شود."""

        __import__("tests.test_server.conftest", fromlist=["stub_invoke"]).stub_invoke(
            monkeypatch, {"text": "ok", "ok": True}
        )
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        before = client.app[APP_STATS].as_dict()
        await client.post("/api/run", json={"prompt": "count me", "session_id": sid})
        assert client.app[APP_STATS].runs == before["runs"] + 1
        async with client.ws_connect(f"/ws?session={sid}") as socket:
            await hello(socket)
            await socket.send_json({"kind": "approve", "payload": {"request_id": "ghost", "approved": True}})
            await wait_for(socket, "ack")
        assert client.app[APP_STATS].approved == before["approved"]

    async def test_websocket_handler_rejects_non_get(self, app_config: Config) -> None:
        """``POST /ws`` ارتقا نمی‌دهد (بدون UI → 405 از روتر)."""
        guarded = await start_client(app_config.model_copy(update={"server_static_dir": "off"}))
        try:
            response = await guarded.post("/ws", json={})
            assert response.status in {404, 405}
            assert "text/html" not in response.headers.get("Content-Type", "")
        finally:
            await guarded.close()

    async def test_close_frame_is_handled(self, client: TestClient) -> None:
        """بستن توسط کلاینت استثنا تولید نمی‌کند."""
        socket = await client.ws_connect("/ws")
        await hello(socket)
        await socket.close()
        assert socket.closed is True
        await asyncio.sleep(0.05)
        assert isinstance(client.app[APP_SESSIONS], object)


def test_routes_registered_without_ui(app_config: Config) -> None:
    """مسیر WS مستقل از وجود UI است."""
    app = create_app(app_config.model_copy(update={"server_static_dir": "off"}))
    assert web  # import smoke
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/ws" in paths and "/" in paths

"""تست مسیرهای REST سرور (بدون شبکه؛ ایجنت با stub جایگزین می‌شود)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from src.config import Config
from src.core.tool_registry import ToolRegistry, discover_tools
from src.server import create_app
from src.server.app import APP_CONFIG, APP_SESSIONS, APP_STATS, _is_rate_limited  # noqa: PLC2701 - تست helper داخلی
from src.server.sessions import AgentSession
from tests.test_server.conftest import start_client, stub_invoke

SECRET = "sk-super-secret-value-123"


async def jget(client: TestClient, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    """درخواست GET و بازگرداندن ``(status, json)``."""
    response = await client.get(path, **kwargs)
    text = await response.text()
    return response.status, (json.loads(text) if text else {})


async def poll_until(predicate: Any, *, attempts: int = 600) -> bool:
    """صبر تا برآورده شدن یک شرط (با polling کوتاه)."""
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return bool(predicate())


class TestAppFactory:
    """قوانین سخت ساخت اپ."""

    def test_open_host_without_token_refused(self, app_config: Config) -> None:
        """bind روی شبکه بدون توکن = خطا (fail closed)."""
        with pytest.raises(RuntimeError, match="without SERVER_TOKEN"):
            create_app(app_config.model_copy(update={"server_host": "0.0.0.0", "server_token": ""}))

    def test_open_host_with_token_ok(self, app_config: Config) -> None:
        """با توکن، باز شدن روی شبکه مجاز است."""
        app = create_app(app_config.model_copy(update={"server_host": "0.0.0.0", "server_token": "a" * 24}))
        assert app[APP_CONFIG].server_is_open is True

    def test_ui_routes_registered_when_web_root_exists(self, app_config: Config) -> None:
        """پوشه‌ی ``web/`` پروژه → مسیرهای UI هم ثبت می‌شوند."""
        paths = {route.resource.canonical for route in create_app(app_config).router.routes()}
        assert "/" in paths and "/api/status" in paths and "/ws" in paths

    def test_no_ui_routes_when_static_dir_missing(self, app_config: Config) -> None:
        """UI نباشد → فقط API (و مسیر ریشه پیام JSON می‌دهد)."""
        settings = app_config.model_copy(update={"server_static_dir": "off"})
        app = create_app(settings)
        assert app[APP_CONFIG].web_root is None
        paths = {route.resource.canonical for route in app.router.routes()}
        assert "/styles.css" not in paths and "/api/status" in paths


class TestPublicAndAuth:
    """health عمومی و احراز توکن."""

    async def test_healthz_is_public_and_small(self, client: TestClient) -> None:
        """``/healthz`` بدون احراز است و راز نمی‌دهد."""
        discover_tools()
        status, body = await jget(client, "/healthz")
        assert status == 200
        assert body["status"] == "ok" and body["auth_required"] is False
        assert body["tools"] == len(ToolRegistry.names())
        assert "generalist" in body["profiles"]
        assert body["uptime_seconds"] >= 0
        assert "api_key" not in json.dumps(body)

    async def test_token_required_when_configured(self, app_config: Config) -> None:
        """با توکن: بدون هدر ۴۰۱، با هدر/کوئری ۲۰، health همچنان باز."""
        guarded = await start_client(app_config, server_token="letmein123")
        try:
            response = await guarded.get("/api/status")
            assert response.status == 401
            body = await response.json()
            assert body["error_code"] == "unauthorized"
            assert "letmein123" not in json.dumps(body)
            assert (await guarded.get("/healthz")).status == 200
            assert (await guarded.get("/api/status", headers={"Authorization": "Bearer letmein123"})).status == 200
            assert (await guarded.get("/api/status?token=letmein123")).status == 200
            assert (await guarded.get("/api/status", headers={"X-Agent-Token": "letmeout"})).status == 401
            assert (
                await guarded.post("/api/run/stop", json={}, headers={"X-Agent-Token": "letmein123"})
            ).status == 200
        finally:
            await guarded.close()

    async def test_foreign_host_header_rejected(self, client: TestClient) -> None:
        """هدر Host غیرمنتظره (DNS rebinding) → ۴۰۳."""
        response = await client.get("/api/status", headers={"Host": "attacker.example"})
        assert response.status == 403
        assert (await response.json())["error_code"] == "bad_host"

    async def test_unknown_api_path_is_json_404(self, client: TestClient) -> None:
        """مسیر API ناآشنا JSON برمی‌گرداند، نه HTML."""
        status, body = await jget(client, "/api/nope")
        assert status == 404 and body["error_code"] == "no_route"

    def test_rate_limit_predicate(self) -> None:
        """انتخاب مسیرهای نرخ‌بندی‌شده."""
        assert _is_rate_limited("POST", "/api/run") is True
        assert _is_rate_limited("POST", "/api/tools/terminal_run/invoke") is True
        assert _is_rate_limited("POST", "/api/keys") is True
        assert _is_rate_limited("POST", "/api/run/stop") is False
        assert _is_rate_limited("GET", "/api/run") is False


class TestStatusAndConfig:
    """``/api/status`` و ``/api/config``."""

    async def test_status_shape(self, client: TestClient) -> None:
        """وضعیت کامل: نسخه، آمادگی، keystore، session ها، قابلیت‌ها."""
        status, body = await jget(client, "/api/status")
        assert status == 200
        assert body["ready"] is True, body["ready_hint"]
        assert body["version"]
        assert body["keystore"]["count"] == 0
        assert body["session"] is None
        assert body["capabilities"]["approvals"] is True
        assert body["ui_available"] is True
        assert body["tool_count"] == len(ToolRegistry.names())
        assert body["stats"] == {"sockets": 0, "runs": 0, "approved": 0, "denied": 0}
        assert client.app[APP_STATS].sockets == 0
        assert body["auth"] == {"token_required": False, "loopback_without_token": True, "token_length": 0}

    async def test_status_with_session_safety(self, session_client: TestClient) -> None:
        """با session، خلاصه‌ی سیاست ایمنی هم می‌آید."""
        status, body = await jget(session_client, "/api/status")
        assert status == 200
        assert body["safety"]["confirmation_enabled"] is True
        assert len(body["sessions"]) == 1
        assert body["session"]["session_id"] == session_client.session.headers["X-Agent-Session"]

    async def test_config_masks_keys(self, client: TestClient) -> None:
        """پیکربندی ماسک‌شده است و راهنمای فیلدها را دارد."""
        status, body = await jget(client, "/api/config")
        assert status == 200
        assert SECRET not in json.dumps(body)
        assert "model_name" in body["config"]
        assert "temperature" in body["editable"]
        assert "gpt-6-astra" in body["notes"]["model_name"]

    async def test_ready_is_false_without_any_key(self, app_config: Config) -> None:
        """بدون کلید (env و keystore) → ready=false با راهنما."""
        guarded = await start_client(app_config.model_copy(update={"openai_api_key": ""}))
        try:
            status, body = await jget(guarded, "/api/status")
            assert status == 200 and body["ready"] is False
            assert "OPENAI_API_KEY" in body["ready_hint"]
        finally:
            await guarded.close()

    async def test_ready_becomes_true_after_key_activation(self, client: TestClient) -> None:
        """فعال‌کردن پروفایل کلید، آمادگی را عوض می‌کند (بدون ریستارت)."""
        bare = await start_client(client.app[APP_CONFIG].model_copy(update={"openai_api_key": ""}))
        try:
            assert (await jget(bare, "/api/status"))[1]["ready"] is False
            await bare.post(
                "/api/keys", json={"name": "phone", "api_key": SECRET, "model": "gpt-6-astra", "activate": True}
            )
            _, body = await jget(bare, "/api/status")
            assert body["ready"] is True and body["keystore"]["active"] == "phone"
        finally:
            await bare.close()


class TestTools:
    """``/api/tools`` و فراخوانی مستقیم."""

    async def test_lists_every_registered_tool(self, client: TestClient) -> None:
        """فهرست کامل + فیلدهای لازم."""
        discover_tools()
        status, body = await jget(client, "/api/tools")
        assert status == 200
        assert body["count"] == len(ToolRegistry.names())
        names = {tool["name"] for tool in body["tools"]}
        assert {"terminal_run", "read_file", "browser_screenshot", "web_search", "memory_info"} <= names
        assert {"description", "category", "risk_level", "requires_confirmation"} <= set(body["tools"][0])

    async def test_filters(self, client: TestClient) -> None:
        """فیلتر دسته و جست‌وجو."""
        _, all_body = await jget(client, "/api/tools")
        _, filesystem = await jget(client, "/api/tools?category=filesystem")
        assert (
            filesystem["count"] == len([tool for tool in all_body["tools"] if tool["category"] == "filesystem"]) >= 4
        )
        _, search = await jget(client, "/api/tools?q=screenshot")
        assert {tool["name"] for tool in search["tools"]} == {"browser_screenshot", "browser_browse"}
        assert (await jget(client, "/api/tools?q=zzzz"))[1]["count"] == 0

    async def test_schema_and_missing(self, client: TestClient) -> None:
        """اسکیمای یک ابزار و ۴۰ برای ناشناخته."""
        status, body = await jget(client, "/api/tools/read_file")
        assert status == 200
        function = body["schema"]["function"]
        assert function["name"] == "read_file"
        assert function["parameters"]["additionalProperties"] is False
        assert all("description" in prop for prop in function["parameters"]["properties"].values())
        assert body["info"]["name"] == "read_file"
        status, missing = await jget(client, "/api/tools/no_such_tool")
        assert status == 404 and missing["error_code"] == "unknown_tool"

    async def test_direct_invoke_blocked_by_safety(self, session_client: TestClient) -> None:
        """فراخوانی مستقیم، همان نگهبان ایمنی را رد می‌کند."""
        response = await session_client.post(
            "/api/tools/read_file/invoke", json={"arguments": {"path": "/etc/passwd"}}
        )
        body = await response.json()
        assert response.status == 200 and body["success"] is False and body["error_code"] == "blocked"

    async def test_direct_invoke_declined_without_client(self, session_client: TestClient) -> None:
        """بدون کلاینت متصل، عملیات حساس رد می‌شود (نه انتظار بی‌پایان)."""
        response = await session_client.post(
            "/api/tools/terminal_run/invoke", json={"arguments": {"command": "echo hi | wc -l", "shell": True}}
        )
        body = await response.json()
        assert response.status == 200 and body["success"] is False
        assert body["error_code"] in {"declined", "confirmation_unavailable"}

    async def test_direct_invoke_reads_inside_workspace(self, session_client: TestClient) -> None:
        """خواندن فایل داخل workspace مجاز است."""
        response = await session_client.post(
            "/api/tools/read_file/invoke", json={"arguments": {"path": "sample.txt"}}
        )
        body = await response.json()
        assert body["success"] is True
        assert "alpha" in json.dumps(body["data"])

    async def test_direct_invoke_disabled(self, app_config: Config) -> None:
        """با ``SERVER_ALLOW_DIRECT_TOOLS=false`` مسیر خاموش است."""
        guarded = await start_client(app_config, server_allow_direct_tools=False)
        try:
            response = await guarded.post("/api/tools/read_file/invoke", json={"arguments": {"path": "x"}})
            assert response.status == 403 and (await response.json())["error_code"] == "disabled"
        finally:
            await guarded.close()

    async def test_invoke_bad_json(self, client: TestClient) -> None:
        """بدنه‌ی نامعتبر → ۴۰۰ با error_code."""
        for data in ("[]", "{oops", '"text"', "5"):
            response = await client.post(
                "/api/tools/read_file/invoke", data=data, headers={"Content-Type": "application/json"}
            )
            assert response.status == 400, data
        response = await client.post("/api/tools/read_file/invoke", json={"arguments": "not-a-dict"})
        assert response.status == 400


class TestProfiles:
    """``/api/profiles``."""

    async def test_profiles_listed(self, client: TestClient) -> None:
        """پروفایل‌های آماده با ابزارها و توضیح."""
        status, body = await jget(client, "/api/profiles")
        assert status == 200
        names = [item["name"] for item in body["profiles"]]
        assert names == sorted(names) and "generalist" in names
        read_only = next(item for item in body["profiles"] if item["name"] == "read_only")
        assert "terminal_run" not in (read_only["tools"] or [])
        # شمارش را از خود رجیستری می‌گیریم تا به تعداد ابزارهای ثبت‌شده گره نخورد
        all_names = {tool["name"] for tool in (await jget(client, "/api/tools"))[1]["tools"]}
        assert set(read_only["tools"]) == all_names - set(read_only["disabled_tools"])
        assert len(read_only["disabled_tools"]) == 8
        generalist = next(item for item in body["profiles"] if item["name"] == "generalist")
        assert generalist["enabled_tools"] is None and "terminal_run" in generalist["tools"]


class TestSessions:
    """CRUD نشست."""

    async def test_create_get_patch_delete(self, client: TestClient) -> None:
        """چرخه‌ی کامل یک session."""
        response = await client.post("/api/sessions", json={"profile": "read_only", "temperature": 0.3})
        assert response.status == 201
        created = await response.json()
        sid = created["session_id"]
        assert created["profile"] == "read_only" and created["overrides"] == {"temperature": 0.3}

        status, body = await jget(client, f"/api/sessions/{sid}")
        assert status == 200 and body["tools"] is None and body["profile"] == "read_only"

        response = await client.patch(
            f"/api/sessions/{sid}", json={"tools": ["read_file"], "system_note": "be terse"}
        )
        patched = await response.json()
        assert response.status == 200 and patched["changed"]["tools"] == ["read_file"]
        assert patched["session"]["tools"] == ["read_file"]
        assert patched["session"]["active_tools"] == ["read_file"]

        assert (await client.delete(f"/api/sessions/{sid}")).status == 200
        status, body = await jget(client, f"/api/sessions/{sid}")
        assert status == 404 and body["error_code"] == "no_session"

    async def test_patch_unknown_profile(self, client: TestClient) -> None:
        """پروفایل نامعتبر → ۴۲۲ با فهرست مجاز."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        response = await client.patch(f"/api/sessions/{sid}", json={"profile": "superuser"})
        assert response.status == 422
        assert "unknown profile" in (await response.json())["error"]

    async def test_patch_validation_error(self, client: TestClient) -> None:
        """مقادیر خارج از دامنه → ۴۰۰ (اعتبارسنجی pydantic)."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        for body in ({"temperature": 9.0}, {"max_tool_iterations": 999}, {"model_name": 12}):
            response = await client.patch(f"/api/sessions/{sid}", json=body)
            assert response.status == 400, body

    async def test_sessions_listing_and_reset(self, client: TestClient) -> None:
        """فهرست + reset."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        status, body = await jget(client, "/api/sessions")
        assert status == 200 and body["count"] >= 1
        assert any(item["session_id"] == sid for item in body["sessions"])
        response = await client.post(f"/api/sessions/{sid}/reset", json={})
        assert response.status == 200 and (await response.json())["reset"] == sid

    async def test_history_and_events_for_new_session(self, client: TestClient) -> None:
        """تاریخچه و رویداد نشست تازه خالی است."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        status, history = await jget(client, f"/api/sessions/{sid}/history")
        assert status == 200 and history["messages"] == []
        status, events = await jget(client, f"/api/sessions/{sid}/events?limit=5&since=0")
        assert status == 200 and events["events"] == []
        status, body = await jget(client, f"/api/sessions/{sid}/history?limit=99999")
        assert status == 200 and body["messages"] == []

    async def test_stop_when_idle(self, client: TestClient) -> None:
        """توقف وقتی چیزی اجرا نمی‌شود → cancelled=false."""
        sid = (await (await client.post("/api/sessions", json={})).json())["session_id"]
        response = await client.post("/api/run/stop", json={"session_id": sid})
        assert response.status == 200 and (await response.json())["cancelled"] is False


class TestRun:
    """``POST /api/run``."""

    async def test_run_returns_agent_result(
        self, session_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """نتیجه‌ی ایجنت با session_id و duration به اپ می‌رسد."""
        stub = {
            "text": "done",
            "ok": True,
            "iterations": 2,
            "tool_calls": [{"tool": "os_info", "succeeded": True, "arguments": {}}],
            "usage": {"total_tokens": 42},
            "model": "gpt-6-astra",
        }
        state = stub_invoke(monkeypatch, stub)
        response = await session_client.post("/api/run", json={"prompt": "what os?"})
        assert response.status == 200
        body = await response.json()
        assert body["text"] == "done" and body["iterations"] == 2
        assert body["session_id"] == str(session_client.session.headers["X-Agent-Session"])
        assert body["usage"]["total_tokens"] == 42
        assert state["captured"] == ["what os?"]

    async def test_run_creates_session_from_body(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """session با id دلخواه ساخته و در فهرست می‌آید."""
        stub_invoke(monkeypatch, {"text": "ok", "ok": True})
        response = await client.post(
            "/api/run", json={"prompt": "hi", "session_id": "sess-phone-1", "profile": "ops"}
        )
        assert response.status == 200
        status, body = await jget(client, "/api/sessions/sess-phone-1")
        assert status == 200 and body["profile"] == "ops"

    async def test_run_new_session_flag(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """``new_session=true`` همیشه نشست تازه می‌سازد."""
        stub_invoke(monkeypatch, {"text": "x", "ok": True})
        await client.post("/api/run", json={"prompt": "one", "session_id": "shared"})
        body = await (
            await client.post("/api/run", json={"prompt": "two", "session_id": "shared", "new_session": True})
        ).json()
        assert body["session_id"] != "shared"

    async def test_run_reports_agent_failure_as_422(
        self, session_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """شکست مدل = ۴۲۲ با جزئیات (نه ۵۰۰)."""
        stub_invoke(monkeypatch, {"text": "", "ok": False, "error": "all models failed: 401 unauthorized"})
        response = await session_client.post("/api/run", json={"prompt": "hi"})
        assert response.status == 422
        body = await response.json()
        assert body["ok"] is False and "all models failed" in body["error"]

    async def test_run_validates_prompt(self, client: TestClient) -> None:
        """پرامپت خالی/غایب/خارج از دامنه → ۴۰۰."""
        for body in ({"prompt": "   "}, {}, {"prompt": "ok", "timeout": 0}, {"prompt": "ok", "timeout": 10**6}):
            response = await client.post("/api/run", json=body)
            assert response.status == 400, body

    async def test_run_without_api_key_is_503(self, app_config: Config) -> None:
        """کلید تنظیم‌نشده → ۵۰ با راهنمای دقیق."""
        guarded = await start_client(app_config.model_copy(update={"openai_api_key": ""}))
        try:
            response = await guarded.post("/api/run", json={"prompt": "hi"})
            assert response.status == 503
            body = await response.json()
            assert body["error_code"] == "missing_api_key" and "Keys panel" in body["error"]
        finally:
            await guarded.close()

    async def test_run_rate_limited(self, app_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """سقف نرخ، درخواست اضافه را ۴۲۹ می‌کند (با retry_after)."""
        stub_invoke(monkeypatch, {"text": "ok", "ok": True})
        guarded = await start_client(app_config, server_rate_limit_per_minute=2)
        try:
            codes = [(await guarded.post("/api/run", json={"prompt": f"p{i}"})).status for i in range(4)]
            assert codes[:2] == [200, 200] and set(codes[2:]) == {429}
            body = await (await guarded.post("/api/run", json={"prompt": "again"})).json()
            assert body["error_code"] == "rate_limited" and 0 < body["details"]["retry_after"] <= 60
            # GET ها محدود نیستند
            assert (await guarded.get("/api/tools")).status == 200
        finally:
            await guarded.close()

    async def test_run_propagates_session_events(
        self, session_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """رویدادهای اجرا در ``/events`` می‌نشینند (برای UI بدون WS)."""

        async def fake_invoke(self: AgentSession, text: str) -> dict[str, Any]:
            self._on_event_sync(
                __import__("src.core.event_bus", fromlist=["Event"]).Event(
                    kind="tool.completed", payload={"tool": "os_info"}
                )
            )
            return {"text": "t", "ok": True}

        monkeypatch.setattr(AgentSession, "_invoke", fake_invoke)
        await session_client.post("/api/run", json={"prompt": "hi"})
        sid = str(session_client.session.headers["X-Agent-Session"])
        _, events = await jget(session_client, f"/api/sessions/{sid}/events")
        assert [event["event"] for event in events["events"]] == ["tool.completed"]


class TestApprovals:
    """مسیرهای تأیید (REST)."""

    async def test_empty_and_stale(self, session_client: TestClient, sid: str) -> None:
        """فهرست خالی و پاسخ به درخواست نامعلوم 409 می‌شود."""
        status, body = await jget(session_client, f"/api/sessions/{sid}/approvals")
        assert status == 200 and body["pending"] == []
        response = await session_client.post(
            f"/api/sessions/{sid}/approvals", json={"request_id": "apv-x", "approved": True}
        )
        assert response.status == 409 and (await response.json())["error_code"] == "stale_approval"

    async def test_resolve_through_rest(self, session_client: TestClient, sid: str) -> None:
        """کاربر می‌تواند بدون WebSocket هم تأیید کند."""
        session = session_client.app[APP_SESSIONS].get(sid)
        session.approvals.bind_publisher(lambda _message: asyncio.sleep(0), probe=lambda: True)
        task = asyncio.ensure_future(session.approvals.request({"tool": "delete_file", "summary": "remove x"}))
        assert await poll_until(lambda: bool(session.approvals.pending)) is True
        request_id = session.approvals.pending[0]["request_id"]
        response = await session_client.post(
            f"/api/sessions/{sid}/approvals", json={"request_id": request_id, "approved": True, "reason": "ok"}
        )
        assert response.status == 200
        assert await task is True
        assert session.approvals.history[-1]["reason"] == "ok"

    async def test_polling_marks_the_channel(self, session_client: TestClient, sid: str) -> None:
        """polling پنجره‌ی پاسخ را باز می‌کند (وگرنه auto-deny می‌شود)."""
        session = session_client.app[APP_SESSIONS].peek(sid)
        assert session.approvals.has_channel is False
        await jget(session_client, f"/api/sessions/{sid}/approvals")
        assert session.approvals.has_channel is True
        session.approvals.poll_grace = 0.0
        await asyncio.sleep(0.02)
        assert session.approvals.has_channel is False

    async def test_approval_validation(self, session_client: TestClient, sid: str) -> None:
        """``request_id`` خالی مجاز نیست."""
        response = await session_client.post(
            f"/api/sessions/{sid}/approvals", json={"request_id": "", "approved": True}
        )
        assert response.status == 400

    async def test_approvals_for_unknown_session(self, client: TestClient) -> None:
        """session موجود نباشد → 404 (چیزی ساخته نمی‌شود)."""
        response = await client.get("/api/sessions/ghost-session/approvals")
        assert response.status == 404 and (await response.json())["error_code"] == "no_session"


class TestKeyManagement:
    """پنل مدیریت کلید API."""

    async def test_full_lifecycle(self, client: TestClient, app_config: Config) -> None:
        """ساخت، فهرست، فعال‌سازی، حذف و ماندگاری روی دیسک."""
        response = await client.post(
            "/api/keys",
            json={
                "name": "work",
                "api_key": SECRET,
                "base_url": "http://gw:8000/v1",
                "model": "gpt-6-astra",
                "fallbacks": ["m2"],
                "activate": True,
            },
        )
        assert response.status == 201
        body = await response.json()
        assert body["active"] == "work" and SECRET not in json.dumps(body)
        assert body["saved"]["masked_key"].startswith("sk-sup") and "…" in body["saved"]["masked_key"]

        status, listed = await jget(client, "/api/keys")
        assert status == 200 and [item["name"] for item in listed["profiles"]] == ["work"]
        assert SECRET not in json.dumps(listed) and listed["status"]["exists"] is True

        response = await client.post("/api/keys", json={"name": "home", "api_key": "sk-home-000111222333"})
        assert (await response.json())["active"] == "work"
        activated = await (await client.post("/api/keys/home/activate", json={})).json()
        assert activated["active"] == "home" and activated["applied_to_sessions"] >= 0

        assert (await client.delete("/api/keys/home")).status == 200
        assert (await client.delete("/api/keys/home")).status == 404
        assert (await client.post("/api/keys/ghost/activate", json={})).status == 404

        saved = json.loads(Path(app_config.keystore_path).read_text(encoding="utf-8"))
        assert saved["profiles"]["work"]["api_key"] == SECRET  # روی دیسک (۰۶۰۰) می‌ماند

    async def test_update_keeps_old_key(self, client: TestClient) -> None:
        """به‌روزرسانی بدون کلید، کلید قبلی را نگه می‌دارد."""
        await client.post("/api/keys", json={"name": "work", "api_key": SECRET})
        body = await (await client.post("/api/keys", json={"name": "work", "model": "other"})).json()
        assert body["saved"]["model"] == "other" and body["saved"]["has_key"] is True

    async def test_rejects_bad_name_and_long_key(self, client: TestClient) -> None:
        """نام نامعتبر و کلید غیرعقلانی رد می‌شوند."""
        for name in ("a/b", "..\\x", "with\nnewline", ""):
            response = await client.post("/api/keys", json={"name": name, "api_key": SECRET})
            assert response.status == 400, name
        assert (await client.post("/api/keys", json={"name": "work", "api_key": "sk-" + "z" * 900})).status == 422

    async def test_import_env_without_key(self, app_config: Config) -> None:
        """import-env وقتی کلیدی در محیط نیست → ۴۰۹."""
        guarded = await start_client(app_config.model_copy(update={"openai_api_key": ""}))
        try:
            response = await guarded.post("/api/keys/import-env", json={})
            assert response.status == 409 and (await response.json())["error_code"] == "nothing_to_import"
        finally:
            await guarded.close()

    async def test_import_env_copies_and_activates(self, app_config: Config) -> None:
        """کلید base config به مخزن منتقل و فعال می‌شود."""
        guarded = await start_client(
            app_config.model_copy(update={"openai_api_key": SECRET, "openai_base_url": "http://gw/v1"})
        )
        try:
            response = await guarded.post("/api/keys/import-env?name=env-key", json={})
            assert response.status == 201
            body = await response.json()
            assert body["active"] == "env-key" and SECRET not in json.dumps(body)
            status, status_body = await jget(guarded, "/api/status")
            assert status == 200 and status_body["keystore"]["names"] == ["env-key"]
        finally:
            await guarded.close()


class TestSafetyEndpoint:
    """``/api/safety``."""

    async def test_safety_summary_for_session(self, session_client: TestClient) -> None:
        """سیاست ایمنی همان خلاصه‌ی ایجنت است."""
        status, body = await jget(session_client, "/api/safety")
        assert status == 200
        assert body["allow_shell"] is True and body["confirmation_enabled"] is True
        assert len(body["allowed_directories"]) == 1
        assert body["protected_system_dirs"]

    async def test_safety_creates_session_from_header(self, client: TestClient) -> None:
        """با هدر ناشناخته، نشست تازه ساخته می‌شود."""
        response = await client.get("/api/safety", headers={"X-Agent-Session": "brand-new"})
        assert response.status == 200
        status, body = await jget(client, "/api/sessions/brand-new")
        assert status == 200 and body["session_id"] == "brand-new"


class TestUiServing:
    """استاتیک UI."""

    async def test_root_serves_index(self, client: TestClient, app_config: Config) -> None:
        """``GET /`` فایل index.html را می‌دهد (UI واقعی پروژه)."""
        assert app_config.web_root is not None
        response = await client.get("/")
        text = await response.text()
        assert response.status == 200
        assert "Universal Agent Hub" in text and 'src="app.js"' in text
        assert response.headers["Cache-Control"] == "no-cache"

    async def test_assets_and_manifest(self, client: TestClient) -> None:
        """فایل‌های جانبی UI با content-type درست."""
        for name, expected in (
            ("styles.css", "text/css"),
            ("app.js", "text/javascript"),
            ("manifest.webmanifest", "application/manifest+json"),
            ("sw.js", "text/javascript"),
            ("icon.svg", "image/svg+xml"),
        ):
            response = await client.get(f"/{name}")
            assert response.status == 200, name
            assert expected in response.headers["Content-Type"], (name, response.headers.get("Content-Type"))
        assert (await client.get("/icons/icon-192.png")).status == 200

    async def test_unknown_path_falls_back_to_index(self, client: TestClient) -> None:
        """مسیر نامشخص → index (مسیریابی SPA)."""
        response = await client.get("/anything/else")
        assert response.status == 200
        assert "Universal Agent Hub" in await response.text()

    async def test_root_without_ui_reports_api_only(self, app_config: Config) -> None:
        """UI نصب نباشد → پیام متنیِ API-only (بدون ۴۰۴ گیج‌کننده)."""
        guarded = await start_client(app_config.model_copy(update={"server_static_dir": "off"}))
        try:
            response = await guarded.get("/")
            body = await response.json()
            assert response.status == 200 and body["ui"].startswith("not installed")
            assert (await guarded.get("/styles.css")).status == 404
            assert (await guarded.get("/api/tools")).status == 200
        finally:
            await guarded.close()

    async def test_static_dir_pointing_at_partial_folder_falls_back(self, app_config: Config) -> None:
        """پوشه‌ی بدون index.html → بازگشت به UI بسته‌بندی‌شده (نه ۵۰۰)."""
        broken = app_config.project_root / "partial-web"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "styles.css").write_text("/* x */", encoding="utf-8")
        guarded = await start_client(app_config.model_copy(update={"server_static_dir": str(broken)}))
        try:
            response = await guarded.get("/")
            assert response.status == 200
            assert "Universal Agent Hub" in await response.text()
        finally:
            await guarded.close()

    async def test_ui_off_values(self, app_config: Config) -> None:
        """هر مقدار «خاموش» در SERVER_STATIC_DIR، UI را می‌بندد."""
        for value in ("off", "NONE", "false", "disabled"):
            assert app_config.model_copy(update={"server_static_dir": value}).web_root is None
        assert app_config.model_copy(update={"server_static_dir": "src/server/web"}).web_root is not None


class TestHelpers:
    """helper های کوچک ماژول app."""

    async def test_is_request_local(self, client: TestClient) -> None:
        """تشخیص درخواست محلی (برای پیام‌های راهنما)."""
        from src.server.app import is_request_local

        class Req:
            remote = "127.0.0.1"

        assert is_request_local(Req()) is True  # type: ignore[arg-type]

    def test_static_handler_for_missing_file(self, tmp_path: Path) -> None:
        """هندلر استاتیک برای فایل رفته ۴۰۴ JSON می‌دهد."""
        from aiohttp import web

        from src.server.app import _static_handler  # noqa: PLC2701

        handler = _static_handler(tmp_path / "gone.css")
        request = web.Request.__new__(web.Request)  # فقط برای type-check؛ هندلر آن را نمی‌خواند
        response = asyncio.run(handler(request))
        assert response.status == 404
        assert b"not_found" in response.body


class TestMemoryAndReports:
    """مسیرهای ``/api/memory`` و ``/api/reports`` (حافظه و پنل گزارش)."""

    async def test_report_shape(self, client: TestClient) -> None:
        """گزارش با ساختار کامل + text آماده‌ی نمایش برگردانده می‌شود."""
        status, body = await jget(client, "/api/reports")
        assert status == 200
        report = body["report"]
        assert set(report) >= {
            "generated_at",
            "runs",
            "tools",
            "safety",
            "plans",
            "next_actions",
            "warnings",
            "files",
        }
        assert report["runs"]["total"] == 0
        assert "Activity report" in body["text"]
        assert client.app[APP_CONFIG].memory_path is not None

    async def test_report_accepts_days_window(self, client: TestClient) -> None:
        """``days=0`` یعنی کل تاریخچه؛ مقدار نامعتبر ۴۰۰ می‌گیرد."""
        status, body = await jget(client, "/api/reports?days=0")
        assert status == 200 and body["report"]["window_days"] == 0
        status, body = await jget(client, "/api/reports?days=lots")
        assert status == 400 and body["error_code"] == "invalid_input"
        status, body = await jget(client, "/api/reports?days=99999")
        assert status == 400 and "between 0 and 3650" in body["error"]

    async def test_memory_list_and_stats(self, client: TestClient) -> None:
        """فهرست خالی هم stats کامل می‌دهد تا پنل بداند حافظه کجاست."""
        status, body = await jget(client, "/api/memory")
        assert status == 200
        assert body["enabled"] is True and body["records"] == []
        assert body["stats"]["path"].endswith(".jsonl")
        assert "preference" in body["kinds"]

    async def test_memory_write_then_search_then_forget(self, client: TestClient) -> None:
        """چرخه‌ی کامل از راه HTTP: نوشتن، جست‌وجو، حذف."""
        response = await client.post(
            "/api/memory",
            json={
                "content": "staging deploys via scripts/deploy.sh",
                "kind": "procedure",
                "tags": ["project:webshop"],
            },
        )
        assert response.status == 201
        saved = (await response.json())["saved"]
        assert saved["kind"] == "procedure" and saved["source"] == "api:memory"
        assert saved["tags"] == ["project:webshop"]

        status, body = await jget(client, "/api/memory?q=deploy+staging")
        assert status == 200 and body["query"] == "deploy staging"
        assert [item["id"] for item in body["records"]] == [saved["id"]]

        response = await client.delete(f"/api/memory/{saved['id']}")
        assert response.status == 200
        assert (await response.json())["stats"]["records"] == 0

    async def test_memory_write_redacts_secrets(self, client: TestClient) -> None:
        """متن یادداشت پیش از ذخیره ماسک می‌شود؛ پاسخ هم کلید کامل ندارد."""
        response = await client.post("/api/memory", json={"content": f"payments key is {SECRET} for env prod"})
        assert response.status == 201
        body = await response.json()
        assert SECRET not in json.dumps(body)
        assert SECRET not in body["saved"]["content"]

    async def test_memory_write_validates_body(self, client: TestClient) -> None:
        """بدنه‌ی خالی/ناشناخته: ۴۰۰ برای خالی، fallback kind برای typo."""
        response = await client.post("/api/memory", json={"content": "   "})
        assert response.status == 400
        assert (await response.json())["error_code"] == "invalid_request"
        response = await client.post("/api/memory", json={"content": "a note", "kind": "gossip"})
        assert response.status == 201
        assert (await response.json())["saved"]["kind"] == "note"
        response = await client.post("/api/memory", json={"content": "x" * 5000})
        assert response.status == 400

    async def test_pinned_note_requires_force(self, client: TestClient) -> None:
        """رکورد pinned با DELETE ساده حذف نمی‌شود."""
        response = await client.post(
            "/api/memory", json={"content": "core preference: answer in Persian", "kind": "preference", "pin": True}
        )
        record_id = (await response.json())["saved"]["id"]
        response = await client.delete(f"/api/memory/{record_id}")
        assert response.status == 409
        assert (await response.json())["error_code"] == "pinned"
        response = await client.delete(f"/api/memory/{record_id}?force=1")
        assert response.status == 200
        assert (await response.json())["deleted"] == record_id

    async def test_forget_unknown_id_is_404(self, client: TestClient) -> None:
        """id ناموجود ۴۰۴ می‌گیرد (نه ۵۰۰)."""
        response = await client.delete("/api/memory/zzzzzzzz")
        assert response.status == 404
        assert (await response.json())["error_code"] == "not_found"

    async def test_memory_disabled_is_409(self, app_config: Config) -> None:
        """با MEMORY_ENABLED=false مسیرها ۴۰۹ روشن می‌دهند."""
        guarded = await start_client(app_config, memory_enabled=False)
        client = guarded
        try:
            status, body = await jget(client, "/api/memory")
            assert status == 409 and body["error_code"] == "memory_disabled"
            response = await client.post("/api/memory", json={"content": "nope"})
            assert response.status == 409
            response = await client.delete("/api/memory/abc")
            assert response.status == 409
            status, body = await jget(client, "/api/reports")
            assert status == 200
            assert body["report"]["memory"]["enabled"] is False
        finally:
            await guarded.close()

    async def test_report_reflects_recorded_run(self, app_config: Config) -> None:
        """اجرای ضبط‌شده در گزارش HTTP هم دیده می‌شود."""
        from src.core.reports import ActivityRecorder

        guarded = await start_client(app_config)
        client = guarded
        try:
            recorder = ActivityRecorder.for_config(guarded.app[APP_CONFIG])
            assert recorder is not None
            recorder.record(
                {
                    "kind": "run",
                    "ok": True,
                    "iterations": 2,
                    "tools": ["terminal_run"],
                    "calls": [{"tool": "terminal_run", "ok": False, "code": "declined", "duration_ms": 5}],
                    "tokens": 40,
                    "duration_ms": 900,
                }
            )
            status, body = await jget(client, "/api/reports")
            assert status == 200
            report = body["report"]
            assert report["runs"]["total"] == 1
            assert report["safety"]["denied"] == 1
            assert "runs: 1" in body["text"]
        finally:
            await guarded.close()

    async def test_paths_require_auth(self, app_config: Config) -> None:
        """مسیرهای جدید هم زیر قفل توکن‌اند (نه استثنای باز)."""
        guarded = await start_client(app_config, server_host="0.0.0.0", server_token="s" * 24)
        client = guarded
        try:
            for path in ("/api/memory", "/api/reports"):
                status, body = await jget(client, path, headers={"X-Agent-Token": "wrong"})
                assert status == 401, path
                assert body["error_code"] == "unauthorized"
            status, body = await jget(client, "/api/memory", headers={"X-Agent-Token": "s" * 24})
            assert status == 200
        finally:
            await guarded.close()

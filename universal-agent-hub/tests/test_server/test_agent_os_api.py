"""تست اندپوینت‌های REST زیرسیستم‌های Agent-OS.

نکته: همه‌ی مسیرهای state (روتین/اعلان/ممیزی/بکاپ/skill) عمداً به ``tmp_path``
منتقل شده‌اند تا هیچ تستی به ``~/.universal-agent-hub`` واقعی دست نزند.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.config import Config
from src.server import create_app
from src.server.app import APP_SERVICES
from tests.test_server.conftest import server_config


@pytest.fixture
def os_config(config: Config, tmp_path: Path) -> Config:
    """تنظیمات سرور + مسیرهای state ایزوله."""
    return server_config(
        config,
        routines_file=str(tmp_path / "routines.json"),
        notifications_file=str(tmp_path / "notifications.jsonl"),
        audit_file=str(tmp_path / "audit.jsonl"),
        mcp_config_file=str(tmp_path / "mcp.json"),
        backup_dir=str(tmp_path / "backups"),
        skills_dirs=[str(tmp_path / "skills")],
        heartbeat_enabled=False,
    )


@pytest.fixture
async def client(os_config: Config) -> Any:
    app = create_app(os_config)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


async def jget(client: TestClient, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    response = await client.get(path, **kwargs)
    text = await response.text()
    return response.status, (json.loads(text) if text else {})


async def jpost(
    client: TestClient, path: str, payload: dict[str, Any] | None = None, **kw: Any
) -> tuple[int, dict[str, Any]]:
    response = await client.post(path, json=payload if payload is not None else {}, **kw)
    text = await response.text()
    return response.status, (json.loads(text) if text else {})


# ------------------------------------------------------------------ diagnostics
async def test_diagnostics(client: TestClient) -> None:
    status, data = await jget(client, "/api/diagnostics")
    assert status == 200
    assert data["audit"]["chain"]["ok"] is True
    assert data["security"]["injection_rules"] > 0
    assert "scheduler" in data and "watchdog" in data and "backup" in data


async def test_service_hub_is_registered(client: TestClient) -> None:
    assert client.app[APP_SERVICES] is not None
    assert client.app[APP_SERVICES].started is True


# --------------------------------------------------------------------- routines
async def test_routines_crud(client: TestClient) -> None:
    status, data = await jget(client, "/api/routines")
    assert status == 200 and data["count"] == 0

    status, routine = await jpost(
        client,
        "/api/routines",
        {"name": "nightly", "trigger": "cron", "expression": "0 22 * * *", "prompt": "summarise"},
    )
    assert status == 201
    assert routine["next_run"] > 0
    routine_id = routine["id"]

    status, data = await jget(client, "/api/routines")
    assert data["count"] == 1

    response = await client.patch(f"/api/routines/{routine_id}", json={"enabled": False})
    assert response.status == 200
    assert (await response.json())["enabled"] is False

    status, data = await jget(client, f"/api/routines/{routine_id}/history")
    assert status == 200 and data["runs"] == []

    response = await client.delete(f"/api/routines/{routine_id}")
    assert response.status == 200
    assert (await client.delete(f"/api/routines/{routine_id}")).status == 404


async def test_routine_create_validation(client: TestClient) -> None:
    status, data = await jpost(
        client, "/api/routines", {"name": "bad", "trigger": "interval", "expression": "abc", "prompt": "p"}
    )
    assert status == 400
    assert data["error_code"] == "invalid_routine"


async def test_routine_update_missing(client: TestClient) -> None:
    response = await client.patch("/api/routines/nope", json={"enabled": False})
    assert response.status == 404


async def test_routine_update_invalid(client: TestClient) -> None:
    _, routine = await jpost(
        client, "/api/routines", {"name": "r", "trigger": "interval", "expression": "60", "prompt": "p"}
    )
    response = await client.patch(f"/api/routines/{routine['id']}", json={"expression": "xyz"})
    assert response.status == 400


async def test_routine_history_missing(client: TestClient) -> None:
    assert (await client.get("/api/routines/nope/history")).status == 404


# ---------------------------------------------------------------- notifications
async def test_notifications_flow(client: TestClient) -> None:
    hub = client.app[APP_SERVICES]
    hub.notifications.push("routine", "first", "body")
    hub.notifications.push("tool", "second", "body", severity="error")

    status, data = await jget(client, "/api/notifications")
    assert status == 200
    assert data["stats"]["total"] == 2
    assert data["stats"]["unread"] == 2

    status, data = await jget(client, "/api/notifications?severity=error")
    assert len(data["notifications"]) == 1

    note_id = data["notifications"][0]["id"]
    status, data = await jpost(client, "/api/notifications/read", {"id": note_id})
    assert status == 200 and data["marked"] == 1

    status, data = await jpost(client, "/api/notifications/read", {"all": True})
    assert status == 200 and data["marked"] == 1

    status, data = await jpost(client, "/api/notifications", {})  # بدون id/all
    # POST به /api/notifications مسیر ندارد ⇒ ۴۰۵ یا ۴۰۴
    assert status in {404, 405}

    response = await client.delete("/api/notifications?read_only=1")
    assert response.status == 200
    assert (await response.json())["cleared"] == 2


async def test_notifications_read_validation(client: TestClient) -> None:
    status, data = await jpost(client, "/api/notifications/read", {})
    assert status == 400
    assert data["error_code"] == "invalid_input"


async def test_notifications_read_missing(client: TestClient) -> None:
    status, _ = await jpost(client, "/api/notifications/read", {"id": "nope"})
    assert status == 404


# ----------------------------------------------------------------------- skills
async def test_skills_endpoints(client: TestClient, os_config: Config, tmp_path: Path) -> None:
    folder = tmp_path / "skills" / "demo-skill"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo\n---\nDo the thing.", encoding="utf-8"
    )
    client.app[APP_SERVICES].skills.scan()

    status, data = await jget(client, "/api/skills")
    assert status == 200 and data["count"] == 1

    status, data = await jget(client, "/api/skills/demo-skill")
    assert status == 200
    assert "Do the thing" in data["instructions"]

    status, _ = await jget(client, "/api/skills/nope")
    assert status == 404


# -------------------------------------------------------------------------- mcp
async def test_mcp_empty(client: TestClient) -> None:
    status, data = await jget(client, "/api/mcp")
    assert status == 200
    assert data["servers"] == []
    assert data["count"] == 0


# ----------------------------------------------------------------------- backup
async def test_backup_flow(client: TestClient, tmp_path: Path) -> None:
    memory = tmp_path / "memory.jsonl"
    memory.write_text('{"id":"m1"}\n', encoding="utf-8")
    hub = client.app[APP_SERVICES]
    hub.backup.set_source("memory", memory)

    status, data = await jget(client, "/api/backup")
    assert status == 200 and data["count"] == 0

    status, info = await jpost(client, "/api/backup", {"note": "test snapshot"})
    assert status == 201
    assert info["note"] == "test snapshot"
    name = info["name"]

    status, data = await jpost(client, f"/api/backup/{name}/restore", {})
    assert status == 200
    assert data["dry_run"] is True
    assert memory.read_text(encoding="utf-8") == '{"id":"m1"}\n'  # دست‌نخورده

    memory.write_text("CORRUPT", encoding="utf-8")
    status, data = await jpost(client, f"/api/backup/{name}/restore", {"confirm": True})
    assert status == 200 and data["dry_run"] is False
    assert '{"id":"m1"}' in memory.read_text(encoding="utf-8")

    response = await client.delete(f"/api/backup/{name}")
    assert response.status == 200
    assert (await client.delete(f"/api/backup/{name}")).status == 404


async def test_backup_create_with_nothing(client: TestClient) -> None:
    hub = client.app[APP_SERVICES]
    hub.backup.sources = {}
    status, data = await jpost(client, "/api/backup", {})
    assert status == 400
    assert data["error_code"] == "backup_failed"


async def test_backup_restore_missing(client: TestClient) -> None:
    status, data = await jpost(client, "/api/backup/nope/restore", {"confirm": True})
    assert status == 400
    assert data["error_code"] == "restore_failed"


# ------------------------------------------------------------------------ audit
async def test_audit_endpoint(client: TestClient) -> None:
    hub = client.app[APP_SERVICES]
    hub.audit.record("agent", "tool.terminal_run", "ls", "allowed")

    status, data = await jget(client, "/api/audit")
    assert status == 200
    assert data["chain"]["ok"] is True
    assert data["entries"][0]["action"] == "tool.terminal_run"

    status, data = await jget(client, "/api/audit?actor=agent&limit=5")
    assert len(data["entries"]) == 1


# ---------------------------------------------------------------------- webhook
async def test_webhook_fires_routine(client: TestClient) -> None:
    hub = client.app[APP_SERVICES]

    async def runner(prompt: str, profile: str) -> dict[str, object]:
        return {"text": "webhook ran"}

    hub.scheduler.executor = runner
    _, routine = await jpost(
        client, "/api/routines", {"name": "hook", "trigger": "webhook", "expression": "deploy", "prompt": "deploy"}
    )

    status, data = await jpost(client, "/hooks/deploy", {"ref": "main"})
    assert status == 200
    assert data["status"] == "ok"
    assert routine["trigger"] == "webhook"


async def test_webhook_unknown_token(client: TestClient) -> None:
    status, data = await jpost(client, "/hooks/nope", {})
    assert status == 404
    assert data["error_code"] == "not_found"


async def test_webhook_requires_no_server_token(os_config: Config) -> None:
    """webhook بیرون از /api است ⇒ با توکن سرور هم بدون هدر کار می‌کند."""
    cfg = os_config.model_copy(update={"server_token": "secret-token"})
    app = create_app(cfg)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        response = await test_client.post("/hooks/anything", json={})
        assert response.status == 404  # ناشناخته، ولی *مسدود نشده*
        # در مقابل، /api بدون توکن رد می‌شود:
        api_response = await test_client.get("/api/routines")
        assert api_response.status == 401
    finally:
        await test_client.close()

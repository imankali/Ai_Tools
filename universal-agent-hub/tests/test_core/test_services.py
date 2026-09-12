"""تست composition root سرویس‌ها."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Config
from src.core.event_bus import EventBus
from src.core.services import ServiceHub


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        openai_api_key="sk-test-0000000000",
        project_root=tmp_path,
        log_file=None,
        log_level="WARNING",
        routines_file=tmp_path / "routines.json",
        notifications_file=tmp_path / "notifications.jsonl",
        audit_file=tmp_path / "audit.jsonl",
        mcp_config_file=tmp_path / "mcp.json",
        backup_dir=tmp_path / "backups",
        skills_dirs=[str(tmp_path / "skills")],
        heartbeat_interval=10.0,
        watchdog_timeout=30.0,
    )


def test_builds_all_services(config: Config) -> None:
    hub = ServiceHub(config)
    assert hub.audit is not None
    assert hub.notifications is not None
    assert hub.scheduler is not None
    assert hub.subagents is not None
    assert hub.backup is not None
    assert hub.heartbeat is not None
    assert hub.watchdog is not None
    assert len(hub.skills) == 0  # پوشه‌ی skills هنوز خالی است


def test_disabled_services_are_none(tmp_path: Path) -> None:
    cfg = Config(
        openai_api_key="x",
        project_root=tmp_path,
        log_file=None,
        audit_enabled=False,
        notifications_enabled=False,
        routines_file=tmp_path / "r.json",
        backup_dir=tmp_path / "b",
        skills_dirs=[str(tmp_path / "s")],
    )
    hub = ServiceHub(cfg)
    assert hub.audit is None
    assert hub.notifications is None


def test_skills_are_scanned(config: Config, tmp_path: Path) -> None:
    folder = tmp_path / "skills" / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody", encoding="utf-8")
    hub = ServiceHub(config)
    assert len(hub.skills) == 1


def test_bus_bridge_on_construction(config: Config) -> None:
    bus = EventBus()
    hub = ServiceHub(config, bus=bus)
    bus.publish_nowait("tool.blocked", {"tool": "terminal_run"}, source="test")
    assert hub.notifications is not None
    assert hub.notifications.unread_count() >= 1


def test_notify_helper_without_center(tmp_path: Path) -> None:
    cfg = Config(
        openai_api_key="x",
        project_root=tmp_path,
        log_file=None,
        notifications_enabled=False,
        audit_enabled=False,
        routines_file=tmp_path / "r.json",
        backup_dir=tmp_path / "b",
        skills_dirs=[str(tmp_path / "s")],
    )
    ServiceHub(cfg)._notify("x", "t", "b")  # نباید استثنا بدهد


def test_start_and_stop(config: Config) -> None:
    import asyncio

    async def runner(prompt: str, profile: str) -> dict[str, object]:
        return {"text": "ok"}

    async def scenario() -> None:
        hub = ServiceHub(config)
        summary = await hub.start(agent_runner=runner)
        assert summary["already_started"] is False
        assert summary["scheduler"] is True
        assert hub.started is True
        assert (await hub.start())["already_started"] is True
        await hub.stop()
        assert hub.started is False
        await hub.stop()  # idempotent

    asyncio.run(scenario())


def test_start_without_runner_leaves_scheduler_off(config: Config) -> None:
    import asyncio

    async def scenario() -> None:
        hub = ServiceHub(config)
        summary = await hub.start()
        assert summary["scheduler"] is False
        await hub.stop()

    asyncio.run(scenario())


def test_heartbeat_can_be_enabled(tmp_path: Path) -> None:
    import asyncio

    cfg = Config(
        openai_api_key="x",
        project_root=tmp_path,
        log_file=None,
        heartbeat_enabled=True,
        heartbeat_interval=10.0,
        routines_file=tmp_path / "r.json",
        audit_file=tmp_path / "a.jsonl",
        backup_dir=tmp_path / "b",
        skills_dirs=[str(tmp_path / "s")],
        heartbeat_disk_min_mb=0,
    )

    async def scenario() -> None:
        hub = ServiceHub(cfg)
        summary = await hub.start()
        assert summary["heartbeat"] is True
        await hub.stop()

    asyncio.run(scenario())


def test_audit_chain_check(config: Config) -> None:
    import asyncio

    hub = ServiceHub(config)
    assert hub.audit is not None
    hub.audit.record("agent", "x")
    result = asyncio.run(hub._check_audit_chain())
    assert result.ok is True


def test_audit_chain_check_detects_tampering(config: Config) -> None:
    import asyncio

    hub = ServiceHub(config)
    assert hub.audit is not None
    hub.audit.record("agent", "a")
    path = config.audit_path
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    payload["action"] = "hacked"
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    result = asyncio.run(hub._check_audit_chain())
    assert result.ok is False
    assert result.severity == "error"
    assert hub.notifications is not None
    assert any(n.severity.value == "critical" for n in hub.notifications.list())


def test_watchdog_timeout_notifies(config: Config) -> None:
    hub = ServiceHub(config)
    hub._on_watchdog_timeout("k1", "stuck run")
    assert hub.notifications is not None
    assert any("stuck run" in n.title for n in hub.notifications.list())
    assert hub.audit is not None
    assert any(e.action == "watchdog.released" for e in hub.audit.entries())


def test_mcp_disabled(config: Config) -> None:
    import asyncio

    cfg = config.model_copy(update={"mcp_enabled": False})
    assert asyncio.run(ServiceHub(cfg).connect_mcp()) == []


def test_mcp_connects_from_config(config: Config, tmp_path: Path) -> None:
    import asyncio
    import sys
    import textwrap

    script = tmp_path / "srv.py"
    script.write_text(
        textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                mid = msg.get("id")
                if msg.get("method") == "initialize":
                    r = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "s"}, "capabilities": {}}
                elif msg.get("method") == "tools/list":
                    r = {"tools": [{"name": "t", "description": "d", "inputSchema": {}}]}
                else:
                    continue
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": r}) + "\\n")
                sys.stdout.flush()
            """),
        encoding="utf-8",
    )
    config.mcp_config_path.write_text(
        json.dumps({"mcpServers": {"local": {"command": sys.executable, "args": [str(script)]}}}), encoding="utf-8"
    )

    async def scenario() -> None:
        hub = ServiceHub(config)
        connected = await hub.connect_mcp()
        assert connected == ["local"]
        assert len(hub.mcp_tools()) == 1
        await hub.stop()

    asyncio.run(scenario())


def test_mcp_bad_server_is_reported(config: Config) -> None:
    import asyncio

    config.mcp_config_path.write_text(
        json.dumps({"mcpServers": {"broken": {"command": "/nonexistent-binary-xyz"}}}), encoding="utf-8"
    )

    async def scenario() -> None:
        hub = ServiceHub(config)
        assert await hub.connect_mcp() == []
        assert hub.notifications is not None
        assert any("broken" in n.title for n in hub.notifications.list())
        await hub.stop()

    asyncio.run(scenario())


def test_diagnostics(config: Config) -> None:
    hub = ServiceHub(config)
    info = hub.diagnostics()
    assert info["started"] is False
    assert info["audit"]["chain"]["ok"] is True
    assert "scheduler" in info
    assert "watchdog" in info
    assert "backup" in info
    assert info["security"]["injection_rules"] > 0
    assert set(info["state_files"]) >= {"routines", "notifications", "audit"}


def test_tool_context_services(config: Config) -> None:
    hub = ServiceHub(config)
    services = hub.tool_context_services()
    assert services["skills"] is hub.skills
    assert services["subagents"] is hub.subagents
    assert services["mcp"] is None  # هنوز اتصال MCP نداریم

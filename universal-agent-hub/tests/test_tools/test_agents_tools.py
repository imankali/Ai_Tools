"""تست ابزارهای agent_delegate / skill_list / skill_load / routine_list."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.base_tool import ToolContext
from src.core.routines import Routine, RoutineScheduler, TriggerKind
from src.core.skills import SkillLibrary
from src.core.subagents import SubagentPool
from src.tools.agents import DelegateTool, RoutineListTool, SkillListTool, SkillLoadTool

SKILL_MD = """---
name: pg-backup
description: Back up a PostgreSQL database safely.
version: 1.0.0
allowed_tools: [terminal_run]
---
# Steps
1. pg_dump --format=custom
"""


class DummyRun:
    def __init__(self, text: str = "child answer") -> None:
        self.text = text
        self.tool_names = ["read_file"]


class DummyAgent:
    async def ask(self, prompt: str) -> DummyRun:
        return DummyRun(f"answer to {prompt[:8]}")

    async def close(self) -> None:
        return None


async def _exec(prompt: str, profile: str) -> dict[str, object]:
    return {"text": "routine done"}


@pytest.fixture
def skill_library(tmp_path: Path) -> SkillLibrary:
    folder = tmp_path / "skills" / "pg-backup"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    library = SkillLibrary([tmp_path / "skills"])
    library.scan()
    return library


def test_delegate_disabled_without_pool() -> None:
    result = asyncio.run(DelegateTool().execute(prompt="x", context=ToolContext()))
    assert result.success is False
    assert result.error_code == "not_enabled"


def test_delegate_safety_denies_without_pool() -> None:
    decision = asyncio.run(DelegateTool().safety_check({"prompt": "x"}, ToolContext()))
    assert decision.allowed is False


def test_delegate_success() -> None:
    pool = SubagentPool(factory=lambda profile: DummyAgent())
    context = ToolContext(subagents=pool)
    tool = DelegateTool()
    decision = asyncio.run(tool.safety_check({"prompt": "summarise"}, context))
    assert decision.allowed is True
    assert decision.requires_confirmation is True
    result = asyncio.run(tool.execute(prompt="summarise", profile="read_only", context=context))
    assert result.success is True
    assert result.data["profile"] == "read_only"
    assert "answer to" in result.data["result"]


def test_delegate_empty_prompt() -> None:
    pool = SubagentPool(factory=lambda profile: DummyAgent())
    result = asyncio.run(DelegateTool().execute(prompt="   ", context=ToolContext(subagents=pool)))
    assert result.success is False
    assert result.error_code == "invalid_input"


def test_delegate_depth_limit_denied() -> None:
    pool = SubagentPool(factory=lambda profile: DummyAgent(), max_depth=0)
    decision = asyncio.run(DelegateTool().safety_check({"prompt": "x"}, ToolContext(subagents=pool)))
    assert decision.allowed is False
    assert "depth" in decision.reasons[0]


def test_delegate_child_failure() -> None:
    class Broken:
        async def ask(self, prompt: str) -> DummyRun:
            raise RuntimeError("child died")

        async def close(self) -> None:
            return None

    pool = SubagentPool(factory=lambda profile: Broken())
    result = asyncio.run(DelegateTool().execute(prompt="x", context=ToolContext(subagents=pool)))
    assert result.success is False
    assert result.error_code == "subagent_failed"


def test_skill_list_without_library() -> None:
    result = asyncio.run(SkillListTool().execute(context=ToolContext()))
    assert result.success is True
    assert result.data["skills"] == []


def test_skill_list(skill_library: SkillLibrary) -> None:
    context = ToolContext(skills=skill_library)
    result = asyncio.run(SkillListTool().execute(context=context))
    assert result.data["count"] == 1
    assert result.data["skills"][0]["name"] == "pg-backup"


def test_skill_list_with_query(skill_library: SkillLibrary) -> None:
    result = asyncio.run(SkillListTool().execute(query="postgres backup", context=ToolContext(skills=skill_library)))
    assert result.data["count"] == 1


def test_skill_load(skill_library: SkillLibrary) -> None:
    result = asyncio.run(SkillLoadTool().execute(name="pg-backup", context=ToolContext(skills=skill_library)))
    assert result.success is True
    assert "pg_dump" in result.data["instructions"]
    assert result.data["allowed_tools"] == ["terminal_run"]


def test_skill_load_unknown(skill_library: SkillLibrary) -> None:
    result = asyncio.run(SkillLoadTool().execute(name="nope", context=ToolContext(skills=skill_library)))
    assert result.success is False
    assert result.error_code == "not_found"
    assert "pg-backup" in result.metadata["available"]


def test_skill_load_disabled(skill_library: SkillLibrary) -> None:
    skill_library.set_enabled("pg-backup", False)
    result = asyncio.run(SkillLoadTool().execute(name="pg-backup", context=ToolContext(skills=skill_library)))
    assert result.error_code == "disabled"


def test_skill_load_without_library() -> None:
    result = asyncio.run(SkillLoadTool().execute(name="x", context=ToolContext()))
    assert result.error_code == "not_enabled"


def test_routine_list_without_scheduler() -> None:
    result = asyncio.run(RoutineListTool().execute(context=ToolContext()))
    assert result.data["routines"] == []


def test_routine_list(tmp_path: Path) -> None:
    scheduler = RoutineScheduler(tmp_path / "r.json", executor=_exec)
    scheduler.add(Routine(name="nightly", trigger=TriggerKind.CRON, expression="0 22 * * *", prompt="report"))
    result = asyncio.run(RoutineListTool().execute(context=ToolContext(scheduler=scheduler)))
    assert result.data["count"] == 1
    assert result.data["routines"][0]["name"] == "nightly"


def test_schemas_are_valid() -> None:
    for tool in (DelegateTool(), SkillListTool(), SkillLoadTool(), RoutineListTool()):
        schema = tool.get_schema()
        assert schema["function"]["name"] == tool.name
        assert schema["type"] == "function"
        assert "parameters" in schema["function"]
        info = tool.to_info()
        assert info.name == tool.name


def test_delegate_is_confirmation_required() -> None:
    assert DelegateTool.requires_confirmation is True
    assert SkillListTool.requires_confirmation is False

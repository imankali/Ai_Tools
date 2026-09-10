"""تست کارخانه‌ی ساخت ایجنت (:mod:`src.core.agent_factory`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent import UniversalAgent
from src.config import Config
from src.core.agent_factory import PROFILE_REGISTRY, AgentFactory
from src.core.event_bus import EventBus
from src.core.tool_registry import ToolRegistry, discover_tools
from src.models.config_models import AgentProfile
from src.models.tool_models import ToolCategory
from tests.fakes import FakeOpenAIClient, make_chat_response


@pytest.fixture(autouse=True)
def _clean_profiles() -> Any:
    """تغییرات پروفایل یک تست به تست دیگر نشت نکند."""
    snapshot = dict(PROFILE_REGISTRY)
    yield
    PROFILE_REGISTRY.clear()
    PROFILE_REGISTRY.update(snapshot)


@pytest.fixture
def client() -> FakeOpenAIClient:
    """client ساختگی با یک پاسخ متنی."""
    return FakeOpenAIClient([make_chat_response(content="ok")])


def test_default_profiles_exist() -> None:
    """پروفایل‌های پیش‌فرض ثبت‌اند."""
    AgentFactory.ensure_loaded()
    assert {"generalist", "read_only", "developer", "ops"} <= set(PROFILE_REGISTRY)
    assert "generalist" in AgentFactory.profiles()


def test_create_generalist_exposes_every_tool(config: Config, client: FakeOpenAIClient) -> None:
    """پروفایل generalist همه‌ی ابزارهای رجیستری را فعال می‌کند."""
    ToolRegistry.set_default_config(config)
    discover_tools(force=True)
    agent = AgentFactory.create("generalist", config=config, client=client)
    assert isinstance(agent, UniversalAgent)
    assert set(agent.tool_names) == set(ToolRegistry.names())
    assert len(agent.schemas) == len(agent.tool_names)
    assert agent.safety is not None and agent.event_bus is not None
    assert agent.profile is not None and agent.profile.name == "generalist"


def test_read_only_profile_excludes_mutating_tools(config: Config, client: FakeOpenAIClient) -> None:
    """پروفایل read_only ابزارهای تغییری را نمی‌دهد."""
    agent = AgentFactory.create("read_only", config=config, client=client)
    for forbidden in ("write_file", "delete_file", "move_file", "terminal_run", "browser_fill_form"):
        assert forbidden not in agent.tool_names, forbidden
    assert "read_file" in agent.tool_names and "list_directory" in agent.tool_names


def test_developer_and_ops_profiles(config: Config, client: FakeOpenAIClient) -> None:
    """پروفایل‌های تخصصی، ابزارها و دمای متفاوتی دارند."""
    developer = AgentFactory.create("developer", config=config, client=client)
    ops = AgentFactory.create("ops", config=config, client=client)
    assert "terminal_run" in developer.tool_names and "browser_browse" not in developer.tool_names
    assert developer.config.temperature == 0.2
    assert set(ops.tool_names) <= {
        "terminal_run",
        "os_info",
        "cpu_info",
        "memory_info",
        "disk_info",
        "network_info",
        "list_directory",
        "read_file",
    }
    assert ops.config.temperature == 0.1


def test_category_filtering(client: FakeOpenAIClient, config: Config) -> None:
    """فیلتر بر اساس دسته‌ی ابزار."""
    profile = AgentProfile(name="sysinfo", categories=[ToolCategory.SYSTEM], temperature=0.5)
    agent = AgentFactory.create(profile, config=config, client=client)
    assert sorted(agent.tool_names) == ["cpu_info", "disk_info", "memory_info", "network_info", "os_info"]
    assert agent.config.temperature == 0.5


def test_auto_confirm_all_disables_confirmation(config: Config, client: FakeOpenAIClient) -> None:
    """پروفایل با auto_confirm_all، تأیید را خاموش می‌کند."""
    profile = AgentProfile(name="yolo", auto_confirm_all=True, max_tool_iterations=3, model="my-model")
    agent = AgentFactory.create(profile, config=config, client=client)
    assert agent.config.enable_confirmation is False
    assert agent.config.max_tool_iterations == 3
    assert agent.config.model_name == "my-model" and agent.model == "my-model"


def test_profile_safety_overrides_block_tools(config: Config, client: FakeOpenAIClient) -> None:
    """قوانین ایمنی پروفایل به guard منتقل می‌شود."""
    profile = AgentProfile(name="locked", safety={"blocked_commands": [r"\bwipe\b"], "read_only_paths": ["docs/*"]})
    agent = AgentFactory.create(profile, config=config, client=client)
    decision = agent.safety.assess_command("wipe disks now")
    assert decision.risk.value == "critical"
    assert agent.safety.read_only_patterns == ["docs/*"]


def test_system_prompt_mentions_tools_and_profile(client: FakeOpenAIClient, config: Config) -> None:
    """system prompt شامل ابزارها و توضیح پروفایل است."""
    profile = AgentProfile(
        name="narrow",
        enabled_tools=["os_info"],
        description="tiny agent",
        system_prompt_extra="Only report OS facts.",
    )
    agent = AgentFactory.create(profile, config=config, client=client)
    prompt = agent.system_prompt
    assert "`os_info`" in prompt
    assert "Only report OS facts." in prompt and "tiny agent" in prompt
    # و همان prompt واقعاً به مدل می‌رود
    import asyncio

    asyncio.run(agent.ask("hi"))
    sent = agent.client.payloads[0]["messages"][0]["content"]
    assert "Only report OS facts." in sent


def test_unknown_tool_names_are_dropped_with_warning(
    config: Config, client: FakeOpenAIClient, caplog: pytest.LogCaptureFixture
) -> None:
    """نام ابزار ناموجود نادیده گرفته می‌شود (بدون شکستن اجرا)."""
    profile = AgentProfile(name="ghosty", enabled_tools=["os_info", "does_not_exist"])
    agent = AgentFactory.create(profile, config=config, client=client)
    assert agent.tool_names == ["os_info"]
    assert "does_not_exist" not in agent.tool_names and len(agent.tools) == 1


def test_explicit_tools_argument_wins(config: Config, client: FakeOpenAIClient) -> None:
    """پارامتر tools بر پروفایل اولویت دارد."""
    agent = AgentFactory.create("read_only", config=config, client=client, tools=["os_info", "memory_info"])
    assert agent.tool_names == ["memory_info", "os_info"]


def test_unknown_profile_raises() -> None:
    """پروفایل ناشناخته KeyError می‌دهد."""
    with pytest.raises(KeyError, match="unknown profile"):
        AgentFactory.get_profile("nope")
    with pytest.raises(KeyError):
        AgentFactory.create("nope", config=Config(openai_api_key="x", log_file=None), client=FakeOpenAIClient([]))


def test_register_and_duplicate_profiles(config: Config, client: FakeOpenAIClient) -> None:
    """ثبت پروفایل جدید و جلوگیری از تکرار."""
    profile = AgentProfile(name="mine", enabled_tools=["os_info"])
    assert AgentFactory.register(profile) == "mine"
    with pytest.raises(ValueError, match="already exists"):
        AgentFactory.register(profile, replace=False)
    AgentFactory.register(profile.model_copy(update={"description": "updated"}))
    assert AgentFactory.get_profile("mine").description == "updated"
    agent = AgentFactory.create(profile, config=config, client=client)
    assert agent.tool_names == ["os_info"]
    # ساخت از روی نام هم کار می‌کند
    assert AgentFactory.create("mine", config=config, client=client).tool_names == ["os_info"]


def test_register_builder_and_agent_class(config: Config, client: FakeOpenAIClient) -> None:
    """builder و agent_class سفارشی."""

    class MyAgent(UniversalAgent):
        """ایجنت با یک attribute اضافه."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.marker = "custom"

    AgentFactory.register_builder("special", lambda **kwargs: MyAgent(**kwargs))
    profile = AgentProfile(name="special", enabled_tools=["os_info"])
    AgentFactory.register(profile)
    built = AgentFactory.create("special", config=config, client=client)
    assert isinstance(built, MyAgent) and built.marker == "custom"
    via_class = AgentFactory.create(profile, config=config, client=client, agent_class=MyAgent)
    assert isinstance(via_class, MyAgent)


def test_event_bus_is_shared_and_logged(config: Config, client: FakeOpenAIClient) -> None:
    """باس تزریق‌شده استفاده می‌شود و observer لاگ وصل است."""
    bus = EventBus(history_size=5)
    agent = AgentFactory.create("generalist", config=config, client=client, event_bus=bus, log=True)
    assert agent.event_bus is bus
    assert any(pattern == "#" for pattern in [item.split(":")[0] for item in bus.listeners()])
    agent2 = AgentFactory.create("generalist", config=config, client=client, log=False)
    assert not agent2.event_bus.listeners()


def test_persisted_events_file(config: Config, client: FakeOpenAIClient, tmp_path: Path) -> None:
    """فایل JSONL رویدادها از config ساخته می‌شود."""
    cfg = config.model_copy(update={"events_log_file": "events.jsonl", "project_root": tmp_path})
    agent = AgentFactory.create("generalist", config=cfg, client=client, log=False)
    import asyncio

    asyncio.run(agent.ask("hello"))
    log_file = tmp_path / "events.jsonl"
    assert log_file.is_file()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert {"agent.started", "agent.completed"} <= {line["kind"] for line in lines}


def test_load_profiles_from_dir(tmp_path: Path, config: Config, client: FakeOpenAIClient) -> None:
    """بارگذاری پروفایل‌ها از پوشه (با نادیده‌گرفتن فایل خراب)."""
    (tmp_path / "audit.json").write_text(
        json.dumps(
            {
                "name": "audit",
                "description": "read only",
                "enabled_tools": ["os_info", "read_file"],
                "temperature": 0.0,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    loaded = AgentFactory.load_profiles_from_dir(tmp_path)
    assert loaded == ["audit"]
    assert AgentFactory.get_profile("audit").temperature == 0.0
    agent = AgentFactory.create("audit", config=config, client=client)
    assert agent.tool_names == ["os_info", "read_file"]
    prefixed = AgentFactory.load_profiles_from_dir(tmp_path, prefix="x_")
    assert "x_audit" in prefixed


def test_load_profiles_from_missing_dir(tmp_path: Path) -> None:
    """پوشه‌ی ناموجود فهرست خالی می‌دهد."""
    assert AgentFactory.load_profiles_from_dir(tmp_path / "nope") == []


def test_create_uses_global_config_when_omitted(client: FakeOpenAIClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """config=None از get_config خوانده می‌شود."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global-test")
    from src.config import reset_config

    reset_config()
    agent = AgentFactory.create("generalist", client=client, log=False)
    assert agent.config.openai_api_key == "sk-global-test"
    reset_config()


def test_yaml_profile_requires_optional_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """پروفایل YAML بدون نصب pyyaml پیام خطای واضح می‌دهد."""
    path = tmp_path / "p.yaml"
    path.write_text("name: yamlprofile\n", encoding="utf-8")
    try:  # pragma: no cover - بستگی به محیط دارد
        import yaml  # noqa: F401

        pytest.skip("pyyaml is installed; the error path is not reachable")
    except ImportError:
        from src.models.config_models import load_profile_file

        with pytest.raises(ValueError, match="pyyaml"):
            load_profile_file(path)


def test_profile_model_fallbacks_applied(config: Config, client: FakeOpenAIClient) -> None:
    """مدل پروفایل، لیست candidate های ایجنت را هم به‌روز می‌کند."""
    cfg = config.model_copy(update={"model_name": "gpt-6-astra", "model_fallbacks": ["m2"]})
    agent = AgentFactory.create(AgentProfile(name="m", model="primary-model"), config=cfg, client=client)
    assert agent.model == "primary-model"
    assert agent._model_candidates[0] == "primary-model"  # noqa: SLF001
    assert "m2" in agent._model_candidates  # noqa: SLF001


def test_safety_guard_comes_from_config(config: Config, client: FakeOpenAIClient) -> None:
    """guard از config ساخته می‌شود و سیاست را حفظ می‌کند."""
    strict = config.model_copy(update={"dangerous_command_policy": "deny"})
    agent = AgentFactory.create("generalist", config=strict, client=client)
    assert agent.safety.policy == "deny"
    assert str(config.project_root) in agent.safety.summary()["allowed_directories"][0]


def test_profile_without_tools_uses_registry(config: Config, client: FakeOpenAIClient) -> None:
    """پروفایل بدون فیلتر = همه‌ی ابزارها."""
    profile: AgentProfile | None = AgentProfile(name="wide")
    agent = AgentFactory.create(profile, config=config, client=client)
    assert set(agent.tool_names) == set(ToolRegistry.names())

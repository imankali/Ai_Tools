"""تست پیکربندی (:mod:`src.config`) و مدل‌های پشتیبان."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.config import DEFAULT_MODEL, KNOWN_PUBLIC_MODELS, Config, get_config, reset_config
from src.models.config_models import AgentProfile, SafetySettings, load_profile_file


class TestDefaults:
    """مقادیر پیش‌فرض و نرمال‌سازی."""

    def test_default_model_is_project_choice(self) -> None:
        """مدل پیش‌فرض همان gpt-6-astra است (قابل بازنویسی با env)."""
        config = Config(openai_api_key="sk-x", log_file=None)
        assert config.model_name == DEFAULT_MODEL
        assert config.active_model == DEFAULT_MODEL
        assert not config.known_model

    def test_sane_security_defaults(self) -> None:
        """پیش‌فرض‌ها باید امن باشند: تأیید روشن، sandbox فعال."""
        config = Config(openai_api_key="sk-x", log_file=None)
        assert config.enable_confirmation
        assert config.dangerous_command_policy == "confirm"
        assert config.allow_shell
        assert not config.unrestricted_filesystem
        assert config.max_command_timeout == 30
        assert config.temperature == 0.7

    def test_log_level_normalized(self) -> None:
        """سطح لاگ نامعتبر به INFO تبدیل می‌شود."""
        assert Config(openai_api_key="x", log_level="verbose", log_file=None).log_level == "INFO"
        assert Config(openai_api_key="x", log_level="debug", log_file=None).log_level == "DEBUG"

    @pytest.mark.parametrize("value", ["deny", "DENY", "confirm", "nonsense"])
    def test_policy_normalized(self, value: str) -> None:
        """سیاست دستورهای خطرناک فقط confirm/deny است."""
        expected = "deny" if value.lower() == "deny" else "confirm"
        assert (
            Config(openai_api_key="x", log_file=None, dangerous_command_policy=value).dangerous_command_policy
            == expected
        )

    def test_search_backend_whitelist(self) -> None:
        """backend نامعتبر به auto برمی‌گردد."""
        assert Config(openai_api_key="x", log_file=None, search_backend="google").search_backend == "auto"
        assert Config(openai_api_key="x", log_file=None, search_backend="offline").search_backend == "offline"

    def test_comma_lists_and_json(self) -> None:
        """لیست‌ها با کاما یا JSON پذیرفته می‌شوند."""
        config = Config(openai_api_key="x", log_file=None, model_fallbacks="a, b", allowed_directories='["c", "d"]')
        assert config.model_fallbacks == ["a", "b"]
        assert [Path(p).name for p in config.allowed_directories] == ["c", "d"]

    def test_empty_list_string_becomes_empty(self) -> None:
        """رشته‌ی خالی لیست را تهی می‌کند (نه [''])."""
        assert Config(openai_api_key="x", log_file=None, allowed_directories="").allowed_directories == []

    def test_paths_are_expanded(self, tmp_path: Path) -> None:
        """مسیرهای نسبی/`~` به مطلق تبدیل می‌شوند."""
        config = Config(
            openai_api_key="x", log_file=None, allowed_directories=["./out", str(tmp_path)], project_root=tmp_path
        )
        assert all(Path(p).is_absolute() for p in config.allowed_directories)
        assert str(tmp_path) in config.allowed_directories

    def test_temperature_bounds(self) -> None:
        """دمای خارج از بازه رد می‌شود."""
        with pytest.raises(PydanticValidationError):
            Config(openai_api_key="x", log_file=None, temperature=5)
        with pytest.raises(PydanticValidationError):
            Config(openai_api_key="x", log_file=None, max_command_timeout=0)

    def test_output_tokens_capped(self) -> None:
        """سقف توکن خروجی محدود می‌شود."""
        assert Config(openai_api_key="x", log_file=None, max_output_tokens=10_000_000).max_output_tokens == 32000

    def test_paths_resolution(self, tmp_path: Path) -> None:
        """فایل‌های لاگ/رویداد نسبت به ریشه‌ی پروژه حل می‌شوند."""
        config = Config(
            openai_api_key="x", project_root=tmp_path, log_file="logs/a.log", events_log_file="events.jsonl"
        )
        assert config.resolved_log_file == tmp_path / "logs" / "a.log"
        assert config.event_log_path == tmp_path / "events.jsonl"
        assert Config(openai_api_key="x", log_file="", events_log_file=None).resolved_log_file is None
        assert Config(openai_api_key="x", log_file=None, events_log_file=None).event_log_path is None


class TestEnvironment:
    """خواندن از محیط و .env."""

    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """متغیرهای محیطی (با alias) خوانده می‌شوند."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        monkeypatch.setenv("MODEL_NAME", "my-local-model")
        monkeypatch.setenv("MODEL_FALLBACKS", "a,b")
        monkeypatch.setenv("MAX_COMMAND_TIMEOUT", "11")
        monkeypatch.setenv("ENABLE_CONFIRMATION", "false")
        monkeypatch.setenv("ALLOW_SHELL", "false")
        config = Config(log_file=None, _env_file=None)
        assert config.openai_api_key == "sk-from-env"
        assert config.model_name == "my-local-model"
        assert config.model_fallbacks == ["a", "b"]
        assert config.max_command_timeout == 11
        assert not config.enable_confirmation
        assert not config.allow_shell

    def test_dotenv_file_in_cwd_is_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """فایل ``.env" در پوشه‌ی جاری (رفتار مستندات) خوانده می‌شود."""
        (tmp_path / ".env").write_text(
            "MODEL_NAME=from-dotenv\nTEMPERATURE=0.1\nALLOWED_DIRECTORIES=.", encoding="utf-8"
        )
        for key in ("MODEL_NAME", "TEMPERATURE", "ALLOWED_DIRECTORIES", "OPENAI_BASE_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        config = Config(openai_api_key="sk", log_file=None)
        assert config.model_name == "from-dotenv"
        assert config.temperature == 0.1

    def test_singleton_and_reset(self) -> None:
        """get_config کش می‌کند و reset_config آن را پاک می‌کند."""
        reset_config()
        first = get_config()
        assert get_config() is first
        reset_config()
        assert get_config() is not first


class TestBehaviour:
    """رفتارها و خروجی‌های کمکی."""

    def test_api_key_detection(self) -> None:
        """کلید تستی «واقعی» حساب نمی‌شود."""
        assert not Config(openai_api_key="", log_file=None).is_api_key_set
        assert not Config(openai_api_key="test-key", log_file=None).is_api_key_set
        assert Config(openai_api_key="sk-proj-real", log_file=None).is_api_key_set

    def test_model_candidates_order(self) -> None:
        """مدل اصلی اول، سپس fallback های یکتا."""
        config = Config(openai_api_key="x", log_file=None, model_name="m1", model_fallbacks=["m2", "m1", "", "m3"])
        assert config.safe_model_candidates == ["m1", "m2", "m3"]
        empty = Config(openai_api_key="x", log_file=None, model_name="  ", model_fallbacks=[])
        assert empty.model_name == DEFAULT_MODEL  # مقدار خالی به پیش‌فرض برمی‌گردد
        assert empty.safe_model_candidates == [DEFAULT_MODEL]

    def test_known_public_models_are_documented(self) -> None:
        """همه‌ی مدل‌های شناخته‌شده، known هستند."""
        for name in KNOWN_PUBLIC_MODELS:
            assert Config(openai_api_key="x", log_file=None, model_name=name).known_model

    def test_safe_dict_masks_secrets(self) -> None:
        """dict نمایشی بدون افشای کلیدها."""
        config = Config(openai_api_key="sk-secret-1234567890", tavily_api_key="tvly-abcdefgh", log_file=None)
        payload = config.to_safe_dict()
        assert "sk-secret-1234567890" not in str(payload)
        assert payload["openai_api_key"].startswith("sk-sec")
        assert payload["tavily_api_key"] == "***"
        assert "model_name" in payload

    def test_extra_env_keys_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """متغیرهای ناشناخته محیط باعث خطا نمی‌شوند."""
        monkeypatch.setenv("SOMETHING_UNKNOWN", "1")
        assert Config(openai_api_key="x", log_file=None).openai_api_key == "x"


class TestProfiles:
    """پروفایل‌ها و تنظیمات ایمنی فایل‌محور."""

    def test_profile_defaults_and_filters(self) -> None:
        """منطق should_include."""
        profile = AgentProfile(name="p", enabled_tools=["a", "b"])
        assert profile.should_include("a") and not profile.should_include("c")
        other = AgentProfile(name="q", disabled_tools=["c"])
        assert other.should_include("a") and not other.should_include("c")
        assert AgentProfile().should_include("anything")

    def test_profile_comma_lists(self) -> None:
        """پذیرش رشته‌ی کامایی."""
        profile = AgentProfile(enabled_tools="a,b , c")
        assert profile.enabled_tools == ["a", "b", "c"]

    def test_safety_settings_risk_coercion(self) -> None:
        """RiskLevel از رشته."""
        settings = SafetySettings(max_risk_level="high", blocked_commands=["rm"])
        assert settings.max_risk_level.value == "high"
        with pytest.raises(PydanticValidationError):
            SafetySettings(max_risk_level="apocalyptic")

    def test_load_profile_json(self, tmp_path: Path) -> None:
        """بارگذاری پروفایل از JSON."""
        path = tmp_path / "profile.json"
        path.write_text(
            json.dumps(
                {
                    "name": "auditor",
                    "description": "read only",
                    "disabled_tools": ["terminal_run"],
                    "safety": {"network_allowed": False},
                }
            ),
            encoding="utf-8",
        )
        profile = load_profile_file(path)
        assert profile.name == "auditor"
        assert profile.safety.network_allowed is False
        assert not profile.should_include("terminal_run")

    @pytest.mark.parametrize("content", ["{not json", "[1,2]", '{"temperature": 99}'])
    def test_load_profile_errors(self, tmp_path: Path, content: str) -> None:
        """فایل خراب/نامعتبر پیام خطای واضح می‌دهد."""
        path = tmp_path / "bad.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            load_profile_file(path)

    def test_load_profile_missing_or_unsupported(self, tmp_path: Path) -> None:
        """فایل ناموجود یا پسوند ناشناخته."""
        with pytest.raises(FileNotFoundError):
            load_profile_file(tmp_path / "nope.json")
        other = tmp_path / "profile.txt"
        other.write_text("name: x", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported"):
            load_profile_file(other)

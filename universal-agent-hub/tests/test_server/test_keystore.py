"""تست مخزن پروفایل‌های کلید API (فایل JSON محلی، بدون افشای راز)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from src.server.keystore import ApiKeyProfile, KeyStore, redact_text

SECRET = "sk-live-abcdefghijklmnop"


class TestApiKeyProfile:
    """خودِ رکورد کلید."""

    def test_masked_key_shape(self) -> None:
        """ماسک، ابتدا و انتهای کلید را نگه می‌دارد و بقیه را نه."""
        profile = ApiKeyProfile(name="work", api_key=SECRET)
        masked = profile.masked_key
        assert "…" in masked
        assert "bcdefghijklmno" not in masked
        assert masked != SECRET

    def test_masked_key_when_empty(self) -> None:
        """بدون کلید → توضیح خالی بودن."""
        profile = ApiKeyProfile(name="work")
        assert profile.is_complete is False
        assert "…" not in profile.masked_key

    def test_to_dict_hides_secret_by_default(self) -> None:
        """``to_dict`` پیش‌فرض کلید را بیرون نمی‌دهد."""
        profile = ApiKeyProfile(name="work", api_key=SECRET, model="gpt-6-astra")
        assert SECRET not in json.dumps(profile.to_dict())
        assert profile.to_dict(include_secret=True)["api_key"] == SECRET

    def test_merged_config_values(self) -> None:
        """مقادیر قابل اعمال روی Config (فقط فیلدهای شناخته‌شده)."""
        profile = ApiKeyProfile(
            name="work", api_key=SECRET, base_url="http://gw:8000/v1", model="m1", fallbacks=["m2", "m3"]
        )
        values = profile.merged_config_values()
        assert values["openai_api_key"] == SECRET
        assert values["openai_base_url"] == "http://gw:8000/v1"
        assert values["model_name"] == "m1"
        assert values["model_fallbacks"] == ["m2", "m3"]

    def test_from_dict_roundtrip(self) -> None:
        """serialize/deserialize بی‌ضرر."""
        profile = ApiKeyProfile(name="work", api_key=SECRET, profile="ops")
        clone = ApiKeyProfile.from_dict(profile.to_dict(include_secret=True))
        assert clone.name == "work" and clone.api_key == SECRET and clone.profile == "ops"
        assert "api_key" not in profile.to_dict()


class TestKeyStore:
    """CRUD و ماندگاری."""

    def test_put_creates_default_name(self, tmp_path: Path) -> None:
        """نام خالی → ``default``."""
        store = KeyStore(tmp_path / "keys.json").load()
        assert store.put("", api_key=SECRET).name == "default"

    def test_put_updates_only_given_fields(self, tmp_path: Path) -> None:
        """مقدار ``None`` یعنی «تغییر نده» (کلید حفظ می‌شود)."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("work", api_key=SECRET, model="a")
        record = store.put("work", model="b")
        assert record.api_key == SECRET
        assert record.model == "b"
        assert record.base_url == ""

    def test_first_profile_becomes_active(self, tmp_path: Path) -> None:
        """اولین پروفایل خودکار فعال می‌شود."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("one", api_key=SECRET)
        store.put("two", api_key="sk-other-9999999999")
        assert store.active_name == "one"
        assert store.activate("two") is True
        assert store.active_name == "two"
        assert store.activate("ghost") is False

    def test_rejects_overlong_key(self, tmp_path: Path) -> None:
        """کلید غیرمنطقی (سقف ۵۱۲ کاراکتر) رد می‌شود."""
        store = KeyStore(tmp_path / "keys.json").load()
        with pytest.raises(ValueError, match="too long"):
            store.put("work", api_key="sk-" + "x" * 600)
        assert store.put("work", api_key="sk-" + "y" * 500).api_key.startswith("sk-y")

    def test_file_mode_and_atomicity(self, tmp_path: Path) -> None:
        """فایل ۰۶۰۰ است و فایل موقت مبقا نمی‌ماند."""
        path = tmp_path / "keys.json"
        store = KeyStore(path).load()
        store.put("work", api_key=SECRET)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not list(tmp_path.glob("*.tmp"))

    def test_persistence_roundtrip(self, tmp_path: Path) -> None:
        """خواندن دوباره همان داده‌ها را می‌دهد و active حفظ می‌شود."""
        path = tmp_path / "keys.json"
        store = KeyStore(path).load()
        store.put("work", api_key=SECRET, model="gpt-6-astra", profile="ops")
        store.put("home", api_key="sk-home-1111111111")
        store.activate("home")

        reopened = KeyStore(path).load()
        assert reopened.load_error is None
        assert reopened.names() == ["home", "work"]
        assert reopened.active_name == "home"
        assert reopened.get("work").api_key == SECRET
        assert reopened.active_config_values()["openai_api_key"] == "sk-home-1111111111"

    def test_corrupt_file_is_survivable(self, tmp_path: Path) -> None:
        """فایل خراب → مخزن خالی ولی قابل استفاده (و نوشتن مجدد)."""
        path = tmp_path / "keys.json"
        path.write_text("{not json", encoding="utf-8")
        store = KeyStore(path).load()
        assert store.load_error and "unreadable" in store.load_error
        assert store.names() == []
        store.put("work", api_key=SECRET)
        assert json.loads(path.read_text(encoding="utf-8"))["profiles"]["work"]["api_key"] == SECRET

    def test_signature_detects_tampering(self, tmp_path: Path) -> None:
        """با password، دستکاری مستقیم فایل شناسایی می‌شود."""
        path = tmp_path / "keys.json"
        store = KeyStore(path, password="pin").load()
        store.put("work", api_key=SECRET)

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["profiles"]["work"]["api_key"] = "sk-attacker-0000"
        path.write_text(json.dumps(payload), encoding="utf-8")

        tampered = KeyStore(path, password="pin").load()
        assert tampered.load_error and "signature mismatch" in tampered.load_error
        assert tampered.names() == []
        # بدون password همان فایل (که فقط integrity داشته) خوانده می‌شود
        plain = KeyStore(path).load()
        assert plain.load_error is None and plain.names() == ["work"]

    def test_delete_and_rename(self, tmp_path: Path) -> None:
        """حذف و تغییر نام، از جمله active."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("a", api_key=SECRET)
        store.put("b", api_key="sk-b-2222222222")
        assert store.delete("missing") is False
        assert store.delete("a") is True
        assert store.rename("b", "c") is True
        assert store.active_name == "c"
        assert store.rename("nope", "x") is False
        assert store.delete("c") is True
        assert store.active_name == ""
        assert store.active_config_values() == {}

    def test_clear_forgets_memory_only(self, tmp_path: Path) -> None:
        """``clear`` حافظه را خالی می‌کند و فایل می‌ماند."""
        path = tmp_path / "keys.json"
        store = KeyStore(path).load()
        store.put("work", api_key=SECRET)
        store.clear()
        assert store.names() == []
        assert path.is_file()

    def test_unwritable_path_returns_false(self, tmp_path: Path) -> None:
        """مسیر غیرقابل نوشتن استثنا نمی‌دهد؛ ``save`` False برمی‌گرداند."""
        store = KeyStore(tmp_path / "missing-dir" / "deep" / "keys.json")
        store._profiles["x"] = ApiKeyProfile(name="x", api_key=SECRET)
        # پوشه‌ی والد ایجاد می‌شود، پس موفق است؛ حالت شکست با یک فایل به‌جای پوشه تست می‌شود
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        (blocked / "keys.json").mkdir()
        store2 = KeyStore(blocked / "keys.json")
        store2._profiles["x"] = ApiKeyProfile(name="x", api_key=SECRET)
        assert store.save() is True
        assert store2.save() is False

    def test_status_has_no_secrets(self, tmp_path: Path) -> None:
        """status فقط متادیتاست."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("work", api_key=SECRET)
        status = store.status()
        assert status["count"] == 1 and status["names"] == ["work"] and status["exists"] is True
        assert SECRET not in json.dumps(status)
        assert "all(include_secret=True)" not in json.dumps(status)

    def test_all_masks_by_default(self, tmp_path: Path) -> None:
        """فهرست UI بدون کلید خام است."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("work", api_key=SECRET)
        assert SECRET not in json.dumps(store.all())
        assert SECRET in json.dumps(store.all(include_secret=True))

    def test_repr_is_safe(self, tmp_path: Path) -> None:
        """repr کلید را نشان نمی‌دهد."""
        store = KeyStore(tmp_path / "keys.json").load()
        store.put("work", api_key=SECRET)
        assert SECRET not in repr(store)


class TestRedactText:
    """ماسک‌کردن کلید در متن‌ها."""

    def test_masks_common_prefixes(self) -> None:
        """پیشوندهای رایج (sk-, gsk_, x-api-key…) ماسک می‌شوند."""
        for raw in (
            f"key={SECRET}",
            "Bearer gsk_live-abcdefghijklmnop",
            "OPENAI_API_KEY=sk-proj-abcdefghij123456",
            "ghp_" + "A" * 20,
            "AIza" + "b" * 20,
        ):
            out = redact_text(raw)
            assert "redacted" in out or "…" in out
        assert redact_text("nothing secret here") == "nothing secret here"

    def test_handles_non_string_and_none(self) -> None:
        """ورودی خالی/غیررشته بی‌خطر است."""
        assert redact_text("") == ""
        assert redact_text(None) == ""  # type: ignore[arg-type]
        assert "123" in redact_text(123)

    def test_file_permissions_survive_reload(self, tmp_path: Path) -> None:
        """بعد از load دوباره هم فایل ۰۶۰ می‌ماند."""
        path = tmp_path / "keys.json"
        KeyStore(path).load().put("work", api_key=SECRET)
        KeyStore(path).load().put("home", api_key="sk-h-123456")
        assert oct(stat.S_IMODE(path.stat().st_mode)) == oct(0o600)
        assert os.getpid() > 0

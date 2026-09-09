"""مدیریت پروفایل‌های کلید API روی دستگاه (برای بخش «مدیریت API» در UI).

اینجا چه خبر است و چرا:

* اپ‌ها باید بتوانند چند کلید (OpenAI، OpenRouter، Ollama، Azure gateway …) را
  نگه دارند و بین آن‌ها جابه‌جا شوند؛ اما هیچ‌وقت نباید کلید را به کلاینت
  برگردانیم. بنابراین فقط ``prefix…suffix`` نمایش داده می‌شود.
* فایل در ``~/.universal-agent-hub/keys.json`` با مجوز ``0600`` نوشته می‌شود.
  اگر دیتااستور/OS keychain در دسترس باشد، لایه‌ی بالاتر می‌تواند همان API را
  به آن بدهد (این کلاس به‌تنهایی فایل‌محور و بدون وابستگی است).
* کلیدها هیچ‌وقت لاگ نمی‌شوند؛ :func:`redact_secrets` روی پیام‌های خطا هم اعمال
  می‌شود تا یک request fail شده، کلید را لو ندهد.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from src.utils.helpers import redact_secrets

__all__ = ["ApiKeyProfile", "KeyStore"]

#: حداکثر طول منطقی یک کلید (کلیدهای بلندتر احتمالاً اشتباه paste شده‌اند)
MAX_KEY_LENGTH = 512


class ApiKeyProfile:
    """یک پروفایل کلید: نام، کلید، base_url و مدل."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        fallbacks: list[str] | None = None,
        profile: str = "generalist",
        created_at: float | None = None,
    ) -> None:
        self.name = str(name or "").strip() or "default"
        self.api_key = str(api_key or "")
        self.base_url = str(base_url or "").strip()
        self.model = str(model or "").strip()
        self.fallbacks = [str(item).strip() for item in (fallbacks or []) if str(item).strip()]
        self.profile = str(profile or "generalist").strip()
        self.created_at = float(created_at if created_at is not None else time.time())
        self.updated_at = self.created_at

    # ------------------------------------------------------------------
    # ساخت/تبدیل
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ApiKeyProfile:
        """ساخت از دیکشنری (مقادیر نامعتبر نادیده گرفته می‌شوند)."""
        return cls(
            name=str(payload.get("name") or "default"),
            api_key=str(payload.get("api_key") or ""),
            base_url=str(payload.get("base_url") or ""),
            model=str(payload.get("model") or ""),
            fallbacks=list(payload.get("fallbacks") or []),
            profile=str(payload.get("profile") or "generalist"),
            created_at=payload.get("created_at"),
        )

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        """نسخه‌ی JSON؛ پیش‌فرض **بدون** خودِ کلید."""
        data: dict[str, Any] = {
            "name": self.name,
            "masked_key": self.masked_key,
            "has_key": bool(self.api_key),
            "base_url": self.base_url,
            "model": self.model,
            "fallbacks": list(self.fallbacks),
            "profile": self.profile,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_secret:
            data["api_key"] = self.api_key
        return data

    # ------------------------------------------------------------------
    # رفتار
    # ------------------------------------------------------------------
    @property
    def masked_key(self) -> str:
        """نمایش امن کلید (``sk-ab…wxyz``) — برای UI."""
        key = self.api_key
        if not key:
            return ""
        if len(key) <= 12:
            return "***"
        return f"{key[:6]}…{key[-4:]}"

    @property
    def is_complete(self) -> bool:
        """آیا برای صدا زدن API کافی است؟"""
        return bool(self.api_key)

    def merged_config_values(self) -> dict[str, Any]:
        """مقادیری که روی :class:`src.config.Config` اعمال می‌شوند (فیلدهای پر شده)."""
        values: dict[str, Any] = {}
        if self.api_key:
            values["openai_api_key"] = self.api_key
        if self.base_url:
            values["openai_base_url"] = self.base_url
        if self.model:
            values["model_name"] = self.model
            values["active_model"] = self.model
        if self.fallbacks:
            values["model_fallbacks"] = list(self.fallbacks)
        return values

    def __repr__(self) -> str:
        """نمایش دیباگ (کلید هرگز چاپ نمی‌شود)."""
        return f"<ApiKeyProfile {self.name!r} key={self.masked_key or '(none)'} model={self.model or '-'}>"


class KeyStore:
    """مخزن فایل‌محور پروفایل‌های کلید (active profile + فهرست).

    Args:
        path: مسیر فایل JSON.
        password: در صورت دادن، یک HMAC روی محتوا امضا می‌کند تا دستکاری فایل
            قابل تشخیص باشد (این «رمزنگاری» نیست؛ فقط integrity است).
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str, *, password: str = "") -> None:
        self.path = Path(path).expanduser()
        self._password = str(password or "")
        self._profiles: dict[str, ApiKeyProfile] = {}
        self._active: str = ""
        self.loaded = False
        self.load_error: str | None = None

    # ------------------------------------------------------------------
    # بارگذاری/ذخیره
    # ------------------------------------------------------------------
    def load(self) -> KeyStore:
        """خواندن فایل؛ فایل نباشد یا خراب باشد، مخزن خالی و قابل استفاده می‌ماند."""
        self._profiles, self._active = {}, ""
        self.load_error = None
        self.loaded = True
        if not self.path.is_file():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.load_error = f"keystore unreadable: {type(exc).__name__}"
            return self
        if isinstance(raw, dict) and self._password:
            expected = self._signature(raw.get("profiles") or {})
            if not secrets.compare_digest(str(raw.get("signature") or ""), expected):
                self.load_error = "keystore signature mismatch (file was modified outside the app)"
                return self
        payload = raw.get("profiles") if isinstance(raw, dict) else None
        if isinstance(payload, dict):
            for name, item in payload.items():
                if isinstance(item, dict):
                    profile = ApiKeyProfile.from_dict({**item, "name": item.get("name") or name})
                    self._profiles[profile.name] = profile
        if isinstance(raw, dict):
            self._active = str(raw.get("active") or "")
        if self._active not in self._profiles:
            self._active = next(iter(self._profiles), "")
        return self

    def save(self) -> bool:
        """نوشتن اتمی فایل با مجوز ۰۶۰۰. نتیجه‌ی موفقیت برمی‌گردد (خطا throw نمی‌شود)."""
        payload: dict[str, Any] = {
            "version": self.SCHEMA_VERSION,
            "active": self._active,
            "profiles": {name: profile.to_dict(include_secret=True) for name, profile in self._profiles.items()},
            "updated_at": time.time(),
        }
        if self._password:
            payload["signature"] = self._signature(payload["profiles"])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                temp.chmod(0o600)
            temp.replace(self.path)
            return True
        except OSError:
            return False

    def _signature(self, profiles: dict[str, Any]) -> str:
        """امضای HMAC-SHA256 برای تشخیص دستکاری فایل (کلید از password می‌آید)."""
        import hashlib
        import hmac

        body = json.dumps(profiles, sort_keys=True, ensure_ascii=False)
        return hmac.new(self._password.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def names(self) -> list[str]:
        """نام پروفایل‌ها (مرتب)."""
        return sorted(self._profiles)

    def all(self, *, include_secret: bool = False) -> list[dict[str, Any]]:
        """فهرست پروفایل‌ها برای UI (پیش‌فرض بدون کلید)."""
        return [profile.to_dict(include_secret=include_secret) for profile in self._profiles.values()]

    def get(self, name: str) -> ApiKeyProfile | None:
        """دریافت یک پروفایل."""
        return self._profiles.get(str(name or "").strip())

    def put(
        self,
        name: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        fallbacks: list[str] | None = None,
        profile: str | None = None,
        persist: bool = True,
    ) -> ApiKeyProfile:
        """ساخت یا به‌روزرسانی یک پروفایل (مقدار ``None`` یعنی «تغییر نده»)."""
        key = str(name or "").strip() or "default"
        existing = self._profiles.get(key)
        record = existing or ApiKeyProfile(name=key)
        if api_key is not None:
            candidate = str(api_key).strip()
            if len(candidate) > MAX_KEY_LENGTH:
                raise ValueError(f"api key is too long ({len(candidate)} chars; max {MAX_KEY_LENGTH})")
            record.api_key = candidate
        if base_url is not None:
            record.base_url = str(base_url).strip()
        if model is not None:
            record.model = str(model).strip()
        if fallbacks is not None:
            record.fallbacks = [str(item).strip() for item in fallbacks if str(item).strip()]
        if profile is not None:
            record.profile = str(profile).strip() or "generalist"
        record.updated_at = time.time()
        self._profiles[key] = record
        if not self._active:
            self._active = key
        if persist:
            self.save()
        return record

    def delete(self, name: str, *, persist: bool = True) -> bool:
        """حذف پروفایل. اگر آخرین پروفایل باشد، active خالی می‌شود."""
        key = str(name or "").strip()
        if key not in self._profiles:
            return False
        del self._profiles[key]
        if self._active == key:
            self._active = next(iter(self._profiles), "")
        if persist:
            self.save()
        return True

    def rename(self, old: str, new: str, *, persist: bool = True) -> bool:
        """تغییر نام پروفایل (کلید و بقیه‌ی مقادیر حفظ می‌شوند)."""
        source, target = str(old or "").strip(), str(new or "").strip()
        if not target or source not in self._profiles or target in self._profiles:
            return False
        record = self._profiles.pop(source)
        record.name = target
        self._profiles[target] = record
        if self._active == source:
            self._active = target
        if persist:
            self.save()
        return True

    # ------------------------------------------------------------------
    # پروفایل فعال
    # ------------------------------------------------------------------
    @property
    def active_name(self) -> str:
        """نام پروفایل فعال (خالی یعنی هیچ)."""
        return self._active

    def activate(self, name: str, *, persist: bool = True) -> bool:
        """انتخاب پروفایل فعال. ``True`` اگر وجود داشت."""
        key = str(name or "").strip()
        if key not in self._profiles:
            return False
        self._active = key
        if persist:
            self.save()
        return True

    @property
    def active(self) -> ApiKeyProfile | None:
        """پروفایل فعال (یا None)."""
        return self._profiles.get(self._active)

    def active_config_values(self) -> dict[str, Any]:
        """مقادیر config پروفایل فعال (برای overlay روی تنظیمات سرور)."""
        record = self.active
        return record.merged_config_values() if record else {}

    # ------------------------------------------------------------------
    # وضعیت
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """خلاصه‌ی وضعیت برای UI (بدون هیچ راز)."""
        return {
            "path": str(self.path),
            "exists": self.path.is_file(),
            "count": len(self._profiles),
            "names": self.names(),
            "active": self._active or None,
            "writable": self.path.parent.is_dir() and os.access(self.path.parent, os.W_OK),
            "integrity_protected": bool(self._password),
            "load_error": getattr(self, "load_error", None),
        }

    def clear(self) -> None:
        """فراموش کردن همه‌چیز در حافظه (فایل را پاک نمی‌کند)."""
        self._profiles, self._active = {}, ""

    def __repr__(self) -> str:
        """نمایش دیباگ."""
        return f"<KeyStore {self.path} profiles={len(self._profiles)} active={self._active or '-'}>"


def redact_text(text: str) -> str:
    """ماسک‌کردن کلیدها در پیام‌های خطایی که به اپ می‌روند."""
    return redact_secrets(str(text or ""))

"""Allowlist خروجی HTTP: فقط میزبان/مسیرهای تصریح‌شده.

چرا، وقتی ``SafetyGuard.assess_network`` داریم؟
-----------------------------------------------
``assess_network`` *ریسک* را می‌سنجد (IP خصوصی، metadata ابری، دامنه‌ی مشکوک).
این ماژول یک سؤال متفاوت می‌پرسد: «آیا این مقصد اصلاً مجاز است؟» — یعنی سیاست
fail-closed به‌جای risk-scoring. برای ایجنتی که خودِ کاربر برایش مقصد تعیین
می‌کند (روتین‌ها، webhookها، MCP over HTTP)، این لایه لازم است.

قواعد:
* لیست خالی ⇒ **همه چیز رد** (fail closed). این عمدی است.
* ``*`` به‌تنهایی ⇒ همه‌چیز مجاز (صریح، نه پیش‌فرض).
* ``*.example.com`` ⇒ خودِ دامنه + همه‌ی زیردامنه‌ها.
* ``example.com/api/*`` ⇒ فقط آن پیشوند مسیر.
* ``https://example.com`` ⇒ علاوه بر میزبان، schema را هم قفل می‌کند.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

__all__ = ["AllowDecision", "EndpointAllowlist", "Rule"]


@dataclass(frozen=True)
class AllowDecision:
    """نتیجه‌ی بررسی یک URL."""

    allowed: bool
    reason: str
    matched_rule: str = ""

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return {"allowed": self.allowed, "reason": self.reason, "matched_rule": self.matched_rule}


@dataclass(frozen=True)
class Rule:
    """یک قاعده‌ی پارس‌شده."""

    raw: str
    scheme: str
    host: str
    path: str

    def matches_host(self, host: str) -> bool:
        """تطبیق میزبان (با پشتیبانی از wildcard چپ)."""
        if self.host == "*":
            return True
        if self.host.startswith("*."):
            suffix = self.host[1:]  # ".example.com"
            return host == self.host[2:] or host.endswith(suffix)
        return host == self.host


def _parse_rule(raw: str) -> Rule | None:
    """یک رشته‌ی قاعده را به :class:`Rule` تبدیل می‌کند."""
    text = str(raw or "").strip()
    if not text or text.startswith("#"):
        return None
    scheme = ""
    rest = text
    if "://" in text:
        head, _, tail = text.partition("://")
        scheme = head.lower().strip()
        rest = tail
    host_part, slash, path_part = rest.partition("/")
    host = host_part.strip().lower()
    if not host:
        return None
    # پورت بخشی از میزبان است؛ جدا نگهش می‌داریم تا تطبیق ساده بماند.
    path = ("/" + path_part) if slash else ""
    if path.endswith("*"):
        path = path.rstrip("*")
    return Rule(raw=text, scheme=scheme, host=host, path=path)


class EndpointAllowlist:
    """فهرست مجاز مقاصد HTTP.

    نمونه::

        allow = EndpointAllowlist(["api.github.com", "*.openai.com/v1/*"])
        allow.check("https://api.github.com/user")   # allowed
        allow.check("https://evil.test/")            # denied
    """

    def __init__(self, rules: list[str] | tuple[str, ...] | None = None) -> None:
        """Args:
        rules: فهرست رشته‌ای قواعد؛ ``None``/خالی یعنی «هیچ‌چیز مجاز نیست».
        """
        self._rules: list[Rule] = []
        self._raw: list[str] = []
        self._allow_all = False
        for item in rules or []:
            parsed = _parse_rule(item)
            if parsed is None:
                continue
            self._rules.append(parsed)
            self._raw.append(parsed.raw)
            if parsed.raw.strip() == "*" and not parsed.scheme:
                self._allow_all = True

    @classmethod
    def from_comma_string(cls, text: str) -> EndpointAllowlist:
        """ساخت از یک رشته‌ی جدا‌شده با کاما (برای ``.env``)."""
        return cls([part for part in str(text or "").split(",") if part.strip()])

    @property
    def rules(self) -> list[str]:
        """قواعد خام."""
        return list(self._raw)

    @property
    def allow_all(self) -> bool:
        """آیا ``*`` صریحاً داده شده؟"""
        return self._allow_all

    def __len__(self) -> int:
        """تعداد قواعد."""
        return len(self._rules)

    def add(self, rule: str) -> bool:
        """یک قاعده اضافه می‌کند.

        Returns:
            ``True`` اگر پذیرفته شد.
        """
        parsed = _parse_rule(rule)
        if parsed is None:
            return False
        if parsed.raw in self._raw:
            return False
        self._rules.append(parsed)
        self._raw.append(parsed.raw)
        if parsed.raw.strip() == "*" and not parsed.scheme:
            self._allow_all = True
        return True

    def check(self, url: str) -> AllowDecision:
        """یک URL را بررسی می‌کند.

        Args:
            url: مقصد.

        Returns:
            :class:`AllowDecision`.
        """
        text = str(url or "").strip()
        if not text:
            return AllowDecision(False, "empty url")
        if not self._rules:
            return AllowDecision(False, "allowlist is empty (fail closed)")
        if self._allow_all:
            return AllowDecision(True, "wildcard * rule", "*")
        try:
            parsed = urlparse(text if "://" in text else f"https://{text}")
        except ValueError as exc:
            return AllowDecision(False, f"unparsable url: {exc}")
        host = (parsed.hostname or "").lower()
        if not host:
            return AllowDecision(False, "no host in url")
        scheme = (parsed.scheme or "https").lower()
        path = parsed.path or "/"
        for rule in self._rules:
            if rule.scheme and rule.scheme != scheme:
                continue
            if not rule.matches_host(host):
                continue
            if rule.path:
                if rule.path.endswith("/"):
                    if not (path == rule.path[:-1] or path.startswith(rule.path)):
                        continue
                elif not (path == rule.path or path.startswith(rule.path.rstrip("/") + "/") or path == rule.path):
                    continue
            return AllowDecision(True, f"matched {rule.raw}", rule.raw)
        return AllowDecision(False, f"host '{host}' is not in the allowlist")

    def filter(self, urls: list[str]) -> dict[str, list[str]]:
        """دسته‌بندی یک فهرست URL به مجاز/رد.

        Returns:
            ``{"allowed": [...], "denied": [...]}``.
        """
        allowed: list[str] = []
        denied: list[str] = []
        for item in urls:
            (allowed if self.check(item).allowed else denied).append(item)
        return {"allowed": allowed, "denied": denied}

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/safety``."""
        return {
            "rules": list(self._raw),
            "count": len(self._rules),
            "allow_all": self._allow_all,
            "policy": (
                "allow-all (explicit *)"
                if self._allow_all
                else ("deny-all (empty)" if not self._rules else "allowlisted only")
            ),
        }


def matches_glob(value: str, patterns: list[str]) -> bool:
    """تطبیق glob ساده (برای مسیرهای محلی، نه URL)."""
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)

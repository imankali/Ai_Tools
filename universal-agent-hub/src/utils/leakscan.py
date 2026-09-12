"""تشخیص نشت secret در محتوای *خروجی*.

فرقش با :func:`src.utils.helpers.redact_secrets`
----------------------------------------------
``redact_secrets`` الگوهای *عمومی* (``sk-…``، ``ghp_…``) را می‌پوشاند. این
ماژول یک قدم جلوتر می‌رود و **مقادیر واقعی** را می‌شناسد: کلیدهایی که در
``.env``، محیط اجرا یا keystore خودِ کاربر هستند. اگر ایجنت بخواهد همان مقدار
را در یک پاسخ، یک webhook یا یک درخواست HTTP بیرون بفرستد، اینجا گیر می‌افتد.

این دقیقاً همان «Leak detection» است که IronClaw به‌عنوان لایه‌ی
anti-exfiltration تعریف می‌کند: *محتوا را در مرز خروجی اسکن کن*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["LeakDetector", "LeakFinding", "LeakReport"]

#: پیشوندهای شناخته‌شده‌ی توکن‌ها (عمومی).
_GENERIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("openai_project_key", re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}\b")),
    ("bearer_header", re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)\s+[A-Za-z0-9._\-+/=]{12,}")),
    ("dsn_with_password", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]{1,64}:[^/\s:@]{3,64}@[^\s/]{1,128}")),
)

#: نام‌هایی که در .env معمولاً secret هستند.
_SENSITIVE_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "DSN", "PRIVATE")

#: کوتاه‌ترین مقداری که ارزش «secret واقعی» شدن دارد.
_MIN_KNOWN_LEN = 8


@dataclass(frozen=True)
class LeakFinding:
    """یک نشت شناسایی‌شده."""

    kind: str
    source: str
    masked: str
    start: int
    end: int

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize (خودِ مقدار هرگز بیرون نمی‌رود)."""
        return {"kind": self.kind, "source": self.source, "masked": self.masked, "start": self.start, "end": self.end}


@dataclass
class LeakReport:
    """نتیجه‌ی اسکن."""

    scanned_chars: int = 0
    findings: list[LeakFinding] = field(default_factory=list)
    sanitized: str = ""

    @property
    def clean(self) -> bool:
        """آیا نشتی پیدا نشد؟"""
        return not self.findings

    @property
    def kinds(self) -> list[str]:
        """نوع‌های یکتای پیدا‌شده."""
        return sorted({f.kind for f in self.findings})

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return {
            "clean": self.clean,
            "scanned_chars": self.scanned_chars,
            "count": len(self.findings),
            "kinds": self.kinds,
            "findings": [f.as_dict() for f in self.findings],
        }


def mask(value: str, *, keep: int = 4) -> str:
    """پوشاندن مقدار؛ فقط چند کاراکتر اول/آخر می‌ماند تا قابل ردیابی باشد."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 8}{value[-keep:]}"


class LeakDetector:
    """اسکنر نشت secret در متن خروجی.

    نمونه::

        detector = LeakDetector.from_env()
        report = detector.scan(outbound_text)
        if not report.clean:
            outbound_text = report.sanitized
    """

    def __init__(self, known: dict[str, str] | None = None, *, include_generic: bool = True) -> None:
        """Args:
        known: نگاشت ``source -> secret`` (مثلاً ``{"env:OPENAI_API_KEY": "sk-…"}``).
        include_generic: الگوهای عمومی هم فعال باشند؟
        """
        self._known: dict[str, str] = {}
        self._include_generic = include_generic
        if known:
            self.add_known(known)

    # ------------------------------------------------------------------ builders
    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, *, include_generic: bool = True) -> LeakDetector:
        """از متغیرهای محیط، فقط آن‌هایی که نامشان بوی secret می‌دهد.

        Args:
            environ: دیکشنری محیط (پیش‌فرض ``os.environ``).
            include_generic: الگوهای عمومی فعال باشند؟
        """
        import os

        env = dict(os.environ if environ is None else environ)
        known = {
            f"env:{name}": value
            for name, value in env.items()
            if value and len(value) >= _MIN_KNOWN_LEN and any(hint in name.upper() for hint in _SENSITIVE_ENV_HINTS)
        }
        return cls(known, include_generic=include_generic)

    # ------------------------------------------------------------------ mutation
    def add_known(self, mapping: dict[str, str]) -> int:
        """مقادیر شناخته‌شده‌ی تازه اضافه می‌کند.

        Returns:
            تعداد مقادیری که واقعاً پذیرفته شدند (خیلی کوتاه‌ها رد می‌شوند).
        """
        added = 0
        for source, value in mapping.items():
            text = str(value or "").strip()
            if len(text) < _MIN_KNOWN_LEN:
                continue
            self._known[str(source)] = text
            added += 1
        return added

    def forget(self, source: str) -> bool:
        """یک منبع شناخته‌شده را حذف می‌کند."""
        return self._known.pop(source, None) is not None

    @property
    def sources(self) -> list[str]:
        """نام منابع شناخته‌شده (نه خودِ مقادیر)."""
        return sorted(self._known)

    # ------------------------------------------------------------------ scanning
    def scan(self, text: str, *, max_findings: int = 50) -> LeakReport:
        """متن را اسکن می‌کند و نسخه‌ی پوشانده‌شده را هم می‌سازد.

        Args:
            text: متن خروجی.
            max_findings: سقف یافته‌ها.

        Returns:
            :class:`LeakReport` با ``sanitized`` آماده‌ی ارسال.
        """
        raw = str(text or "")
        report = LeakReport(scanned_chars=len(raw), sanitized=raw)
        if not raw:
            return report

        spans: list[tuple[int, int, str, str]] = []

        if self._include_generic:
            for kind, pattern in _GENERIC_PATTERNS:
                for match in pattern.finditer(raw):
                    spans.append((match.start(), match.end(), kind, "pattern"))

        # مقادیر واقعیِ شناخته‌شده: بلندترها اول، تا mask درست باشد.
        for source, value in sorted(self._known.items(), key=lambda kv: -len(kv[1])):
            start = raw.find(value)
            while start != -1:
                kind = "known-secret"
                spans.append((start, start + len(value), kind, source))
                start = raw.find(value, start + len(value))

        if not spans:
            return report

        # مرتب‌سازی بر اساس شروع، و در هم‌پوشانی: بلندترها و سپس
        # «known-secret» اول. دلیل: یک secret *شناخته‌شده* (که source دارد)
        # از یک تطبیق الگوی عمومی قابل‌اقدام‌تر است، پس در گره باید آن ببرد.
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0]), 0 if s[3] != "pattern" else 1))
        cursor = 0
        for start, end, kind, source in spans:
            if len(report.findings) >= max(1, max_findings):
                break
            if start < cursor:
                continue
            report.findings.append(
                LeakFinding(kind=kind, source=source, masked=mask(raw[start:end]), start=start, end=end)
            )
            cursor = end

        rebuilt: list[str] = []
        cursor = 0
        for finding in report.findings:
            rebuilt.append(raw[cursor : finding.start])
            rebuilt.append(f"[REDACTED:{finding.kind}]")
            cursor = finding.end
        rebuilt.append(raw[cursor:])
        report.sanitized = "".join(rebuilt)
        return report

    def contains_secret(self, text: str) -> bool:
        """بررسی سریع bool."""
        return not self.scan(text).clean

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/safety`` (بدون افشای مقدار)."""
        return {
            "known_sources": self.sources,
            "known_count": len(self._known),
            "generic_patterns": [name for name, _ in _GENERIC_PATTERNS] if self._include_generic else [],
        }

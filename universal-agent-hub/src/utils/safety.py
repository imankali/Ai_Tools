"""بررسی ایمنی عملیات (Safety guard).

این ماژول «تصمیم‌گیر ایمنی» پروژه است و هیچ عملیاتی بدون عبور از آن اجرا
نمی‌شود. سه وظیفه دارد:

1. **تشخیص دستورهای خطرناک** در ترمینال (حذف ریشه، فرمت دیسک، نوشتن روی
   دستگاه، fork bomb، ``curl | sh`` و…).
2. **محدودسازی مسیر فایل‌ها** به دایرکتوری‌های مجاز و جلوگیری از خواندن
   کلیدها و فایل‌های حساس (``~/.ssh``, ``.env``, …).
3. **سطح‌بندی ریسک** تا ایجنت بداند چه کاری نیاز به تأیید کاربر دارد.

سیاست رفتاری از تنظیمات می‌آید:

* ``DANGEROUS_COMMAND_POLICY=confirm`` → اجرا فقط پس از تأیید کاربر.
* ``DANGEROUS_COMMAND_POLICY=deny``    → دستور پرخطر به‌کل اجرا نمی‌شود.
"""

from __future__ import annotations

import contextlib
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from src.models.tool_models import RiskLevel
from src.utils.validators import ValidationError, is_within

__all__ = ["CommandToken", "SafetyDecision", "SafetyGuard", "ShellFeature"]

#: دایرکتوری‌های سیستمی که نوشتن/حذف در آن‌ها هرگز مجاز نیست
PROTECTED_SYSTEM_DIRS: tuple[str, ...] = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
)

#: الگوهای مسیر حساس (خواندن/نوشتن آن‌ها مسدود است)
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    "*.ssh/*",
    "*ssh/id_rsa*",
    "*.gnupg/*",
    "*.aws/credentials",
    "*.azure/*",
    "*.config/gcloud/*",
    "*.docker/config.json",
    "*.netrc",
    "*.npmrc",
    ".netrc",
    ".npmrc",
    "*.env",
    "*.env.*",
    ".env",
    ".env.*",
    "*/.git/config",
    "*/.git-credentials",
    "*/.kube/config",
    "*/etc/shadow",
    "*/etc/sudoers",
    "*id_ed25519",
    "*/.bash_history",
    "*/.python_history",
    "*/.config/gh/hosts.yml",
)

#: الگوهای «نام فایل» حساس که در آرگومان هر دستور shell هم رد می‌شوند
SENSITIVE_FILE_NAMES: tuple[str, ...] = (
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "authorized_keys",
    ".netrc",
    ".npmrc",
    ".git-credentials",
    "shadow",
    "sudoers",
    "credentials",
    ".env",
    "hosts.yml",
)


def _is_sensitive_name(token: str) -> bool:
    """آیا یک توکن دستور به فایل حساسی اشاره می‌کند؟ (مثلاً ``~/.ssh/id_rsa``)"""
    lowered = token.lower().rstrip(chr(92) + chr(34))
    if not lowered or lowered.startswith("-"):
        return False
    name = lowered.rsplit("/", 1)[-1]
    if name in SENSITIVE_FILE_NAMES or name.startswith(".env."):
        return True
    return any(
        marker in lowered for marker in ("/.ssh/", "/.gnupg/", "/.aws/", "/.kube/", "/.docker/", "/etc/shadow")
    )


class ShellFeature(str, Enum):
    """ویژگی‌های shell که ریسک تزریق را بالا می‌برند."""

    PIPE = "|"
    CHAIN = "&&"
    SEMICOLON = ";"
    REDIRECT = ">"
    SUBSHELL = "$()"
    BACKTICK = "`"
    EXPANSION = "${"


@dataclass(frozen=True)
class CommandToken:
    """تجزیه‌ی ساده‌ی یک دستور برای بررسی‌های تکمیلی.

    Attributes:
        program: نام برنامه (توکن اول).
        args: آرگومان‌ها.
        pipes: فهرست دستورات جدا‌شده با ``|``.
        redirects: مسیرهای مقصد redirect.
    """

    program: str
    args: tuple[str, ...] = ()
    pipes: tuple[str, ...] = ()
    redirects: tuple[str, ...] = ()
    raw: str = ""

    @property
    def lower_text(self) -> str:
        """نسخه‌ی lowercase کل دستور (برای تطبیق الگو)."""
        return self.raw.lower()


@dataclass
class SafetyDecision:
    """نتیجه‌ی بررسی ایمنی یک عملیات.

    Attributes:
        allowed: آیا اجازه‌ی اجرا وجود دارد.
        requires_confirmation: آیا پیش از اجرا باید کاربر تأیید کند.
        risk: سطح ریسک تشخیص داده‌شده.
        reasons: دلایل قابل نمایش برای کاربر.
        metadata: اطلاعات تکمیلی (مثل ویژگی‌های shell شناسایی‌شده).
    """

    allowed: bool = True
    requires_confirmation: bool = False
    risk: RiskLevel = RiskLevel.LOW
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        """آیا عملیات به‌طور کامل مسدود شده است؟"""
        return not self.allowed

    @property
    def reason_text(self) -> str:
        """ترکیب دلایل به‌صورت یک رشته."""
        return "; ".join(self.reasons) if self.reasons else "no specific risk detected"

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize برای لاگ و metadata ابزار."""
        return {
            "allowed": self.allowed,
            "requires_confirmation": self.requires_confirmation,
            "risk": self.risk.value,
            "reasons": list(self.reasons),
        }


def _merge_risk(current: RiskLevel, new: RiskLevel) -> RiskLevel:
    """بزرگ‌ترین سطح ریسک بین دو مقدار."""
    order = [
        RiskLevel.SAFE,
        RiskLevel.LOW,
        RiskLevel.MEDIUM,
        RiskLevel.HIGH,
        RiskLevel.CRITICAL,
    ]
    return new if order.index(new) > order.index(current) else current


class SafetyGuard:
    """نگهبان ایمنی: بررسی دستورهای shell و مسیرهای فایل.

    Args:
        policy: ``confirm`` یا ``deny`` برای دستورهای پرخطر.
        allowed_directories: دایرکتوری‌های مجاز (خالی = فقط دایرکتوری پروژه).
        unrestricted_filesystem: اگر True باشد محدودیت مسیر برداشته می‌شود.
        blocked_commands: الگوهای regex اضافی برای مسدودسازی.
        confirm_commands: الگوهای regex که نیازمند تأییدند.
        read_only_paths: الگوهای glob مسیرهای فقط‌خواندنی.
        blocked_paths: الگوهای glob مسیرهای ممنوع.
    """

    #: دستوراتی که می‌توانند کل سیستم را از بین ببرند
    CRITICAL_PATTERNS: tuple[tuple[str, str], ...] = (
        (r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+/?\s*$", "recursive force delete of the filesystem root"),
        (r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+(\/|/\*|~|\$HOME)(\s|$)", "recursive force delete of a root/home path"),
        (
            r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+(/(bin|boot|dev|etc|lib|proc|root|run|sbin|sys|usr|var))",
            "recursive force delete of a system directory",
        ),
        (r"\bmkfs(\.\w+)?\b", "filesystem formatting command"),
        (r"\bdd\b[^|]*\bof=/dev/", "raw write to a block device via dd"),
        (r">\s*/dev/(sd[a-z]|nvme\d+n\d+|disk\d+|hd[a-z])", "redirecting output onto a block device"),
        (r"\b(shutdown|reboot|halt|poweroff)\b", "power-state change"),
        (r"\bchown\b\s+(-\S+\s+)*\S+\s+/(?:\s|$)", "changing ownership of the filesystem root"),
        (
            r"\bchmod\b\s+(-\S+\s+)*(0777|777|a\+rwx|\+w)\s+/(?:\s|\*|$)",
            "world-writable permissions on the filesystem root",
        ),
        (r":\(\)\s*\{.*\};\s*:", "fork bomb"),
        (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|da)?sh\b", "piping a remote download directly into a shell"),
        (r"\bsudo\s+(-[a-zA-Z]+\s+)*rm\b", "privileged file removal"),
        (r"\bhistory\s+-c\b", "deleting shell history (anti-forensics)"),
        (r"\bioreg\s+.*-w\s+yes\b", "low-level device write"),
        (r"\bsetenforce\s+0\b", "disabling SELinux enforcement"),
        (r"\b iptables\s+-F\b", "flushing the firewall ruleset"),
        (r"\buserdel\b.*-(r|R)\b", "removing user accounts with home data"),
        (r"\bcrontab\s+-r\b", "deleting all cron jobs"),
        (r"\bgit\s+--git-dir=.*\s+(reset\s+--hard|clean\s+-[fd])", "destructive git cleanup in another repository"),
        (r"\bfind\b.*-delete\b", "find with -delete (mass deletion)"),
        (r"\bgit\s+push\b.*(--force|-f)\b.*\s(main|master)\b", "force-push to a protected branch"),
    )

    #: دستوراتی که نیازمند تأییدند ولی لزوماً فاجعه‌بار نیستند
    CONFIRM_PATTERNS: tuple[tuple[str, str], ...] = (
        (r"\bsudo\b", "requires elevated privileges"),
        (r"\bsu\s+-?\w*", "switching user"),
        (r"\b(chmod|chown)\b", "changing permissions or ownership"),
        (r"\bkill(all)?\b", "terminating processes"),
        (r"\bpkill\b", "killing processes by name"),
        (r"\bkill\b", "terminating a process"),
        (r"\bgit\s+(reset|clean|rebase|filter-branch)\b", "history-rewriting git operation"),
        (r"\bgit\s+push\b", "writing to a remote repository"),
        (
            r"\bdocker\b\s+(system\s+prune|volume\s+rm|container\s+rm|image\s+rm)",
            "docker cleanup that can delete data",
        ),
        (r"\bapt(-get)?\s+(remove|purge|autoremove)\b", "removing system packages"),
        (r"\b(brew|dnf|yum|pacman|zypper)\s+(uninstall|remove|clean)\b", "removing packages"),
        (r"\bpip3?\s+uninstall\b", "removing Python packages"),
        (r"\bnpm\s+(uninstall|link|publish)\b", "mutating global/local node packages"),
        (r"\b(curl|wget)\b", "network download"),
        (r"\bnmap\b|\bmasscan\b", "network scanning"),
        (r"\b(base64|xxd|hexdump)\b.*\|\s*(bash|sh)\b", "executing encoded payload"),
        (r">\s*/(etc|usr|var|opt|home)/", "redirecting output into a system directory"),
        (r"\btee\b\s+/(etc|usr|var|opt|boot)/", "writing into a system directory via tee"),
        (r"\btruncate\b", "truncating file contents"),
        (r"\brmdir\b|\bunlink\b", "directory/entry removal"),
        (r"\bcp\b.*-r.*/(etc|usr|bin|boot|sys|proc)", "recursive copy onto a system directory"),
        (r"\bmv\b.*/(bin|boot|dev|etc|lib|sbin|usr)/", "moving files into a system directory"),
        (r"\bsed\s+-i\b", "in-place file editing"),
        (r"\bxargs\b.*(rm|delete)", "mass deletion through xargs"),
    )

    def __init__(
        self,
        *,
        policy: str = "confirm",
        allowed_directories: Sequence[str | Path] | None = None,
        unrestricted_filesystem: bool = False,
        blocked_commands: Iterable[str] | None = None,
        confirm_commands: Iterable[str] | None = None,
        read_only_paths: Iterable[str] | None = None,
        blocked_paths: Iterable[str] | None = None,
        project_root: str | Path | None = None,
        allow_shell: bool = True,
        enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.allow_shell = bool(allow_shell)
        self.policy = "deny" if str(policy).lower() == "deny" else "confirm"
        self.unrestricted_filesystem = bool(unrestricted_filesystem)
        self.project_root = Path(project_root or Path.cwd()).resolve(strict=False)
        self.allowed_directories = self._normalize_dirs(allowed_directories)
        self.read_only_patterns = [glob for glob in (read_only_paths or []) if glob]
        self.blocked_path_patterns = [glob for glob in (blocked_paths or []) if glob]
        self._extra_blocklist = self._compile(blocked_commands, "custom blocked command")
        self._extra_confirm = self._compile(confirm_commands, "custom confirmation rule")

    # ------------------------------------------------------------------
    # ساخت/نرمال‌سازی
    # ------------------------------------------------------------------
    def _normalize_dirs(self, dirs: Sequence[str | Path] | None) -> list[Path]:
        """نرمال‌سازی دایرکتوری‌های مجاز (همیشه حداقل دایرکتوری پروژه)."""
        result: list[Path] = []
        for entry in dirs or []:
            path = Path(str(entry)).expanduser().resolve(strict=False)
            if path not in result:
                result.append(path)
        if not result and not self.unrestricted_filesystem:
            result.append(self.project_root)
        return result

    @staticmethod
    def _compile(patterns: Iterable[str] | None, label: str) -> list[tuple[re.Pattern[str], str]]:
        """ساخت regex‌های معتبر از رشته‌های کاربر (الگوی خراب نادیده گرفته می‌شود)."""
        compiled: list[tuple[re.Pattern[str], str]] = []
        for pattern in patterns or []:
            try:
                compiled.append((re.compile(pattern, re.IGNORECASE), label))
            except re.error:
                continue
        return compiled

    @classmethod
    def from_config(cls, config: Any) -> SafetyGuard:
        """ساخت guard از روی نمونه‌ی :class:`src.config.Config`."""
        safety = getattr(config, "safety", None)
        return cls(
            policy=getattr(config, "dangerous_command_policy", "confirm"),
            allowed_directories=getattr(config, "allowed_directories", None),
            unrestricted_filesystem=getattr(config, "unrestricted_filesystem", False),
            read_only_paths=getattr(safety, "read_only_paths", None),
            blocked_paths=getattr(safety, "blocked_paths", None),
            blocked_commands=getattr(safety, "blocked_commands", None),
            confirm_commands=getattr(safety, "confirm_commands", None),
            project_root=getattr(config, "project_root", Path.cwd()),
            allow_shell=getattr(config, "allow_shell", True),
            enabled=getattr(config, "enable_safety_guard", True),
        )

    # ------------------------------------------------------------------
    # بررسی دستور shell
    # ------------------------------------------------------------------
    def parse_command(self, command: str) -> CommandToken:
        """تجزیه‌ی اولیه‌ی دستور به :class:`CommandToken` (بدون اجرا)."""
        raw = str(command or "").strip()
        segments = [segment.strip() for segment in re.split(r"\s*(?:\|\||&&|;|\|)\s*", raw) if segment.strip()]
        program = ""
        args: tuple[str, ...] = ()
        if segments:
            try:
                tokens = shlex.split(segments[0], posix=True)
            except ValueError:
                tokens = segments[0].split()
            program = Path(tokens[0]).name if tokens else ""
            args = tuple(tokens[1:]) if len(tokens) > 1 else ()
        redirects = tuple(re.findall(r"(?:(?:\d*)>{1,2})\s*([^\s|;&]+)", raw))
        return CommandToken(
            program=program,
            args=args,
            pipes=tuple(segments[1:]),
            redirects=redirects,
            raw=raw,
        )

    def _assess_command(self, command: str) -> SafetyDecision:
        """ارزیابی ایمنی یک دستور shell.

        Args:
            command: دستور خام.

        Returns:
            :class:`SafetyDecision` با ریسک، دلایل و وضعیت تأیید.
        """
        raw = str(command or "").strip()
        decision = SafetyDecision(risk=RiskLevel.SAFE)
        if not raw:
            decision.allowed = False
            decision.risk = RiskLevel.CRITICAL
            decision.reasons.append("empty command")
            return decision

        lowered = raw.lower()
        features: list[str] = []
        for feature in ShellFeature:
            if feature.value in lowered:
                features.append(feature.value)
        if features:
            decision.risk = _merge_risk(decision.risk, RiskLevel.MEDIUM)
            decision.reasons.append(f"shell control sequences detected ({', '.join(features)})")

        parsed = self.parse_command(raw)
        decision.metadata["program"] = parsed.program
        decision.metadata["redirects"] = list(parsed.redirects)

        for token in parsed.args:
            if _is_sensitive_name(token):
                decision.risk = RiskLevel.CRITICAL
                decision.reasons.append(f"command touches a sensitive file ('{token}')")
                break

        for pattern, label in self.CRITICAL_PATTERNS:
            if re.search(pattern, lowered):
                decision.risk = RiskLevel.CRITICAL
                decision.reasons.append(f"dangerous command detected: {label}")
                break
        else:
            for pattern, label in self.CONFIRM_PATTERNS:
                if re.search(pattern, lowered):
                    decision.risk = _merge_risk(decision.risk, RiskLevel.HIGH)
                    decision.reasons.append(f"sensitive command: {label}")
            for blocked_pattern, _ in self._extra_blocklist:
                if blocked_pattern.search(lowered):
                    decision.risk = RiskLevel.CRITICAL
                    decision.reasons.append("matches a blocked command pattern from configuration")
            for confirm_pattern, _ in self._extra_confirm:
                if confirm_pattern.search(lowered):
                    decision.risk = _merge_risk(decision.risk, RiskLevel.HIGH)
                    decision.requires_confirmation = True
                    decision.reasons.append("matches a confirmation rule from configuration")

        if not self.unrestricted_filesystem:
            self._check_command_paths(parsed, decision)

        if decision.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            if self.policy == "deny":
                decision.allowed = False
                decision.requires_confirmation = False
                decision.reasons.append("blocked by DANGEROUS_COMMAND_POLICY=deny")
            else:
                decision.requires_confirmation = True
        elif decision.risk is RiskLevel.MEDIUM and features:
            decision.requires_confirmation = True

        return decision

    def _check_command_paths(self, parsed: CommandToken, decision: SafetyDecision) -> None:
        """بررسی اینکه آرگومان‌های مسیریِ دستور از sandbox بیرون نزنند."""
        if self.unrestricted_filesystem or not self.allowed_directories:
            return  # دسترسی آزاد = بدون محدودیت مسیر
        for token in parsed.args:
            if not token or token.startswith("-"):
                continue
            candidate = Path(token).expanduser()
            if not candidate.exists():
                continue
            if (candidate.is_dir() or candidate.suffix) and not is_within(candidate, self.allowed_directories):
                decision.risk = _merge_risk(decision.risk, RiskLevel.HIGH)
                decision.reasons.append(f"path '{candidate}' is outside the allowed directories")

    # ------------------------------------------------------------------
    # بررسی مسیر فایل
    # ------------------------------------------------------------------
    def _assess_path(self, path: str | Path, *, action: str = "read") -> SafetyDecision:
        """ارزیابی ایمنی یک مسیر فایل برای عملیات مشخص.

        Args:
            path: مسیر خام (نسبی/مطلق/با ``~``).
            action: یکی از ``read``, ``write``, ``delete``, ``move``, ``list``.

        Returns:
            :class:`SafetyDecision`.
        """
        decision = SafetyDecision(risk=RiskLevel.SAFE, metadata={"action": action})
        raw = str(path or "").strip()
        if not raw:
            decision.allowed = False
            decision.risk = RiskLevel.CRITICAL
            decision.reasons.append("empty path")
            return decision
        if "\x00" in raw:
            decision.allowed = False
            decision.risk = RiskLevel.CRITICAL
            decision.reasons.append("path contains a NUL byte")
            return decision

        candidate = Path(raw).expanduser()
        resolved = candidate.resolve(strict=False)
        decision.metadata["resolved"] = str(resolved)
        # مسیرهای ویندوزی روی POSIX هم باید بررسی شوند (resolve آن‌ها را به cwd می‌چسباند)
        decision.metadata["raw"] = raw

        for pattern in SENSITIVE_PATH_PATTERNS:
            if self._matches_glob(resolved, pattern) or self._matches_glob(candidate, pattern):
                decision.allowed = False
                decision.risk = RiskLevel.CRITICAL
                decision.reasons.append(f"protected credential/config path (pattern '{pattern}')")
                return decision

        for pattern in self.blocked_path_patterns:
            if self._matches_glob(resolved, pattern):
                decision.allowed = False
                decision.risk = RiskLevel.HIGH
                decision.reasons.append("path matches a blocked pattern from configuration")
                return decision

        if (
            not self.unrestricted_filesystem
            and self.allowed_directories
            and not is_within(resolved, self.allowed_directories)
        ):
            roots = ", ".join(str(root) for root in self.allowed_directories)
            decision.allowed = False
            decision.risk = RiskLevel.HIGH
            decision.reasons.append(f"path is outside the allowed directories ({roots})")
            return decision

        if action in {"write", "delete", "move"}:
            if self._is_protected_system_path(resolved) or self._is_protected_system_path(
                Path(raw.replace("\\", "/"))
            ):
                decision.allowed = False
                decision.risk = RiskLevel.CRITICAL
                decision.reasons.append(f"'{action}' inside a protected system directory")
                return decision
            for pattern in self.read_only_patterns:
                if self._matches_glob(resolved, pattern):
                    decision.allowed = False
                    decision.risk = RiskLevel.HIGH
                    decision.reasons.append("path is marked read-only by configuration")
                    return decision
            decision.risk = RiskLevel.HIGH if action == "delete" else RiskLevel.MEDIUM
            if action == "delete":
                decision.reasons.append("deleting a file is irreversible")
                decision.requires_confirmation = True
            else:
                decision.requires_confirmation = True
        elif action == "list":
            decision.risk = RiskLevel.SAFE
        else:
            decision.risk = RiskLevel.SAFE if resolved.exists() else RiskLevel.LOW
            if resolved.is_dir():
                decision.risk = RiskLevel.LOW

        return decision

    def _is_protected_system_path(self, resolved: Path) -> bool:
        """آیا مسیر یکی از دایرکتوری‌های حفاظت‌شده‌ی سیستمی است؟"""
        text = str(resolved).replace("\\", "/").lower()
        for protected in PROTECTED_SYSTEM_DIRS:
            base = protected.replace("\\", "/").lower().rstrip("/")
            if text == base or text.startswith(base + "/"):
                return True
        home = Path.home().resolve(strict=False)
        return text in {str(home), str(home.parent)}

    def _matches_glob(self, path: Path, pattern: str) -> bool:
        """مقایسه‌ی مسیر با یک الگو (fnmatch روی کل مسیر، نام فایل و مسیر نسبی).

        مسیر هم به‌صورت مطلق و هم نسبت به ریشه‌ی پروژه بررسی می‌شود تا الگوهایی
        مثل ``.env`` (فایل dotenv ریشه) درست عمل کنند و ``src/config.py`` اشتباه
        رد نشود.
        """
        from fnmatch import fnmatch

        glob = pattern.replace("\\", "/").lower()
        text = str(path).replace("\\", "/").lower()
        name = text.rsplit("/", 1)[-1]
        candidates = [text, name]
        with contextlib.suppress(ValueError):
            candidates.insert(1, str(path.relative_to(self.project_root)).replace("\\", "/").lower())
        for index, candidate in enumerate(candidates):
            if fnmatch(candidate, glob):
                return True
            # الگوی بدون اسلش (مثلاً ``.env``) باید با نام فایل هم مقایسه شود،
            # اما الگوهای چندبخشی فقط روی کل مسیر تطبیق داده می‌شوند تا
            # «*ssh/*» کل فایل‌ها را نگیرد.
            if index > 0 and "/" not in glob and fnmatch(candidate, glob):
                return True
        return False

    # ------------------------------------------------------------------
    # بررسی‌های سطح‌بالا برای ابزارها
    # ------------------------------------------------------------------
    def _assess_network(self, url: str) -> SafetyDecision:
        """بررسی SSRF: جلوگیری از درخواست به آدرس‌های داخلی."""
        from src.utils.validators import is_private_host

        decision = SafetyDecision(risk=RiskLevel.LOW, metadata={"url": url})
        try:
            if is_private_host(url):
                decision.allowed = False
                decision.risk = RiskLevel.HIGH
                decision.reasons.append("private/loopback host is not reachable from the agent (SSRF protection)")
        except ValidationError as exc:
            decision.allowed = False
            decision.reasons.append(str(exc))
        return decision

    def _relax(self, decision: SafetyDecision) -> SafetyDecision:
        """تنها شل‌کردن عامدانه: با ``ENABLE_SAFETY_GUARD=false`` هر چیزی مجاز می‌شود.

        قواعد **بحرانی** و بلوک‌های سطح HIGH دست‌نخورده می‌مانند؛ این‌طور حتی با
        نگهبان خاموش هم «rm -rf /» یا نوشتن روی /dev/sda ممکن نمی‌شود. طراحی عمدی است:
        کاربر ممکن است غلط کند، اما نباید بدون اراده داده‌اش را از دست بدهد.
        """
        if self.enabled:
            return decision
        if decision.risk is RiskLevel.CRITICAL:
            return decision
        if decision.blocked and decision.risk is RiskLevel.HIGH:
            return decision
        if decision.allowed and not decision.requires_confirmation:
            return decision
        return SafetyDecision(
            allowed=True,
            requires_confirmation=False,
            risk=decision.risk,
            reasons=[*decision.reasons, "safety guard disabled via ENABLE_SAFETY_GUARD=false"],
            metadata={**decision.metadata, "guard_disabled": True},
        )

    def assess_command(self, command: str) -> SafetyDecision:
        """بررسی دستور shell (با لحاظ کردن کلید فعال/غیرفعال نگهبان)."""
        return self._relax(self._assess_command(command))

    def assess_path(self, path: str | Path, *, action: str = "read") -> SafetyDecision:
        """بررسی مسیر فایل (با لحاظ کردن کلید فعال/غیرفعال نگهبان)."""
        return self._relax(self._assess_path(path, action=action))

    def assess_network(self, url: str) -> SafetyDecision:
        """بررسی آدرس شبکه (با لحاظ کردن کلید فعال/غیرفعال نگهبان)."""
        return self._relax(self._assess_network(url))

    def summary(self) -> dict[str, Any]:
        """خلاصه‌ی تنظیمات ایمنی (برای ``/config`` در CLI)."""
        return {
            "enabled": self.enabled,
            "policy": self.policy,
            "allow_shell": self.allow_shell,
            "allowed_directories": [str(p) for p in self.allowed_directories],
            "unrestricted_filesystem": self.unrestricted_filesystem,
            "protected_system_dirs": len(PROTECTED_SYSTEM_DIRS),
            "sensitive_patterns": len(SENSITIVE_PATH_PATTERNS),
            "critical_rules": len(self.CRITICAL_PATTERNS),
            "confirm_rules": len(self.CONFIRM_PATTERNS),
            "custom_block_rules": len(self._extra_blocklist),
            "custom_confirm_rules": len(self._extra_confirm),
        }

"""ابزار اجرای دستور در ترمینال (Command Pattern + Strategy).

ویژگی‌ها:

* اجرای ایوا با ``asyncio.create_subprocess_exec`` (بدون shell) به‌عنوان مسیر پیش‌فرض
* اجرای ``shell`` فقط با اجازه‌ی صریح (``ALLOW_SHELL=true`` یا ``shell=true``)
* بررسی ایمنی اجباری: دستورهای ویرانگر مسدود یا نیازمند تأیید می‌شوند
* timeout قابل تنظیم، capture stdout/stderr، کد بازگشت، اجرای موازی
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.helpers import format_duration
from src.utils.safety import SafetyDecision, ShellFeature
from src.utils.validators import ValidationError, bounded_int, validate_shell_tokens

__all__ = ["ShellSession", "TerminalTool"]

#: سقف خواندن خروجی هر stream (بایت) تا حافظه منفجر نشود
STREAM_READ_LIMIT = 1_048_576


@register_tool
class TerminalTool(BaseTool):
    """اجرای یک دستور shell با ثبت خروجی و کد بازگشت."""

    name: ClassVar[str] = "terminal_run"
    description: ClassVar[str] = (
        "Run a shell command on the host and return stdout, stderr and the exit code. "
        "Prefer one focused command per call. Destructive commands (rm, sudo, package removal, git "
        "history rewriting, force-push) are blocked or require explicit user confirmation. "
        "Always quote paths containing spaces."
    )
    requires_confirmation: ClassVar[bool] = False
    category: ClassVar[ToolCategory] = ToolCategory.DEVELOPER
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM
    required_parameters: ClassVar[tuple[str, ...]] = ("command",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("timeout", "cwd", "shell", "input", "env")
    sensitive_parameters: ClassVar[tuple[str, ...]] = ("input",)

    # ------------------------------------------------------------------
    # ورودی / ایمنی
    # ------------------------------------------------------------------
    def validate_input(self, **kwargs: Any) -> bool:
        """بررسی ساختاری دستور پیش از اجرا.

        Raises:
            ValidationError: دستور خالی/چندخطی، timeout خارج از بازه، یا استفاده
                از اپراتورهای shell بدون فعال‌کردن ``shell=true``.
        """
        super().validate_input(**kwargs)
        command = str(kwargs.get("command", ""))
        validate_shell_tokens(command)
        bounded_int(
            kwargs.get("timeout"),
            name="timeout",
            minimum=1,
            maximum=self._max_timeout(),
            default=self._max_timeout(),
        )
        return True

    async def safety_check(self, kwargs: dict[str, Any], context: ToolContext | None = None) -> SafetyDecision:
        """ارزیابی ریسک دستور و تعیین نیاز به تأیید.

        اینجا ورودی‌هایی که بدون shell معنای درستی ندارند (لوله، redirect،
        زیرshell) هم رد می‌شوند تا مدل به ``shell=true`` هدایت شود.
        """
        command = str(kwargs.get("command", ""))
        guard = self.safety_guard(context)
        if guard is None:
            # بدون guard هم shell mode و اپراتورهای shell تأیید می‌خواهند (defense in depth)
            risky = bool(kwargs.get("shell")) or any(feature.value in command for feature in ShellFeature)
            return SafetyDecision(
                allowed=True,
                requires_confirmation=self.requires_confirmation or risky,
                risk=RiskLevel.HIGH if risky else self.risk_level,
                reasons=["safety guard unavailable; relying on intrinsic tool risk"],
            )
        decision = guard.assess_command(command)
        if not kwargs.get("shell"):
            offenders = sorted({feature.value for feature in ShellFeature if feature.value in command})
            if offenders:
                decision.allowed = False
                decision.reasons.append(
                    "command uses shell operators "
                    + ", ".join(repr(item) for item in offenders)
                    + " but shell=false; re-run with shell=true to confirm the interpreted command"
                )
                return decision
        if bool(kwargs.get("shell")):
            decision.reasons.append("shell mode requested: operators | ; && > are interpreted by /bin/sh")
            decision.risk = RiskLevel.CRITICAL if decision.risk is RiskLevel.CRITICAL else RiskLevel.HIGH
            if guard.policy != "deny":
                decision.requires_confirmation = True
        if not guard.allow_shell and kwargs.get("shell"):
            decision.allowed = False
            decision.reasons.append("shell mode is disabled (set ALLOW_SHELL=true to enable)")
        return decision

    # ------------------------------------------------------------------
    # اجرا
    # ------------------------------------------------------------------
    async def execute(  # type: ignore[override]
        self,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        shell: bool = False,
        input: str | None = None,  # noqa: A002 - نام پارامتر مطابق schema (ورودی stdin)
        env: dict[str, str] | None = None,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """اجرای دستور و بازگرداندن خروجی.

        Args:
            command: دستور خام.
            timeout: سقف زمانی اجرا (ثانیه)؛ سقف نهایی از config می‌آید.
            cwd: دایرکتوری کاری. باید داخل مسیرهای مجاز باشد.
            shell: اجرای از طریق ``/bin/sh -c`` (نیازمند تأیید بیشتر).
            input: ورودی stdin (مثلاً برای ``read`` یا interactive prompt).
            env: متغیرهای محیطی اضافی (روی environ والد سوار می‌شوند).
            context: زمینه‌ی تزریق‌شده توسط ایجنت.

        Returns:
            ``data`` شامل ``stdout``/``stderr``/``exit_code`` در یک dict.
        """
        guard = self.safety_guard(context)
        workdir = self._resolve_cwd(cwd, guard)
        effective_timeout = min(int(timeout or self._max_timeout()), self._max_timeout())
        # در حالت shell خودِ /bin/sh را اجرا می‌کنیم (به‌جای ``shell=True`` که
        # ریسک تزریق را بالاتر می‌برد و توسط ابزارهای lint هم علامت می‌خورد).
        argv: list[str] = ["/bin/sh", "-c", command] if shell else shlex.split(command, posix=True)
        started = asyncio.get_running_loop().time()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir) if workdir else None,
                env=self._merge_env(env),
            )
        except FileNotFoundError as exc:
            return ToolResult.fail(
                f"command not found: {exc}",
                tool=self.name,
                error_code="command_not_found",
                metadata={"program": command.split()[0] if command.split() else command[:40]},
            )
        except (OSError, ValueError) as exc:
            return ToolResult.fail(f"could not start process: {exc}", tool=self.name, error_code="spawn_failed")

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=input.encode() if isinstance(input, str) else input),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            await self._terminate(process)
            elapsed = format_duration(asyncio.get_running_loop().time() - started)
            return ToolResult.fail(
                f"command timed out after {effective_timeout}s ({elapsed} elapsed)",
                tool=self.name,
                error_code="timeout",
                metadata={"command": command[:200], "pid": process.pid},
            )
        except asyncio.CancelledError:  # pragma: no cover - کاربر Ctrl+C زد
            await self._terminate(process)
            raise

        stdout = self._decode(stdout_bytes)
        stderr = self._decode(stderr_bytes)
        exit_code = process.returncode
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        metadata: dict[str, Any] = {
            "command": command[:400],
            "cwd": str(workdir) if workdir else str(Path.cwd()),
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "shell": bool(shell),
            "stdout_bytes": len(stdout_bytes or b""),
            "stderr_bytes": len(stderr_bytes or b""),
        }
        payload = {
            "exit_code": exit_code,
            "stdout": stdout["text"],
            "stderr": stderr["text"],
            "duration_ms": duration_ms,
            "cwd": metadata["cwd"],
        }
        metadata["truncated_streams"] = bool(stdout["truncated"] or stderr["truncated"])
        if exit_code == 0:
            return ToolResult(success=True, data=payload, tool=self.name, metadata=metadata)
        return ToolResult(
            success=False,
            data=payload,
            error=(stderr["text"] or stdout["text"] or f"command exited with code {exit_code}").strip()[:2000],
            error_code="non_zero_exit",
            tool=self.name,
            metadata=metadata,
        )

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """کشتن مؤدبانه و سپس اجباری process (برای جلوگیری از یتیم‌شدن)."""
        for signal_name in ("terminate", "kill"):
            try:
                getattr(process, signal_name)()
            except (ProcessLookupError, PermissionError):  # pragma: no cover - race با پایان process
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
                return
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------
    # schema / کمکی‌ها
    # ------------------------------------------------------------------
    def get_schema(self) -> dict[str, Any]:
        """اسکیمای OpenAI Function Calling ابزار ترمینال."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            required=("command",),
            properties={
                "command": {
                    "type": "string",
                    "description": "The command to execute, e.g. 'ls -la' or 'pytest -q tests'.",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Max seconds to wait (1-{self._max_timeout()}). Defaults to the configured limit.",
                    "minimum": 1,
                    "maximum": self._max_timeout(),
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the command. Must be inside an allowed directory.",
                },
                "shell": {
                    "type": "boolean",
                    "description": "Run through /bin/sh so that pipes/redirection work. Requires confirmation.",
                    "default": False,
                },
                "input": {
                    "type": "string",
                    "description": "Optional data written to the command's stdin.",
                },
                "env": {
                    "type": "object",
                    "description": "Extra environment variables for this command only.",
                    "additionalProperties": {"type": "string"},
                },
            },
        )

    def _max_timeout(self) -> int:
        """سقف timeout از config (پیش‌فرض ۳۰ ثانیه)."""
        return int(getattr(self.config, "max_command_timeout", 30) or 30)

    def _merge_env(self, env: dict[str, str] | None) -> dict[str, str] | None:
        """ترکیب environ فعلی با متغیرهای درخواستی (None یعنی ارث‌بری کامل)."""
        if not env:
            return None
        merged = dict(os.environ)
        for key, value in env.items():
            if not isinstance(key, str) or not key:
                raise ValidationError("env keys must be non-empty strings")
            merged[str(key)] = "" if value is None else str(value)
        return merged

    def _resolve_cwd(self, cwd: str | None, guard: Any) -> Path | None:
        """اعتبارسنجی دایرکتوری کاری نسبت به سیاست ایمنی.

        Raises:
            ValidationError: مسیر وجود ندارد یا خارج از sandbox است.
        """
        if not cwd:
            return None
        path = Path(str(cwd)).expanduser().resolve(strict=False)
        if not path.is_dir():
            raise ValidationError(f"cwd '{cwd}' is not an existing directory")
        if guard is not None and not getattr(guard, "unrestricted_filesystem", False):
            decision = guard.assess_path(path, action="list")
            if not decision.allowed:
                raise ValidationError(f"cwd rejected by safety guard: {decision.reason_text}")
        return path

    @staticmethod
    def _decode(raw: bytes | None) -> dict[str, Any]:
        """دیکد بایت‌ها با حد حجم و علامت‌گذاری برش."""
        data = raw or b""
        truncated = len(data) > STREAM_READ_LIMIT
        text = data[:STREAM_READ_LIMIT].decode("utf-8", errors="replace").rstrip("\n")
        if truncated:
            text += f"\n…[stream truncated at {STREAM_READ_LIMIT} bytes]"
        return {"text": text, "truncated": truncated, "bytes": len(data)}


class ShellSession:
    """اجرای موازی چند دستور مستقل (Command Pattern با batch).

    مثال:
        >>> import asyncio
        >>> session = ShellSession(commands=["echo a", "echo b"], max_parallel=2)
        >>> results = asyncio.run(session.run())
        >>> [r.metadata.get("exit_code") for r in results]
        [0, 0]
    """

    def __init__(
        self,
        commands: list[str],
        *,
        tool: TerminalTool | None = None,
        context: ToolContext | None = None,
        max_parallel: int = 3,
        fail_fast: bool = False,
    ) -> None:
        """
        Args:
            commands: دستورهای مستقل.
            tool: نمونه‌ی ابزار مورد استفاده (ساخته می‌شود اگر None باشد).
            context: زمینه‌ی اجرا.
            max_parallel: حداکثر اجرای همزمان.
            fail_fast: با اولین خطا بقیه اجرا نشوند.
        """
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        self.commands = list(commands)
        self.tool = tool or TerminalTool()
        self.context = context
        self.max_parallel = max_parallel
        self.fail_fast = fail_fast
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._aborted = False

    async def run(self) -> list[ToolResult]:
        """اجرای همه‌ی دستور‌ها با محدودیت همزمانی."""
        results: list[ToolResult | None] = [None] * len(self.commands)

        async def _worker(index: int, command: str) -> None:
            if self._aborted:
                results[index] = ToolResult.fail(
                    "skipped: an earlier command failed (fail_fast)",
                    tool=self.tool.name,
                    error_code="skipped",
                )
                return
            async with self._semaphore:
                # ممکن است حین انتظار برای semaphore، اجرای قبلی شکست خورده باشد
                if self._aborted:
                    results[index] = ToolResult.fail(
                        "skipped: an earlier command failed (fail_fast)",
                        tool=self.tool.name,
                        error_code="skipped",
                    )
                    return
                # batch از طریق shell اجرا می‌شود تا اپراتورها معنا داشته باشند
                outcome = await self.tool.run({"command": command, "shell": True}, context=self.context)
                results[index] = outcome
                if self.fail_fast and not outcome.success:
                    self._aborted = True

        await asyncio.gather(*(_worker(i, cmd) for i, cmd in enumerate(self.commands)))
        return [result for result in results if result is not None]

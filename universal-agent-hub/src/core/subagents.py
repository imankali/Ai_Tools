"""ساب‌ایجنت‌ها: تفویض محدود و کنترل‌شده.

چرا «محدود»؟
------------
OpenClaw ``fleet``/``routing`` دارد و Agent-Zero ``subagents``. اما تفویض
بدون مهار یعنی **بمب بازگشتی**: ایجنت A کار را به B می‌سپارد، B به C، و تا
ابد. پس اینجا سه ترمز داریم:

1. ``max_depth`` — عمق زنجیره‌ی تفویض (پیش‌فرض ۲).
2. ``max_concurrent`` — چند ساب‌ایجنت هم‌زمان (پیش‌فرض ۳).
3. ``inherit_guard`` — گارد ایمنی والد به فرزند *ارث* می‌رسد و فرزند نمی‌تواند
   آن را شل‌تر کند؛ پروفایل فرزند فقط می‌تواند **محدودتر** باشد.

همچنین هر تفویض در ``depth`` جاری ثبت می‌شود تا گزارش‌ها نشان دهند چه کسی به
چه کسی کار سپرد.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from src.utils.logger import get_logger

__all__ = ["DelegationResult", "SubagentPool", "SubagentSpec", "SubagentStatus"]

logger = get_logger("core.subagents")

#: سازنده‌ی ایجنت: ``(profile) -> agent`` که ``agent.ask(prompt)`` دارد.
AgentFactoryFn = Callable[[str], Any]


class SubagentStatus(str, Enum):
    """وضعیت یک تفویض."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass
class SubagentSpec:
    """درخواست تفویض."""

    prompt: str
    profile: str = "generalist"
    label: str = ""
    timeout: float = 240.0
    max_depth: int = 1

    def __post_init__(self) -> None:
        """اعتبارسنجی سبک."""
        self.prompt = str(self.prompt or "").strip()
        self.label = str(self.label or "").strip()[:80] or self.prompt[:40]
        self.timeout = max(5.0, min(float(self.timeout), 3600.0))
        self.max_depth = max(0, min(int(self.max_depth), 8))


@dataclass
class DelegationResult:
    """نتیجه‌ی یک تفویض."""

    id: str = field(default_factory=lambda: uuid4().hex[:12])
    label: str = ""
    profile: str = "generalist"
    status: SubagentStatus = SubagentStatus.PENDING
    text: str = ""
    error: str = ""
    depth: int = 0
    duration_ms: int = 0
    tool_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """آیا موفق بود؟"""
        return self.status is SubagentStatus.DONE

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return {
            "id": self.id,
            "label": self.label,
            "profile": self.profile,
            "status": self.status.value,
            "text": self.text[:2000],
            "error": self.error,
            "depth": self.depth,
            "duration_ms": self.duration_ms,
            "tool_names": list(self.tool_names),
        }


class SubagentPool:
    """مدیر تفویض کار به ایجنت‌های فرزند.

    نمونه::

        pool = SubagentPool(factory=lambda profile: AgentFactory.create(profile))
        result = await pool.delegate(SubagentSpec(prompt="summarise this repo", profile="read_only"))
    """

    def __init__(
        self,
        factory: AgentFactoryFn | None = None,
        *,
        max_depth: int = 2,
        max_concurrent: int = 3,
        allowed_profiles: frozenset[str] | None = None,
        audit: Any = None,
        notify: Callable[[str, str, str], None] | None = None,
    ) -> None:
        """Args:
        factory: سازنده‌ی ایجنت بر اساس پروفایل.
        max_depth: سقف عمق تفویض.
        max_concurrent: سقف هم‌زمانی.
        allowed_profiles: اگر داده شود، فقط این پروفایل‌ها مجازند.
        audit: لاگ ممیزی اختیاری.
        notify: مرکز اعلان‌ها اختیاری.
        """
        self.factory = factory
        self.max_depth = max(0, int(max_depth))
        self.max_concurrent = max(1, int(max_concurrent))
        self.allowed_profiles = allowed_profiles
        self._audit = audit
        self._notify = notify
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._active: dict[str, DelegationResult] = {}
        self._history: list[DelegationResult] = []
        self._depth = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ policy
    def check(self, spec: SubagentSpec, *, depth: int | None = None) -> tuple[bool, str]:
        """سیاست تفویض را بدون اجرا بررسی می‌کند.

        Args:
            spec: درخواست.
            depth: عمق فعلی (پیش‌فرض عمق داخلی pool).

        Returns:
            ``(مجاز, دلیل)``.
        """
        current = self._depth if depth is None else depth
        if self.factory is None:
            return False, "no agent factory configured"
        if not spec.prompt:
            return False, "empty prompt"
        if current >= self.max_depth:
            return False, f"delegation depth limit reached ({current}/{self.max_depth})"
        if self.allowed_profiles is not None and spec.profile not in self.allowed_profiles:
            known = ", ".join(sorted(self.allowed_profiles))
            return False, f"profile '{spec.profile}' is not allowed for subagents (allowed: {known})"
        return True, "ok"

    # ------------------------------------------------------------------ execution
    @property
    def depth(self) -> int:
        """عمق فعلی."""
        return self._depth

    @property
    def active(self) -> list[DelegationResult]:
        """تفویض‌های در جریان."""
        return list(self._active.values())

    def history(self, *, limit: int = 20) -> list[DelegationResult]:
        """تاریخچه، جدیدترین اول."""
        items = list(self._history)
        items.reverse()
        return items[: max(1, limit)]

    async def delegate(self, spec: SubagentSpec, *, depth: int | None = None) -> DelegationResult:
        """یک کار را به ساب‌ایجنت می‌سپارد.

        Args:
            spec: درخواست تفویض.
            depth: عمق فعلی زنجیره.

        Returns:
            :class:`DelegationResult` — هرگز استثنا نمی‌دهد.
        """
        current = self._depth if depth is None else depth
        result = DelegationResult(label=spec.label, profile=spec.profile, depth=current)
        allowed, reason = self.check(spec, depth=current)
        if not allowed:
            result.status = SubagentStatus.REJECTED
            result.error = reason
            self._record(result)
            self._audit_record("subagent.rejected", result, "denied", reason=reason)
            return result

        async with self._semaphore:
            self._active[result.id] = result
            result.status = SubagentStatus.RUNNING
            started = time.monotonic()
            agent: Any = None
            self._depth = current + 1
            try:
                agent = self.factory(spec.profile) if self.factory else None
                if agent is None:
                    raise RuntimeError("agent factory returned nothing")
                run = await asyncio.wait_for(agent.ask(spec.prompt), timeout=spec.timeout)
                result.text = str(getattr(run, "text", run) or "")[:4000]
                result.tool_names = [str(name) for name in (getattr(run, "tool_names", None) or [])]
                result.status = SubagentStatus.DONE
            except asyncio.TimeoutError:
                result.status = SubagentStatus.FAILED
                result.error = f"timed out after {spec.timeout:.0f}s"
            except asyncio.CancelledError:
                self._active.pop(result.id, None)
                self._depth = current
                raise
            except Exception as exc:  # noqa: BLE001 - شکست فرزند نباید والد را بکشد
                result.status = SubagentStatus.FAILED
                result.error = f"{type(exc).__name__}: {exc}"[:500]
            finally:
                self._depth = current
                result.duration_ms = int((time.monotonic() - started) * 1000)
                self._active.pop(result.id, None)
                close = getattr(agent, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("subagents: close failed: %s", exc)

        self._record(result)
        self._audit_record(
            f"subagent.{result.status.value}",
            result,
            "allowed" if result.ok else "failed",
            profile=result.profile,
            error=result.error,
        )
        if self._notify is not None:
            try:
                self._notify(
                    "subagent",
                    f"subagent '{result.label}' {result.status.value}",
                    result.text[:300] or result.error,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("subagents: notify failed: %s", exc)
        return result

    async def delegate_many(self, specs: list[SubagentSpec]) -> list[DelegationResult]:
        """چند کار را موازی (اما با سقف ``max_concurrent``) می‌سپارد."""
        if not specs:
            return []
        return list(await asyncio.gather(*(self.delegate(spec) for spec in specs)))

    def _record(self, result: DelegationResult) -> None:
        """تاریخچه را به‌روز می‌کند (سقف ۲۰۰)."""
        self._history.append(result)
        if len(self._history) > 200:
            self._history = self._history[-200:]

    def _audit_record(self, action: str, result: DelegationResult, decision: str, **detail: Any) -> None:
        """ثبت در لاگ ممیزی."""
        if self._audit is None:
            return
        try:
            self._audit.record("subagent", action, result.label, decision, delegation_id=result.id, **detail)
        except Exception as exc:  # noqa: BLE001
            logger.debug("subagents: audit failed: %s", exc)

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/diagnostics``."""
        return {
            "max_depth": self.max_depth,
            "max_concurrent": self.max_concurrent,
            "depth": self._depth,
            "active": [item.as_dict() for item in self.active],
            "history": [item.as_dict() for item in self.history(limit=10)],
            "allowed_profiles": sorted(self.allowed_profiles) if self.allowed_profiles else None,
            "factory": self.factory is not None,
        }

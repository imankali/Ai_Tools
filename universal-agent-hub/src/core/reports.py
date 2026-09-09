"""گزارش فعالیت (activity report) — «چه کردم، چه چیزی را بلوکه کردی، بعدی چیست».

سه تکه در این ماژول است:

1. :class:`ActivityRecorder` — مشترکِ :class:`~src.core.event_bus.EventBus` که خلاصه‌ی هر
   اجرا و هر تصمیم ایمنی را در یک فایل JSONL (``Config.activity_path``) می‌نویسد. این تنها
   منبع *پایدار* گزارش است؛ تاریخچه‌ی bus در حافظه است و با ری‌استارت می‌رود.
2. :func:`build_report` — ترکیب آن فایل با :class:`~src.core.memory.AgentMemory` برای یک
   dict کامل (اجراها، ابزارهای پرتکرار، بلوک‌ها/ردّها، توکن‌ها، برنامه‌های باز).
3. :func:`render_report_text` — همان گزارش برای ترمینال/Rich و برای تب «Reports» UI.

سیاست طراحی: گزارش فقط *خواندن* است و هیچ اکشنی ندارد. نوشتنِ «برنامه‌ی بعدی» هم در حافظه
توسط خود ایجنت انجام می‌شود (``memory_write`` با ``kind=plan``) تا مسئولیت و منبع روشن بماند.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

from src.core.event_bus import EVENTS, EventBus
from src.utils.helpers import ensure_dir, format_duration, redact_secrets, truncate_text
from src.utils.logger import get_logger

__all__ = ["ActivityRecorder", "build_report", "redact_line", "render_report_text", "reset_recorder_cache"]

logger = get_logger("core.reports")

#: انواع رکورد در فایل فعالیت
KINDS: tuple[str, ...] = ("run", "run_failed", "blocked", "denied", "tool_failed")


def redact_line(value: Any, limit: int = 200) -> str:
    """یک خط تمیز و ماسک‌شده برای لاگ/گزارش."""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return truncate_text(redact_secrets(text), limit, suffix="…")[0]


class ActivityRecorder:
    """نویسنده/خواننده‌ی JSONL فعالیت ایجنت (thread-safe، append-only با فشرده‌سازی).

    Args:
        path: فایل JSONL. اگر ``None`` باشد فقط در حافظه می‌نویسد (برای تست).
        max_records: پس از عبور از ۱.۵× این تعداد، فایل فشرده می‌شود.
        enabled: ``False`` یعنی هیچ‌چیز روی دیسک نوشته نشود.
    """

    _CACHE: ClassVar[dict[Path, ActivityRecorder]] = {}
    _CACHE_LOCK: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, path: str | Path | None, *, max_records: int = 2000, enabled: bool = True) -> None:
        self.path: Path | None = Path(path).expanduser() if path else None
        self.max_records = max(50, int(max_records))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        if self.enabled and self.path is not None:
            self._load()

    # ------------------------------------------------------------------
    # ساخت و اتصال
    # ------------------------------------------------------------------
    @classmethod
    def for_config(cls, config: Any) -> ActivityRecorder | None:
        """recorder مشترک برای یک config (``None`` اگر گزارش خاموش باشد)."""
        if config is None or not getattr(config, "reports_enabled", False):
            return None
        path: Path | None = getattr(config, "activity_path", None)
        if path is None:
            return None
        key = Path(path)
        with cls._CACHE_LOCK:
            cached = cls._CACHE.get(key)
            if cached is None:
                cached = cls(
                    key,
                    max_records=int(getattr(config, "report_max_runs", 2000)),
                    enabled=True,
                )
                cls._CACHE[key] = cached
            return cached

    @classmethod
    def attach(cls, bus: EventBus, config: Any) -> list[str]:
        """وصل کردن recorder به یک bus. برگرداندن id اشتراک‌ها (خالی یعنی خاموش)."""
        recorder = cls.for_config(config)
        if recorder is None:
            return []
        return recorder.subscribe(bus)

    def subscribe(self, bus: EventBus) -> list[str]:
        """اشتراک روی رویدادهای سطح‌بالا (اجرا + تصمیم‌های ایمنی)."""
        wanted = (
            EVENTS.AGENT_COMPLETED,
            EVENTS.AGENT_FAILED,
            EVENTS.SAFETY_BLOCKED,
            EVENTS.TOOL_DENIED,
            EVENTS.TOOL_FAILED,
        )
        return [bus.subscribe(kind, self._on_event) for kind in wanted]

    # ------------------------------------------------------------------
    # نوشتن
    # ------------------------------------------------------------------
    def _on_event(self, event: Any) -> None:
        """هندلر سنکرون bus: هر رویداد را به یک رکورد گزارش تبدیل می‌کند."""
        kind = str(getattr(event, "kind", "") or "")
        payload: dict[str, Any] = dict(getattr(event, "payload", None) or {})
        if kind == EVENTS.AGENT_COMPLETED:
            self.record(
                {
                    "kind": "run",
                    "ok": bool(payload.get("ok", True)),
                    "iterations": int(payload.get("iterations") or 0),
                    "tools": [str(name) for name in (payload.get("tools") or [])],
                    "duration_ms": int(payload.get("duration_ms") or 0),
                    "tokens": int(payload.get("tokens") or 0),
                    "error": redact_line(payload.get("error"), 240),
                }
            )
        elif kind == EVENTS.AGENT_FAILED:
            self.record({"kind": "run_failed", "ok": False, "error": redact_line(payload.get("error"), 240)})
        elif kind == EVENTS.SAFETY_BLOCKED:
            reasons = payload.get("reasons") or payload.get("reason") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            self.record(
                {
                    "kind": "blocked",
                    "tool": str(payload.get("tool") or ""),
                    "reasons": [redact_line(reason, 160) for reason in list(reasons)[:6]],
                }
            )
        elif kind == EVENTS.TOOL_DENIED:
            self.record(
                {
                    "kind": "denied",
                    "tool": str(payload.get("tool") or ""),
                    "call_id": str(payload.get("call_id") or ""),
                }
            )
        elif kind == EVENTS.TOOL_FAILED:
            self.record(
                {
                    "kind": "tool_failed",
                    "tool": str(payload.get("tool") or ""),
                    "error": redact_line(payload.get("error"), 240),
                }
            )

    def _ensure_file(self) -> None:
        """فایل با مجوز ۰۶۰۰ (گزارش‌ها متن prompt و مسیرهای کاربر را دارند)."""
        assert self.path is not None
        ensure_dir(self.path.parent)
        with contextlib.suppress(OSError):
            if not self.path.exists():
                self.path.touch()
            self.path.chmod(0o600)

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        """افزودن یک رکورد (با ts و kind نرمال‌شده) و نوشتن پایدار."""
        if not self.enabled:
            return dict(entry)
        payload = dict(entry)
        payload["kind"] = str(payload.get("kind") or "run")
        payload.setdefault("ts", time.time())
        payload.setdefault("iso", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(payload["ts"]))))
        with self._lock:
            self._buffer.append(payload)
            if self.enabled and self.path is not None:
                try:
                    self._ensure_file()
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
                    if len(self._buffer) > int(self.max_records * 1.5):
                        self._compact_locked()
                except OSError as exc:  # pragma: no cover - دیسک پر/دسترسی
                    logger.warning("activity log write failed: %s", exc)
        return payload

    def note(self, text: str, *, kind: str = "note") -> dict[str, Any]:
        """یک یادداشت متنی در گزارش (مثلاً «کاربر از من خواست …»)."""
        return self.record({"kind": kind, "text": redact_line(text, 400)})

    def _load(self) -> None:
        """خواندن فایل موجود (خطوط خراب بی‌صدا رد می‌شوند)."""
        assert self.path is not None
        if not self.path.exists():
            return
        records: list[dict[str, Any]] = []
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue  # خط نصفه‌نویسه‌شده (crash حین نوشتن) بی‌صدا رد می‌شود
            if isinstance(parsed, dict) and "kind" in parsed:
                records.append(parsed)
        self._buffer = records[-self.max_records :]

    def _compact_locked(self) -> None:
        """بازنوشتن فایل با تازه‌ترین ``max_records`` رکورد.

        از ۱.۵× سقف به بعد صدا زده می‌شود، پس تعداد خطوط فایل همیشه بین
        ``max_records`` و ``1.5 * max_records`` می‌ماند (بازنوشتن به‌ازای هر رکورد
        گران است، مخصوصاً وقتی سرور ساعت‌ها روشن می‌ماند).
        """
        assert self.path is not None
        keep = self._buffer[-self.max_records :]
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in keep), encoding="utf-8"
            )
            tmp.replace(self.path)
            with self.path.open("a", encoding="utf-8"):
                pass
            self._buffer = keep
        except OSError as exc:  # pragma: no cover
            logger.warning("activity log compaction failed: %s", exc)

    # ------------------------------------------------------------------
    # خواندن
    # ------------------------------------------------------------------
    def entries(
        self, *, limit: int | None = None, since: float | None = None, kinds: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """رکوردها، از تازه به قدیم؛ با فیلتر زمانی/نوعی."""
        wanted = set(kinds) if kinds else None
        with self._lock:
            snapshot = list(self._buffer)
        out: list[dict[str, Any]] = []
        for item in reversed(snapshot):
            if since is not None and float(item.get("ts") or 0.0) < since:
                continue
            if wanted is not None and str(item.get("kind")) not in wanted:
                continue
            out.append(item)
            if limit is not None and len(out) >= max(1, int(limit)):
                break
        return out

    def clear(self) -> int:
        """پاک کردن فایل گزارش (بکاپ نمی‌گیرد؛ برای «فراموش‌کردن» آگاهانه)."""
        with self._lock:
            count = len(self._buffer)
            self._buffer = []
            if self.path is not None and self.path.exists():
                try:
                    self.path.write_text("", encoding="utf-8")
                except OSError as exc:  # pragma: no cover
                    logger.warning("activity log clear failed: %s", exc)
            return count

    def stats(self) -> dict[str, Any]:
        """آمار خام برای سربرگ گزارش."""
        with self._lock:
            total = len(self._buffer)
            by_kind: dict[str, int] = {}
            for item in self._buffer:
                key = str(item.get("kind") or "run")
                by_kind[key] = by_kind.get(key, 0) + 1
            first = self._buffer[0].get("ts") if self._buffer else None
            last = self._buffer[-1].get("ts") if self._buffer else None
        return {
            "enabled": self.enabled,
            "path": str(self.path) if self.path else None,
            "records": total,
            "by_kind": by_kind,
            "first_ts": float(first) if first else None,
            "last_ts": float(last) if last else None,
            "max_records": self.max_records,
        }

    @classmethod
    def clear_cache(cls) -> None:
        """پاک کردن کش (فقط برای تست‌ها)."""
        with cls._CACHE_LOCK:
            cls._CACHE.clear()


def reset_recorder_cache() -> None:
    """میان‌بر تستی: cache recorderها را خالی کن."""
    ActivityRecorder.clear_cache()


def _tally_row(tally: dict[str, dict[str, int]], name: Any) -> dict[str, int]:
    """یک سطر شمارش برای ابزار (ساخت در صورت نبود)."""
    return tally.setdefault(str(name or "?"), {"calls": 0, "failures": 0, "denied": 0, "blocked": 0})


def _tool_tally(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """شمارش فراخوانی/خطای هر ابزار روی کل بازه.

    منبع اصلی، خودِ رکوردهای اجراست (``calls`` با ok/code)؛ اگر کسی فقط با
    اشتراک روی bus کار کرده باشد (``ActivityRecorder.subscribe``) از رویدادهای
    مجزا پر می‌شود.
    """
    tally: dict[str, dict[str, int]] = {}
    for item in entries:
        kind = str(item.get("kind"))
        if kind == "run":
            calls = item.get("calls") or []
            if calls:
                for call in calls:
                    row = _tally_row(tally, call.get("tool"))
                    row["calls"] += 1
                    code = str(call.get("code") or "")
                    if not bool(call.get("ok")):
                        row["failures"] += 1
                    if code == "declined":
                        row["denied"] += 1
                    elif code == "blocked":
                        row["blocked"] += 1
            else:
                for name in item.get("tools") or []:
                    _tally_row(tally, name)["calls"] += 1
        elif kind == "tool_failed":
            row = tally.setdefault(
                str(item.get("tool") or "?"), {"calls": 0, "failures": 0, "denied": 0, "blocked": 0}
            )
            row["failures"] += 1
        elif kind == "denied":
            row = tally.setdefault(
                str(item.get("tool") or "?"), {"calls": 0, "failures": 0, "denied": 0, "blocked": 0}
            )
            row["denied"] += 1
        elif kind == "blocked":
            row = tally.setdefault(
                str(item.get("tool") or "?"), {"calls": 0, "failures": 0, "denied": 0, "blocked": 0}
            )
            row["blocked"] += 1
    ordered = sorted(tally.items(), key=lambda pair: (-pair[1]["calls"], pair[0]))
    return [{"tool": name, **counts} for name, counts in ordered]


def build_report(
    config: Any,
    *,
    days: float = 7.0,
    memory: Any = None,
    recorder: ActivityRecorder | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """گزارش کامل فعالیت در ``days`` روز اخیر (شامل برنامه‌ها و ترجیحات حافظه).

    Args:
        config: نمونه‌ی :class:`~src.config.Config` (برای مسیرها و فعال/خاموش‌بودن).
        days: پنجره‌ی زمانی؛ ``0`` یعنی همه‌ی تاریخچه.
        memory: نمونه‌ی AgentMemory (پیش‌فرض از config).
        recorder: نمونه‌ی ActivityRecorder (پیش‌فرض از config).
        limit: حداکثر اجراهای list‌شده در بخش ``recent``.

    Returns:
        dict با کلیدهای ``generated_at`` ``window_days`` ``runs`` ``tools`` ``safety``
        ``recent`` ``memory`` ``plans`` ``next_actions`` ``warnings``.
    """
    since = time.time() - max(0.0, float(days)) * 86400.0 if days and days > 0 else None
    if recorder is None:
        recorder = ActivityRecorder.for_config(config)
    entries = recorder.entries(limit=None, since=since) if recorder is not None else []
    runs = [item for item in entries if str(item.get("kind")) in {"run", "run_failed"}]
    ok_runs = [item for item in runs if item.get("ok")]
    tokens = sum(int(item.get("tokens") or 0) for item in runs)
    duration = sum(int(item.get("duration_ms") or 0) for item in runs)
    # شمارش تصمیم‌های ایمنی: هم از رویدادهای مجزا (ActivityRecorder.subscribe) و هم
    # از دل رکوردهای اجرا (code هر فراخوانی ابزار) — بدون شمارش دوباره.
    blocked: list[dict[str, Any]] = [item for item in entries if str(item.get("kind")) == "blocked"]
    denied: list[dict[str, Any]] = [item for item in entries if str(item.get("kind")) == "denied"]
    failed_tools: list[dict[str, Any]] = [item for item in entries if str(item.get("kind")) == "tool_failed"]
    for item in runs:
        for call in item.get("calls") or []:
            name = str(call.get("tool") or "")
            code = str(call.get("code") or "")
            derived: dict[str, Any] = {"tool": name, "iso": item.get("iso")}
            if code == "blocked":
                blocked.append({**derived, "reasons": [code]})
            elif code == "declined":
                denied.append(derived)
            elif not bool(call.get("ok")):
                failed_tools.append({**derived, "error": code or "failed"})

    if memory is None:
        try:  # import محلی تا چرخه‌ی import ایجاد نشود
            from src.core.memory import AgentMemory

            memory = AgentMemory.for_config(config)
        except Exception as exc:  # noqa: BLE001 - گزارش نباید به‌خاطر حافظه بترکد
            logger.debug("memory unavailable for report: %s", exc)
            memory = None

    plans: list[dict[str, Any]] = []
    recent_notes: list[dict[str, Any]] = []
    memory_stats: dict[str, Any] = {"enabled": False}
    if memory is not None:
        memory_stats = memory.stats()
        plans = [
            {
                "id": item.id,
                "content": item.content,
                "tags": item.tags,
                "age_days": round(max(0.0, (time.time() - item.created_at) / 86400.0), 1),
            }
            for item in memory.plans(20)
        ]
        recent_notes = [
            {"id": item.id, "kind": item.kind, "content": item.content, "tags": item.tags}
            for item in memory.recent(12)
        ]

    next_actions = _derive_next_actions(
        plans=plans, failed_tools=failed_tools, denied=denied, ok=bool(runs and not runs[-1].get("ok"))
    )
    warnings: list[str] = []
    if recorder is None:
        warnings.append("activity logging is off (REPORTS_ENABLED=false); run counts come from memory only")
    if not memory_stats.get("enabled"):
        warnings.append("long-term memory is off (MEMORY_ENABLED=false); plans and preferences are not persisted")

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window_days": float(days) if days else 0,
        "runs": {
            "total": len(runs),
            "ok": len(ok_runs),
            "failed": len(runs) - len(ok_runs),
            "tool_calls": sum(len(item.get("tools") or []) for item in runs),
            "tokens": tokens,
            "duration_ms": duration,
            "duration_human": format_duration(duration / 1000.0) if duration else "0s",
        },
        "tools": _tool_tally(entries)[:20],
        "safety": {
            "blocked": len(blocked),
            "denied": len(denied),
            "tool_failures": len(failed_tools),
            "last_denied": [{"tool": item.get("tool"), "iso": item.get("iso")} for item in denied[:8]],
            "last_blocked": [
                {"tool": item.get("tool"), "reasons": item.get("reasons") or [], "iso": item.get("iso")}
                for item in blocked[:8]
            ],
        },
        "recent": [
            {
                "kind": item.get("kind"),
                "ok": bool(item.get("ok", True)),
                "iso": item.get("iso"),
                "tools": (item.get("tools") or [])[:12],
                "iterations": item.get("iterations"),
                "tokens": item.get("tokens"),
                "duration_ms": item.get("duration_ms"),
                "error": item.get("error"),
            }
            for item in runs[: max(1, int(limit))]
        ],
        "memory": memory_stats,
        "plans": plans,
        "notes": recent_notes,
        "next_actions": next_actions,
        "warnings": warnings,
        "files": {
            "activity": recorder.stats().get("path") if recorder is not None else None,
            "memory": memory_stats.get("path"),
        },
    }


def _derive_next_actions(
    *,
    plans: list[dict[str, Any]],
    failed_tools: list[dict[str, Any]],
    denied: list[dict[str, Any]],
    ok: bool,
) -> list[str]:
    """چند پیشنهاد عملی از دل برنامه‌های باز، ابزارهای شکست‌خورده و ردّ‌ها."""
    actions: list[str] = []
    for plan in plans[:5]:
        actions.append(f"open plan: {plan['content'][:160]}")
    if failed_tools:
        names = sorted({str(item.get("tool") or "") for item in failed_tools[-10:] if item.get("tool")})
        if names:
            actions.append("tools failing repeatedly: " + ", ".join(names[:6]) + " — inspect before retrying")
    if denied:
        names = sorted({str(item.get("tool") or "") for item in denied[-10:] if item.get("tool")})
        if names:
            actions.append(
                "you denied these tools recently: " + ", ".join(names[:6]) + " — ask the user before repeating"
            )
    if ok:
        actions.append("last run ended with an error — re-run the failing step with --dry-run first")
    return actions


def render_report_text(report: dict[str, Any], *, title: str = "Activity report") -> str:
    """نسخه‌ی متنی (برای CLI و برای pre در مرورگر)."""
    lines: list[str] = [f"# {title} · {report.get('generated_at', '')}"]
    window = float(report.get("window_days") or 0)
    lines.append(f"window: {'all time' if window <= 0 else f'last {window:g} days'}")
    runs = report.get("runs") or {}
    lines.append(
        f"runs: {runs.get('total', 0)} ({runs.get('ok', 0)} ok, {runs.get('failed', 0)} failed) · "
        f"{runs.get('tool_calls', 0)} tool calls · {runs.get('tokens', 0)} tokens · {runs.get('duration_human', '0s')}"
    )
    safety = report.get("safety") or {}
    lines.append(
        f"safety: {safety.get('blocked', 0)} blocked, {safety.get('denied', 0)} denied, {safety.get('tool_failures', 0)} tool failures"
    )
    tools = report.get("tools") or []
    if tools:
        lines.append("")
        lines.append("busiest tools")
        for row in tools[:8]:
            lines.append(
                f"  {row['tool']:<22} calls={row['calls']:<4} failures={row['failures']:<3} denied={row['denied']:<3} blocked={row['blocked']}"
            )
    notes = report.get("notes") or []
    if notes:
        lines.append("")
        lines.append("from memory")
        for item in notes[:8]:
            kind = str(item.get("kind") or "note")
            lines.append(f"  {kind:<10} {redact_line(item.get('content'), 150)}")
    plans = report.get("plans") or []
    if plans:
        lines.append("")
        lines.append("open plans")
        for item in plans[:8]:
            lines.append(f"  · {redact_line(item['content'], 170)}  ({item['age_days']}d old)")
    actions = report.get("next_actions") or []
    if actions:
        lines.append("")
        lines.append("suggested next")
        for item in actions[:8]:
            lines.append(f"  → {item}")
    for warning in report.get("warnings") or []:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)

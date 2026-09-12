"""لاگ ممیزی تغییرناپذیر با زنجیره‌ی هش (tamper-evident audit log).

چرا این ماژول؟
--------------
``reports.py`` می‌گوید ایجنت *چه کرد*؛ این ماژول می‌گوید *چه کسی، چه چیزی را،
با چه تصمیمی* انجام داد — به‌شکلی که بعداً نتوان بی‌صدا تغییرش داد.

هر رکورد ``prev_sha`` و ``sha`` دارد:

    sha = sha256(prev_sha + canonical_json(record_without_sha))

بنابراین حذف/ویرایش/جابه‌جایی هر رکورد، زنجیره را از همان نقطه می‌شکند و
:meth:`AuditLog.verify_chain` دقیقاً همان ایندکس را گزارش می‌کند.

نکته‌ی طراحی: این یک «بلاک‌چین» نیست و ادعای آن را هم ندارد؛ فقط یک
append-only log با checksum زنجیره‌ای است — همان چیزی که برای «آیا کسی لاگ
را دستکاری کرده؟» کافی است و هیچ وابستگی تازه‌ای ندارد.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.utils.logger import get_logger

__all__ = [
    "GENESIS_SHA",
    "AuditDecision",
    "AuditEntry",
    "AuditLog",
    "ChainIntegrity",
]

logger = get_logger("core.audit")

#: هش رکورد صفرم؛ زنجیره همیشه از این مقدار شروع می‌شود.
GENESIS_SHA = "0" * 64


class AuditDecision(str, Enum):
    """نتیجه‌ی یک عملیات ممیزی‌شده."""

    ALLOWED = "allowed"
    DENIED = "denied"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    INFO = "info"


class AuditEntry(BaseModel):
    """یک رکورد ممیزی.

    Attributes:
        index: شماره‌ی ترتیبی (از صفر).
        ts: زمان UTC به‌صورت ISO-8601.
        actor: چه کسی (``user`` / ``agent`` / ``scheduler`` / ``webhook`` / …).
        action: چه کاری (``tool.terminal_run`` / ``routine.fire`` / …).
        target: روی چه چیزی (مسیر، URL، نام ابزار، …).
        decision: نتیجه.
        detail: جزئیات آزاد؛ پیش از نوشتن redact می‌شود.
        prev_sha: هش رکورد قبلی.
        sha: هش این رکورد.
    """

    index: int = Field(ge=0)
    ts: str
    actor: str
    action: str
    target: str = ""
    decision: AuditDecision = AuditDecision.INFO
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_sha: str = GENESIS_SHA
    sha: str = GENESIS_SHA

    def payload(self) -> dict[str, Any]:
        """بدون ``sha`` — همان چیزی که هش می‌شود."""
        return {
            "index": self.index,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "decision": self.decision.value,
            "detail": self.detail,
            "prev_sha": self.prev_sha,
        }

    def to_json(self) -> str:
        """یک خط JSON پایدار (کلیدها مرتب) برای نوشتن در JSONL."""
        data = self.payload()
        data["sha"] = self.sha
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ChainIntegrity(BaseModel):
    """نتیجه‌ی بررسی زنجیره."""

    ok: bool
    entries: int = 0
    broken_at: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize برای API."""
        return {"ok": self.ok, "entries": self.entries, "broken_at": self.broken_at, "reason": self.reason}


def _utc_now() -> str:
    """زمان حال UTC به‌صورت ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def compute_sha(entry: AuditEntry) -> str:
    """هش یک رکورد بر اساس payload و هش قبلی."""
    raw = json.dumps(entry.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _redact(value: Any, depth: int = 0) -> Any:
    """حذف کلیدهایی که معمولاً secret نگه می‌دارند (به‌صورت بازگشتی)."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            low = str(key).lower()
            if any(token in low for token in ("token", "secret", "password", "api_key", "apikey", "credential")):
                out[key] = "***redacted***"
            else:
                out[key] = _redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + "…"
    return value


class AuditLog:
    """لاگ append-only با زنجیره‌ی هش.

    نمونه::

        audit = AuditLog(path)
        audit.record("agent", "tool.terminal_run", "ls -la", AuditDecision.ALLOWED)
        assert audit.verify_chain().ok
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 8 * 1024 * 1024) -> None:
        """مسیر فایل JSONL را می‌گیرد و پوشه‌ی والد را می‌سازد.

        Args:
            path: فایل JSONL لاگ.
            max_bytes: سقف اندازه؛ بیش از این، فایل چرخش می‌کند (``.1``).
        """
        self.path = Path(path).expanduser().resolve()
        self.max_bytes = max(4096, int(max_bytes))
        self._lock = threading.RLock()
        self._count = 0
        self._head = GENESIS_SHA
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._resume()

    # ------------------------------------------------------------------ internals
    def _resume(self) -> None:
        """بازسازی ``_count`` و ``_head`` از رکوردهای *معتبر*.

        نکته: شمارش بر اساس خطوط نیست. اگر یک خط خراب در فایل باشد و آن را
        بشماریم، رکورد بعدی ``index`` اشتباه می‌گیرد و ``verify_chain`` به
        «index gap» می‌خورد — یعنی یک خط خراب، کل زنجیره را بی‌اعتبار می‌کرد.
        """
        if not self.path.exists():
            return
        valid = 0
        last: AuditEntry | None = None
        lineno = 0
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    lineno += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = AuditEntry.model_validate_json(line)
                    except Exception:  # noqa: BLE001 - یک خط خراب نباید لاگ را ببندد
                        logger.warning("audit: skipping malformed line #%d", lineno)
                        continue
                    valid += 1
        except OSError as exc:
            logger.warning("audit: cannot read %s: %s", self.path, exc)
            return
        self._count = valid
        self._head = last.sha if last is not None else GENESIS_SHA

    def _rotate_if_needed(self) -> None:
        """اگر فایل از سقف بزرگ‌تر شد، یک نسل عقب می‌برد."""
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        rotated = self.path.with_suffix(self.path.suffix + ".1")
        try:
            self.path.replace(rotated)
        except OSError as exc:
            logger.warning("audit: rotation failed: %s", exc)
            return
        self._count = 0
        self._head = GENESIS_SHA
        logger.info("audit: rotated log to %s", rotated)

    def _atomic_append(self, line: str) -> None:
        """نوشتن اتمیک: در فایل موقت هم‌پوشه، بعد append (تا خط نیمه‌نماند)."""
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            with contextlib.suppress(OSError):  # روی برخی FSها fsync مجاز نیست
                os.fsync(handle.fileno())

    # ------------------------------------------------------------------ public API
    @property
    def head(self) -> str:
        """هش آخرین رکورد (یا genesis اگر لاگ خالی است)."""
        return self._head

    def __len__(self) -> int:
        """تعداد رکوردهای نوشته‌شده."""
        return self._count

    def record(
        self,
        actor: str,
        action: str,
        target: str = "",
        decision: AuditDecision | str = AuditDecision.INFO,
        **detail: Any,
    ) -> AuditEntry:
        """یک رکورد به لاگ اضافه می‌کند و زنجیره را جلو می‌برد.

        Args:
            actor: عامل (``user``/``agent``/``scheduler``/``webhook``).
            action: نام عملیات.
            target: موضوع عملیات.
            decision: نتیجه.
            **detail: جزئیات آزاد (secretها redact می‌شوند).

        Returns:
            رکورد نوشته‌شده.
        """
        if isinstance(decision, str):
            try:
                decision = AuditDecision(decision.lower())
            except ValueError:
                decision = AuditDecision.INFO
        with self._lock:
            self._rotate_if_needed()
            entry = AuditEntry(
                index=self._count,
                ts=_utc_now(),
                actor=str(actor or "unknown"),
                action=str(action or "unknown"),
                target=str(target or "")[:512],
                decision=decision,
                detail=_redact(dict(detail)) or {},
                prev_sha=self._head,
            )
            entry.sha = compute_sha(entry)
            self._atomic_append(entry.to_json())
            self._count += 1
            self._head = entry.sha
            return entry

    def entries(self, *, limit: int | None = None, tail: bool = True) -> list[AuditEntry]:
        """رکوردها را می‌خواند.

        Args:
            limit: حداکثر تعداد.
            tail: اگر ``True``، آخرین‌ها (نه اولین‌ها).
        """
        items = list(self.iter_entries())
        if tail:
            items.reverse()
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    def iter_entries(self) -> Iterator[AuditEntry]:
        """پیمایش ترتیبی همه‌ی رکوردهای معتبر."""
        if not self.path.exists():
            return iter(())
        out: list[AuditEntry] = []
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(AuditEntry.model_validate_json(line))
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("audit: skipping unparsable entry: %s", exc)
                        continue
        except OSError as exc:
            logger.warning("audit: cannot read %s: %s", self.path, exc)
        return iter(out)

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        decision: AuditDecision | str | None = None,
        limit: int = 50,
    ) -> list[AuditEntry]:
        """فیلتر ساده روی آخرین رکوردها."""
        wanted = (
            AuditDecision(decision.lower()).value
            if isinstance(decision, str)
            else (decision.value if decision is not None else None)
        )
        found: list[AuditEntry] = []
        for entry in reversed(list(self.iter_entries())):
            if actor and entry.actor != actor:
                continue
            if action and entry.action != action:
                continue
            if wanted and entry.decision.value != wanted:
                continue
            found.append(entry)
            if len(found) >= max(1, limit):
                break
        return found

    def verify_chain(self) -> ChainIntegrity:
        """زنجیره را از ابتدا بررسی می‌کند.

        Returns:
            :class:`ChainIntegrity` با ``broken_at`` = ایندکس اولین رکورد خراب.
        """
        expected = GENESIS_SHA
        count = 0
        try:
            with open(self.path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = AuditEntry.model_validate_json(line)
                    except Exception as exc:  # noqa: BLE001
                        return ChainIntegrity(ok=False, entries=count, broken_at=lineno, reason=f"unparsable: {exc}")
                    if entry.index != count:
                        return ChainIntegrity(
                            ok=False, entries=count, broken_at=count, reason=f"index gap: expected {count}"
                        )
                    if entry.prev_sha != expected:
                        return ChainIntegrity(
                            ok=False,
                            entries=count,
                            broken_at=count,
                            reason="prev_sha mismatch (entry edited/removed)",
                        )
                    if compute_sha(entry) != entry.sha:
                        return ChainIntegrity(
                            ok=False, entries=count, broken_at=count, reason="sha mismatch (payload edited)"
                        )
                    expected = entry.sha
                    count += 1
        except FileNotFoundError:
            return ChainIntegrity(ok=True, entries=0)
        except OSError as exc:
            return ChainIntegrity(ok=False, entries=count, reason=f"unreadable: {exc}")
        return ChainIntegrity(ok=True, entries=count)

    def stats(self) -> dict[str, Any]:
        """خلاصه‌ی وضعیت برای ``/api/diagnostics``."""
        by_decision: dict[str, int] = {}
        by_action: dict[str, int] = {}
        for entry in self.iter_entries():
            by_decision[entry.decision.value] = by_decision.get(entry.decision.value, 0) + 1
            by_action[entry.action] = by_action.get(entry.action, 0) + 1
        size = 0
        try:
            size = self.path.stat().st_size
        except OSError as exc:
            logger.debug("audit: cannot stat %s: %s", self.path, exc)
        return {
            "path": str(self.path),
            "entries": self._count,
            "bytes": size,
            "head": self._head,
            "by_decision": by_decision,
            "top_actions": dict(sorted(by_action.items(), key=lambda kv: -kv[1])[:8]),
        }

    def export_json(self, path: str | Path) -> int:
        """خروجی JSON کامل (برای بکاپ)."""
        dest = Path(path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        items = [entry.model_dump(mode="json") for entry in self.iter_entries()]
        handle, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".audit-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"entries": items}, stream, ensure_ascii=False, indent=2)
            Path(tmp).replace(dest)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
        return len(items)

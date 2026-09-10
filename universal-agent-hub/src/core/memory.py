"""حافظه‌ی بلندمدت ایجنت (persistent, self-written memory).

چرا این فایل وجود دارد
----------------------
تاریخچه‌ی گفت‌وگو (``AgentState.messages``) با بستن ترمینال از بین می‌رود؛ ولی
ایجنتی که «مثل یک آدم» روی یک سیستم کار می‌کند باید بداند:

* این کاربر چه ترجیحاتی دارد (زبان، مسیر پروژه، سبک گزارش‌دهی)؛
* دفعه‌ی قبل چه کرد، چه چیزی یاد گرفت و چه چیزی را باید ادامه بدهد؛
* چه «رویه‌هایی» (procedure) جواب داد — مثلاً «برای این سایت اول لاگین، بعد…».

اینجا همان حافظه پیاده شده است: یک فایل **JSONL** که خط‌به‌خط به آن اضافه می‌شود، در
``~/.universal-agent-hub/memory/`` با این ویژگی‌ها:

* **بی‌خطر برای چند پروسه**: نوشتن اتمی (فایل موقت + ``replace``) و کش مشترک
  ماژولی، پس دو session همان رکوردها را می‌بینند؛
* **dedupe**: متن یکسان → همان رکورد به‌روز می‌شود (حافظه باد نمی‌کند)؛
* **redaction**: هر کلید API/توکن قبل از نوشتن ماسک می‌شود؛
* **rank ساده**: تطبیق کلمه‌ای + تازگی + ``pinned`` (بدون وابستگی به sqlite_fts);
* **context_block**: چیزی که مستقیم به system prompt اضافه می‌شود، با سقف کاراکتر؛
* **خاموش‌شدنی**: ``MEMORY_ENABLED=false`` یعنی هیچ فایل نوشته/خوانده نمی‌شود.

الگوهای طراحی: **Repository** ( this file ) + **Observer-lite** (``capture_run``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.utils.helpers import redact_secrets, truncate_text
from src.utils.logger import get_logger

__all__ = [
    "MEMORY_KINDS",
    "AgentMemory",
    "MemoryRecord",
    "memory_for_config",
    "reset_memory_cache",
]

logger = get_logger("core.memory")

#: نوع‌های مجاز رکورد (هر کدام یک «لایه» از حافظه است)
MEMORY_KINDS: tuple[str, ...] = ("fact", "preference", "procedure", "decision", "plan", "note")

#: واژه‌های بی‌ارزش برای جست‌وجوی ساده
_STOPWORDS: frozenset[str] = frozenset(
    (
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "be",
        "this",
        "that",
        "it",
        "its",
        "as",
        "at",
        "by",
        "from",
        "من",
        "به",
        "با",
        "در",
        "که",
        "این",
        "آن",
        "است",
        "هست",
        "برای",
        "از",
        "تا",
        "آیا",
        "چه",
        "چرا",
        "کجا",
        "خیلی",
        "بیشتر",
        "کمتر",
    )
)

_TOKEN_RE = re.compile(r"[a-z0-9_./:-]{2,}|[\u0600-\u06FF]{2,}", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    """واژه‌های کلیدی کوچک‌شده (برای امتیازدهی جست‌وجو)."""
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(text or "")} - set(_STOPWORDS)


class MemoryRecord(BaseModel):
    """یک تکه حافظه.

    Attributes:
        id: شناسه‌ی کوتاه (8 کاراکتر اول sha محتوا + شمارنده در صورت تشابه).
        kind: نوع رکورد (از :data:`MEMORY_KINDS`).
        content: خودِ یادداشت (متن آزاد؛ حداکثر ۴۰۰۰ کاراکتر بریده می‌شود).
        tags: برچسب‌های کوچک برای فیلتر (مثلاً ``["project:webshop"]``).
        source: چه چیزی این را نوشته (``agent``, ``user``, ``tool:terminal_run``, …).
        confidence: ۰..۱ — «چقدر مطمئنم»; برای ranking استفاده می‌شود.
        created_at/updated_at: زمان unix.
        pinned: اگر True باشد هرگز prune نمی‌شود و اول context block می‌آید.
        hits: چند بار به این رکورد مراجعه شده (برای رتبه‌بندی و گزارش).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = ""
    kind: str = "note"
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str = "agent"
    confidence: float = 0.7
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    pinned: bool = False
    hits: int = 0

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: Any) -> str:
        """kind ناشناخته → ``note`` (حافظه نباید به‌خاطر یک تایپو از کار بیفتد)."""
        text = str(value or "note").strip().lower()
        return text if text in MEMORY_KINDS else "note"

    @field_validator("content")
    @classmethod
    def _sanitize(cls, value: str) -> str:
        """برش + ماسک‌کردن کلیدهای API پیش از ماندن روی دیسک."""
        text, _ = truncate_text(value, 4000, suffix="…[truncated]")
        return redact_secrets(text)

    @property
    def sha(self) -> str:
        """اثر انگشت محتوا (برای dedupe)."""
        return hashlib.sha256(f"{self.kind}|{self.content}".encode("utf-8", "ignore")).hexdigest()[:16]

    def score(self, query_tokens: set[str], *, now: float | None = None) -> float:
        """امتیاز ترتیب‌دادن: تطبیق واژه + تازگی + pin + اعتماد به‌نفس + دفعات استفاده."""
        tokens = _tokens(self.content) | {tag.lower() for tag in self.tags}
        overlap = len(query_tokens & tokens) / (len(query_tokens) or 1) if query_tokens else 0.0
        age_days = max(0.0, ((now or time.time()) - self.updated_at) / 86400.0)
        recency = 1.0 / (1.0 + age_days / 14.0)
        return (
            2.5 * overlap
            + 1.0 * recency
            + (1.5 if self.pinned else 0.0)
            + 0.5 * max(0.0, min(1.0, self.confidence))
            + 0.05 * min(10, self.hits)
        )

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی JSON (بدون فیلد اضافه)."""
        return self.model_dump(mode="json")


class AgentMemory:
    """مخزن حافظه (JSONL) با ایندکس در حافظه.

    نمونه‌ها از :func:`memory_for_config` گرفته می‌شوند تا همه‌ی session ها یک
    store مشترک داشته باشند؛ ``AgentMemory(path)`` مستقیم برای تست هم مجاز است.
    """

    _CACHE: ClassVar[dict[Path, AgentMemory]] = {}
    _CACHE_LOCK: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, path: str | Path | None, *, max_records: int = 2000, enabled: bool = True) -> None:
        """ساخت/بازکردن یک store.

        Args:
            path: فایل JSONL (``None`` یعنی فقط در حافظه؛ برای تست).
            max_records: سقف رکورد پس از هر write (pinned معاف است).
            enabled: ``False`` یعنی store توخالی و بی‌اثر (هیچ IO ای).
        """
        self.enabled = bool(enabled)
        self.path = Path(path).expanduser() if path is not None else None
        self.max_records = max(20, int(max_records))
        self._records: list[MemoryRecord] = []
        self._by_sha: dict[str, MemoryRecord] = {}
        self._lock = threading.RLock()
        if self.enabled and self.path is not None:
            self._load()

    # ------------------------------------------------------------------ ساخت/کش

    @classmethod
    def for_config(cls, config: Any) -> AgentMemory | None:
        """store مربوط به یک :class:`src.config.Config` (یا ``None`` اگر خاموش بود)."""
        if not getattr(config, "memory_enabled", False):
            return None
        path: Path | None = getattr(config, "memory_path", None)
        key = Path(path) if path is not None else Path(f"__in_memory__:{id(config)}")
        with cls._CACHE_LOCK:
            cached = cls._CACHE.get(key)
            if cached is None:
                cached = cls(
                    path,
                    max_records=int(getattr(config, "memory_max_records", 2000)),
                    enabled=bool(getattr(config, "memory_enabled", True)),
                )
                cls._CACHE[key] = cached
            return cached

    @classmethod
    def clear_cache(cls) -> None:
        """پاک‌کردن کش (برای تست و پس از تغییر تنظیمات)."""
        with cls._CACHE_LOCK:
            cls._CACHE.clear()

    # ------------------------------------------------------------------ IO

    def _load(self) -> None:
        """خواندن JSONL؛ خطوط خراب بی‌صدا رد می‌شوند (حافظه نباید برنامه را بخواباند)."""
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - دیسک پر/دسترسی
            logger.warning("memory file unreadable (%s): %s", self.path, exc)
            return
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = MemoryRecord(**json.loads(line))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            self._append_index(record)
        logger.debug("memory loaded: %d record(s) from %s", len(self._records), self.path)

    def flush(self) -> None:
        """نوشتن کامل (اتمیک). برای داده‌ی کم این ساده‌ترین و امن‌ترین راه است."""
        if not self.enabled or self.path is None:
            return
        with self._lock:
            payload = "\n".join(json.dumps(item.as_dict(), ensure_ascii=False) for item in self._records)
            if payload:
                payload += "\n"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(self.path.suffix + ".tmp")
                temp.write_text(payload, encoding="utf-8")
                with contextlib.suppress(OSError):
                    temp.chmod(0o600)
                temp.replace(self.path)
            except OSError as exc:  # pragma: no cover - دیسک پر
                logger.warning("memory flush failed (%s): %s", self.path, exc)

    # ------------------------------------------------------------------ CRUD

    def _append_index(self, record: MemoryRecord) -> MemoryRecord:
        """افزودن به ایندکس + پرکردن id (داخل قفل)."""
        if not record.id:
            record.id = record.sha[:10]
        self._records.append(record)
        self._by_sha[record.sha] = record
        return record

    def add(
        self,
        content: str,
        *,
        kind: str = "note",
        tags: Sequence[str] | None = None,
        source: str = "agent",
        confidence: float = 0.7,
        pin: bool = False,
    ) -> MemoryRecord | None:
        """ثبت یک یادداشت؛ اگر عیناً قبلاً بوده همان رکورد تقویت می‌شود.

        Args:
            content: متن یادداشت (redact و truncate می‌شود).
            kind: نوع رکورد؛ ناشناخته → ``note``.
            tags: برچسب‌ها (حداکثر ۱۲ تا، هر کدام ۴۰ کاراکتر).
            source: نویسنده (``agent``/``user``/``tool:…``).
            confidence: اطمینان ۰..۱.
            pin: چسباندن رکورد (هرگز prune نمی‌شود).

        Returns:
            رکورد نهایی، یا ``None`` اگر حافظه خاموش/متن خالی بود.
        """
        text = str(content or "").strip()
        if not text or not self.enabled:
            return None
        clean_tags = [str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()][:12]
        with self._lock:
            draft = MemoryRecord(
                kind=kind,
                content=text,
                tags=clean_tags,
                source=str(source or "agent")[:60],
                confidence=max(0.0, min(1.0, float(confidence))),
                pinned=bool(pin),
            )
            existing = self._by_sha.get(draft.sha)
            if existing is not None:
                # تکرار: به‌روز می‌شود، نه اضافه (ضد تورم حافظه)
                existing.updated_at = time.time()
                existing.confidence = max(existing.confidence, draft.confidence)
                existing.pinned = existing.pinned or draft.pinned
                for tag in draft.tags:
                    if tag not in existing.tags:
                        existing.tags.append(tag)
                self.flush()
                return existing
            self._append_index(draft)
            self._prune_locked()
            self.flush()
            return draft

    def remember(self, content: str, **kwargs: Any) -> MemoryRecord | None:
        """میان‌بر فارسی‌پسندِ :meth:`add` (همان معنا)."""
        return self.add(content, **kwargs)

    def update(self, record_id: str, **changes: Any) -> MemoryRecord | None:
        """تغییر فیلدهای مجاز یک رکورد (``content`` هم redact می‌شود)."""
        allowed = {"content", "kind", "tags", "source", "confidence", "pinned"}
        with self._lock:
            record = self.find(record_id)
            if record is None:
                return None
            patch = {key: value for key, value in changes.items() if key in allowed and value is not None}
            if not patch:
                return record
            payload = {**record.as_dict(), **patch, "updated_at": time.time()}
            updated = MemoryRecord(**payload)
            self._by_sha.pop(record.sha, None)
            self._records[self._records.index(record)] = updated
            self._by_sha[updated.sha] = updated
            self.flush()
            return updated

    def forget(self, record_id: str) -> bool:
        """حذف یک رکورد (با پیشوند id هم کار می‌کند)."""
        with self._lock:
            record = self.find(record_id)
            if record is None:
                return False
            self._records.remove(record)
            self._by_sha.pop(record.sha, None)
            self.flush()
            return True

    def find(self, record_id: str) -> MemoryRecord | None:
        """جست‌وجو با id کامل یا پیشوند یکتا."""
        wanted = str(record_id or "").strip()
        if not wanted:
            return None
        exact = next((item for item in self._records if item.id == wanted), None)
        if exact is not None:
            return exact
        prefix = [item for item in self._records if item.id.startswith(wanted)]
        return prefix[0] if len(prefix) == 1 else None

    def all(self, *, kinds: Iterable[str] | None = None) -> list[MemoryRecord]:
        """رکوردها، مرتب از تازه‌ترین."""
        wanted = {str(kind).lower() for kind in kinds} if kinds else None
        items = [item for item in self._records if wanted is None or item.kind in wanted]
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    def search(self, query: str, *, kinds: Iterable[str] | None = None, limit: int = 8) -> list[MemoryRecord]:
        """جست‌وجوی رتبه‌بندی‌شده (تطبیق واژه + تازگی + pin + دفعات استفاده).

        Returns:
            رکوردها به ترتیب امتیاز نزولی؛ اگر query خالی باشد ``recent``.
        """
        tokens = _tokens(query or "")
        wanted = {str(kind).lower() for kind in kinds} if kinds else None
        pool = [item for item in self._records if wanted is None or item.kind in wanted]
        if not tokens:
            return sorted(pool, key=lambda item: item.updated_at, reverse=True)[: max(1, int(limit))]
        scored = [(item.score(tokens), item) for item in pool]
        scored = [pair for pair in scored if pair[0] > 0.9]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[: max(1, int(limit))]]

    def touch(self, records: Iterable[MemoryRecord]) -> None:
        """ثبت «مورد استفاده قرار گرفت» (برای رتبه‌بندی و گزارش)."""
        changed = False
        with self._lock:
            for record in records:
                record.hits += 1
                changed = True
            if changed:
                self.flush()

    def _prune_locked(self) -> int:
        """حذف قدیمی‌ترین‌های غیر-pinned تا سقف ``max_records``."""
        if len(self._records) <= self.max_records:
            return 0
        ordered = sorted(self._records, key=lambda item: (item.pinned, item.updated_at))
        drop = len(self._records) - self.max_records
        removed = 0
        for candidate in ordered:
            if removed >= drop:
                break
            if candidate.pinned:
                continue
            self._records.remove(candidate)
            self._by_sha.pop(candidate.sha, None)
            removed += 1
        return removed

    # ------------------------------------------------------------------ خروجی‌ها

    def context_block(self, *, max_chars: int = 4000, query: str = "") -> str:
        """متنی که به system prompt اضافه می‌شود (خالی یعنی چیزی نیست).

        ترتیب: preference/pinned اول (این‌ها «شخصیت» کاربرند)، بعد procedure و
        fact های مرتبط با query، و در آخر plan جاری. سقف کاراکتر رعایت می‌شود تا
        هزینه‌ی توکن کنترل‌شده بماند.
        """
        if not self.enabled or max_chars <= 0 or not self._records:
            return ""
        tokens = _tokens(query)
        priority = {"preference": 0, "procedure": 1, "decision": 2, "fact": 3, "plan": 4, "note": 5}

        def rank(item: MemoryRecord) -> tuple[int, float]:
            score = item.score(tokens) if tokens else (item.updated_at / 1e9)
            return (priority.get(item.kind, 9), -score)

        ordered = sorted(self._records, key=rank)
        header = "## Long-term memory (auto-loaded; don't repeat it verbatim to the user)"
        lines: list[str] = [header]
        used = len(header)
        for index, item in enumerate(ordered):
            flags = item.kind + ("|pinned" if item.pinned else "") + (f"|{item.tags[0]}" if item.tags else "")
            line = f"- [{flags}] {item.content}".replace("\n", " ")
            if used + len(line) + 1 > max_chars:
                skipped = len(ordered) - index
                lines.append(f"- …{skipped} more record(s) — call the memory_search tool when you need them")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    def stats(self) -> dict[str, Any]:
        """خلاصه برای پنل/CLI."""
        by_kind: dict[str, int] = {}
        for item in self._records:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        return {
            "enabled": self.enabled,
            "path": str(self.path) if self.path else None,
            "records": len(self._records),
            "pinned": sum(1 for item in self._records if item.pinned),
            "by_kind": by_kind,
            "total_hits": sum(item.hits for item in self._records),
            "max_records": self.max_records,
            "last_updated": max((item.updated_at for item in self._records), default=0.0),
        }

    def recent(self, limit: int = 10, *, kinds: Iterable[str] | None = None) -> list[MemoryRecord]:
        """تازه‌ترین رکوردها (برای «دیروز چه کردم؟»)."""
        return self.all(kinds=kinds)[: max(1, int(limit))]

    def plans(self, limit: int = 10) -> list[MemoryRecord]:
        """برنامه‌های باز (kind=``plan``) — خوراک پنل «Next steps»."""
        return self.all(kinds=("plan",))[: max(1, int(limit))]

    # ------------------------------------------------------------------ capture / exchange

    def capture_run(
        self,
        result: Any,
        *,
        prompt: str = "",
        profile: str = "",
        min_iterations: int = 1,
    ) -> MemoryRecord | None:
        """پس از یک اجرا، یک یادداشت خودکار ثبت می‌کند (خروجی کار، ابزارها، زمان).

        Only meaningful runs are stored: یک گفت‌وگوی بدون ابزار که فقط یک «سلام»
        بوده ارزش حافظه ندارد (سقف بی‌معنی پر می‌شود).
        """
        if not self.enabled:
            return None
        iterations = int(getattr(result, "iterations", 0) or 0)
        tools_used = list(getattr(result, "tool_names", []) or [])
        ok = bool(getattr(result, "ok", True))
        if not tools_used and iterations < max(1, int(min_iterations)):
            return None
        text = str(getattr(result, "text", "") or "").strip()
        error = str(getattr(result, "error", "") or "").strip()
        bits = [f"Task: {truncate_text(str(prompt or ''), 180, suffix='…')[0] or '(unspecified)'}"]
        if profile:
            bits.append(f"profile={profile}")
        bits.append(f"outcome={'ok' if ok else 'failed'}")
        if tools_used:
            bits.append("tools=" + ", ".join(tools_used[:12]))
        bits.append(f"iterations={iterations}")
        duration = int(getattr(result, "duration_ms", 0) or 0)
        if duration:
            bits.append(f"{duration / 1000:.1f}s")
        if error:
            bits.append(f"error={error[:160]}")
        elif text:
            bits.append("answer=" + truncate_text(text, 240, suffix="…")[0].replace("\n", " "))
        return self.add(
            " · ".join(bits),
            kind="decision" if ok else "note",
            tags=[f"profile:{profile}"] if profile else None,
            source="agent:capture_run",
            confidence=0.55 if ok else 0.8,
        )

    def export_json(self, path: str | Path) -> int:
        """خروجی JSON (برای بکاپ/انتقال آگاهانه). تعداد رکورد برمی‌گرداند."""
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        records = [item.as_dict() for item in self.all()]
        payload = {"version": 1, "exported_at": time.time(), "records": records}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        with contextlib.suppress(OSError):
            target.chmod(0o600)
        return len(records)

    def import_json(self, path: str | Path) -> int:
        """ورودی JSON از فایل :meth:`export_json` (dedupe خودکار). تعداد افزوده‌شده."""
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(source)
        try:
            parsed: Any = json.loads(source.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise ValueError(f"'{source.name}' is not a readable memory export file ({type(exc).__name__})") from exc
        if not isinstance(parsed, (dict, list)):
            raise ValueError(f"'{source.name}' is not a valid memory export (expected a JSON object or list)")
        records: list[Any] = list(parsed.get("records", []) if isinstance(parsed, dict) else parsed)
        added = 0
        for entry in records:
            if not isinstance(entry, dict):
                continue
            before = len(self._records)
            self.add(
                str(entry.get("content", "")),
                kind=str(entry.get("kind", "note")),
                tags=list(entry.get("tags") or []),
                source=str(entry.get("source", "import")),
                confidence=float(entry.get("confidence", 0.7) or 0.7),
                pin=bool(entry.get("pinned", False)),
            )
            added += 1 if len(self._records) > before else 0
        return added

    def describe(self) -> str:
        """یک خط خلاصه (برای CLI)."""
        info = self.stats()
        if not info["enabled"]:
            return "memory: disabled"
        kinds = ", ".join(f"{key}={value}" for key, value in sorted(info["by_kind"].items()))
        return f"memory: {info['records']}/{info['max_records']} records ({kinds or 'empty'}) → {info['path']}"


def memory_for_config(config: Any) -> AgentMemory | None:
    """میان‌بر ماژولی: store مشترک برای یک config (``None`` اگر خاموش)."""
    return AgentMemory.for_config(config)


def reset_memory_cache() -> None:
    """پاک‌کردن کش store ها (تست/تست‌های موازی/تغییر path در runtime)."""
    AgentMemory.clear_cache()

"""بکاپ‌گیری، بازرسی، پیش‌نمایش و بازیابی وضعیت ایجنت.

Agent-Zero شش اندپوینت ``backup_*`` دارد (create/inspect/preview_grouped/
restore/restore_preview/test) و ما هیچ نداشتیم. این برای پروژه‌ای که
*حافظه‌ی بلندمدت* و *روتین‌های زمان‌بندی‌شده* دارد یک شکاف جدی است: بدون
بکاپ، یک ``memory.jsonl`` خراب یعنی از دست رفتن همه‌ی چیزهایی که ایجنت یاد
گرفته.

چه چیزی بکاپ می‌شود؟
--------------------
فقط حالتِ خودِ ایجنت، **نه** فایل‌های کاربر:

* ``memory/memory.jsonl`` — حافظه‌ی بلندمدت
* ``routines.json`` — روتین‌های زمان‌بندی‌شده
* ``notifications.jsonl`` — فید اعلان‌ها
* ``audit.jsonl`` — لاگ ممیزی
* ``keys.json`` — فقط اگر ``include_keys=True`` (پیش‌فرض **خیر**)
* ``activity.jsonl`` — گزارش فعالیت

هر بکاپ یک ``manifest.json`` با sha256 هر عضو دارد، بنابراین
:meth:`BackupManager.test` می‌تواند تشخیص دهد بکاپ سالم است یا نه — بدون
بازیابی کردنش.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.helpers import format_size
from src.utils.logger import get_logger

__all__ = ["BackupInfo", "BackupManager", "RestorePlan"]

logger = get_logger("core.backup")

SCHEMA_VERSION = 1

#: نام‌های منطقی → نام فایل داخل zip.
_MEMBER_ALIASES: dict[str, str] = {
    "memory": "memory/memory.jsonl",
    "routines": "routines.json",
    "notifications": "notifications.jsonl",
    "audit": "audit.jsonl",
    "activity": "activity.jsonl",
    "keys": "keys.json",
}


@dataclass
class BackupInfo:
    """متادیتای یک بکاپ."""

    name: str
    path: Path
    created_at: str = ""
    bytes: int = 0
    members: dict[str, str] = field(default_factory=dict)  # logical -> sha256
    sizes: dict[str, int] = field(default_factory=dict)
    note: str = ""
    include_keys: bool = False

    @property
    def human_size(self) -> str:
        """اندازه‌ی خوانا (مثلاً ``1.2 KiB``).

        property است نه فقط کلید ``as_dict``: CLI و لاگ‌ها هم به آن نیاز دارند
        و تکرار ``format_size(...)`` در چند جا یعنی جایی فراموش می‌شود.
        """
        return format_size(self.bytes)

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return {
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at,
            "bytes": self.bytes,
            "human_size": self.human_size,
            "members": self.members,
            "sizes": self.sizes,
            "note": self.note,
            "include_keys": self.include_keys,
        }


@dataclass
class RestorePlan:
    """نتیجه‌ی پیش‌نمایش بازیابی (dry-run)."""

    backup: str
    ok: bool = True
    reason: str = ""
    would_write: list[str] = field(default_factory=list)
    would_skip: list[str] = field(default_factory=list)
    would_overwrite: list[str] = field(default_factory=list)
    checksum_mismatch: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return {
            "backup": self.backup,
            "ok": self.ok,
            "reason": self.reason,
            "would_write": self.would_write,
            "would_skip": self.would_skip,
            "would_overwrite": self.would_overwrite,
            "checksum_mismatch": self.checksum_mismatch,
        }


def _sha256_file(path: Path) -> str:
    """هش یک فایل به‌صورت جریان‌یافته."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    """هش یک بایت‌رشته."""
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    """زمان حال UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BackupManager:
    """مدیر بکاپ‌های حالت ایجنت.

    نمونه::

        manager = BackupManager(store_dir, sources={"memory": memory_path, "routines": routines_path})
        info = manager.create(note="before upgrade")
        plan = manager.preview(info.name)
        manager.restore(info.name, confirm=True)
    """

    def __init__(
        self,
        directory: str | Path,
        sources: dict[str, str | Path] | None = None,
        *,
        keep: int = 20,
        source_provider: Callable[[], dict[str, str | Path]] | None = None,
    ) -> None:
        """Args:
        directory: پوشه‌ی نگهداری بکاپ‌ها.
        sources: نگاشت نام منطقی → مسیر فایل.
        keep: تعداد بکاپ نگه‌داشته‌شده (بیشتر ⇒ قدیمی‌ترها حذف).
        source_provider: تابعی که نگاشت منابع را *در لحظه* برمی‌گرداند.
        """
        self.directory = Path(directory).expanduser().resolve()
        self.sources = {name: Path(p).expanduser() for name, p in (sources or {}).items()}
        self.keep = max(1, int(keep))
        self._source_provider = source_provider
        #: منابعی که صریحاً با ``set_source`` ثبت شده‌اند. این‌ها بر خروجی
        #: provider اولویت دارند — وگرنه ``refresh_sources()`` در هر create
        #: منبعی را که فراخوان بیرون از ServiceHub دستی وصل کرده بود پاک می‌کرد.
        self._explicit: dict[str, Path] = {}
        self.directory.mkdir(parents=True, exist_ok=True)

    def refresh_sources(self) -> dict[str, Path]:
        """منابع را از provider بازخوانی می‌کند.

        چرا لازم است؟ چون فایل‌های وضعیت (حافظه، روتین‌ها، …) ممکن است *بعد از*
        ساخت manager به وجود بیایند — مثلاً اولین ``memory.jsonl`` پس از اولین
        اجرا نوشته می‌شود. اگر فهرست منابع را فقط در ``__init__`` snapshot
        بگیریم، بکاپ بی‌صدا ناقص می‌شود.
        """
        if self._source_provider is not None:
            merged = {name: Path(path).expanduser() for name, path in self._source_provider().items()}
            merged.update(self._explicit)  # صریح > provider
            self.sources = merged
        return self.sources

    # ------------------------------------------------------------------ helpers
    def set_source(self, name: str, path: str | Path) -> None:
        """یک منبع را صریحاً ثبت می‌کند (بر provider اولویت دارد)."""
        resolved = Path(path).expanduser()
        self.sources[name] = resolved
        self._explicit[name] = resolved

    def _target(self, name: str) -> Path:
        """مسیر محلی یک منبع منطقی."""
        if name not in self.sources:
            raise KeyError(f"unknown backup source '{name}' (known: {', '.join(sorted(self.sources))})")
        return self.sources[name]

    def _existing_targets(self) -> dict[str, Path]:
        """منابعی که فایلشان وجود دارد."""
        return {name: path for name, path in self.sources.items() if path.exists()}

    # ------------------------------------------------------------------ create
    def create(self, *, note: str = "", include_keys: bool = False, name: str | None = None) -> BackupInfo:
        """یک بکاپ می‌سازد.

        Args:
            note: یادداشت آزاد.
            include_keys: ``keys.json`` هم اضافه شود؟ (پیش‌فرض خیر)
            name: نام بکاپ (پیش‌فرض timestamp).

        Returns:
            :class:`BackupInfo`.

        Raises:
            ValueError: اگر هیچ منبعی موجود نباشد.
        """
        self.refresh_sources()
        available = self._existing_targets()
        if not include_keys:
            available.pop("keys", None)
        if not available:
            raise ValueError("nothing to back up: none of the configured sources exist")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_name = str(name or f"agent-hub-{stamp}").strip()
        if not backup_name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("backup name must be alphanumeric with - or _")
        path = self.directory / f"{backup_name}.zip"
        if path.exists():
            raise ValueError(f"backup '{backup_name}' already exists")

        members: dict[str, str] = {}
        sizes: dict[str, int] = {}
        tmp = self.directory / f".{backup_name}.zip.tmp"
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for logical, source in sorted(available.items()):
                    arcname = _MEMBER_ALIASES.get(logical, f"extra/{logical}")
                    data = source.read_bytes()
                    archive.writestr(arcname, data)
                    members[logical] = _sha256_bytes(data)
                    sizes[logical] = len(data)
                manifest = {
                    "schema": SCHEMA_VERSION,
                    "name": backup_name,
                    "created_at": _utc_now(),
                    "note": str(note or "")[:500],
                    "include_keys": bool(include_keys),
                    "members": members,
                    "sizes": sizes,
                }
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            tmp.replace(path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"cannot write backup: {exc}") from exc

        info = BackupInfo(
            name=backup_name,
            path=path,
            created_at=_utc_now(),
            bytes=path.stat().st_size,
            members=members,
            sizes=sizes,
            note=str(note or "")[:500],
            include_keys=bool(include_keys),
        )
        self._prune()
        return info

    def _prune(self) -> int:
        """بکاپ‌های قدیمی را تا سقف ``keep`` حذف می‌کند."""
        backups = self.list()
        if len(backups) <= self.keep:
            return 0
        removed = 0
        for info in backups[self.keep :]:
            try:
                info.path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("backup: cannot prune %s: %s", info.path, exc)
        return removed

    # ------------------------------------------------------------------ read
    def list(self) -> list[BackupInfo]:
        """فهرست بکاپ‌ها، جدیدترین اول."""
        out: list[BackupInfo] = []
        for path in sorted(self.directory.glob("*.zip")):
            info = self._read_manifest(path)
            if info is not None:
                out.append(info)
        out.sort(key=lambda item: item.created_at or item.name, reverse=True)
        return out

    def _read_manifest(self, path: Path) -> BackupInfo | None:
        """manifest یک zip را می‌خواند."""
        try:
            with zipfile.ZipFile(path) as archive:
                raw = archive.read("manifest.json")
            data = json.loads(raw.decode("utf-8"))
        except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            logger.warning("backup: %s has no readable manifest: %s", path.name, exc)
            return None
        members = data.get("members") if isinstance(data.get("members"), dict) else {}
        sizes = data.get("sizes") if isinstance(data.get("sizes"), dict) else {}
        return BackupInfo(
            name=str(data.get("name") or path.stem),
            path=path,
            created_at=str(data.get("created_at") or ""),
            bytes=path.stat().st_size if path.exists() else 0,
            members={str(k): str(v) for k, v in members.items()},
            sizes={str(k): int(v) for k, v in sizes.items()},
            note=str(data.get("note") or ""),
            include_keys=bool(data.get("include_keys", False)),
        )

    def find(self, name: str) -> BackupInfo | None:
        """یافتن با نام (با یا بدون ``.zip``)."""
        wanted = name if name.endswith(".zip") else f"{name}.zip"
        path = self.directory / wanted
        if not path.exists():
            return None
        return self._read_manifest(path)

    def inspect(self, name: str) -> dict[str, Any]:
        """جزئیات یک بکاپ + نتیجه‌ی آزمون سلامت.

        Raises:
            KeyError: اگر بکاپ پیدا نشود.
        """
        info = self.find(name)
        if info is None:
            raise KeyError(f"backup '{name}' not found")
        verdict = self.test(name)
        return {**info.as_dict(), "integrity": verdict}

    def test(self, name: str) -> dict[str, Any]:
        """سلامت بکاپ را بدون بازیابی بررسی می‌کند.

        Returns:
            ``{"ok": bool, "reason": str, "checked": int, "mismatch": [...]}``.
        """
        info = self.find(name)
        if info is None:
            return {"ok": False, "reason": f"backup '{name}' not found", "checked": 0, "mismatch": []}
        mismatch: list[str] = []
        checked = 0
        try:
            with zipfile.ZipFile(info.path) as archive:
                bad = archive.testzip()
                if bad is not None:
                    return {"ok": False, "reason": f"corrupt member: {bad}", "checked": 0, "mismatch": [bad]}
                reverse = {arc: logical for logical, arc in _MEMBER_ALIASES.items()}
                for arc in archive.namelist():
                    if arc == "manifest.json":
                        continue
                    logical = reverse.get(arc)
                    if logical is None:
                        continue
                    data = archive.read(arc)
                    checked += 1
                    expected = info.members.get(logical)
                    if expected and _sha256_bytes(data) != expected:
                        mismatch.append(logical)
        except (OSError, zipfile.BadZipFile) as exc:
            return {"ok": False, "reason": f"unreadable: {exc}", "checked": checked, "mismatch": mismatch}
        return {
            "ok": not mismatch,
            "reason": "checksum mismatch: " + ", ".join(mismatch) if mismatch else "all members verified",
            "checked": checked,
            "mismatch": mismatch,
        }

    # ------------------------------------------------------------------ restore
    def preview(self, name: str, *, only: Iterable[str] | None = None) -> RestorePlan:
        """پیش‌نمایش بازیابی (dry-run) — هیچ فایلی نوشته نمی‌شود.

        Args:
            name: نام بکاپ.
            only: فقط این منابع منطقی.

        Returns:
            :class:`RestorePlan`.
        """
        plan = RestorePlan(backup=name)
        info = self.find(name)
        if info is None:
            plan.ok = False
            plan.reason = f"backup '{name}' not found"
            return plan
        verdict = self.test(name)
        if not verdict["ok"]:
            plan.ok = False
            plan.reason = verdict["reason"]
            plan.checksum_mismatch = list(verdict["mismatch"])
            return plan
        wanted = set(only) if only else set(info.members)
        try:
            with zipfile.ZipFile(info.path) as archive:
                present = set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            plan.ok = False
            plan.reason = f"unreadable: {exc}"
            return plan
        reverse = {arc: logical for logical, arc in _MEMBER_ALIASES.items()}
        for arc in sorted(present):
            logical = reverse.get(arc)
            if logical is None or logical not in wanted:
                continue
            if logical not in self.sources:
                plan.would_skip.append(f"{logical} (no destination configured)")
                continue
            target = self.sources[logical]
            (plan.would_overwrite if target.exists() else plan.would_write).append(str(target))
        if not plan.would_write and not plan.would_overwrite:
            plan.ok = False
            plan.reason = "nothing to restore from this backup"
        return plan

    def restore(
        self,
        name: str,
        *,
        confirm: bool = False,
        only: Iterable[str] | None = None,
        safety_backup: bool = True,
    ) -> dict[str, Any]:
        """بازیابی یک بکاپ.

        Args:
            name: نام بکاپ.
            confirm: **باید** ``True`` باشد؛ وگرنه فقط پیش‌نمایش برمی‌گردد.
            only: فقط این منابع.
            safety_backup: پیش از بازنویسی، از وضعیت فعلی یک بکاپ اضطراری بگیر.

        Returns:
            ``{"restored": [...], "skipped": [...], "safety_backup": str|None}``.

        Raises:
            ValueError: اگر بکاپ خراب باشد.
        """
        plan = self.preview(name, only=only)
        if not plan.ok:
            raise ValueError(plan.reason or "restore preview failed")
        if not confirm:
            return {
                "restored": [],
                "skipped": plan.would_write + plan.would_overwrite,
                "safety_backup": None,
                "dry_run": True,
                "plan": plan.as_dict(),
            }

        info = self.find(name)
        if info is None:  # pragma: no cover - preview قبلاً چک کرده
            raise ValueError(f"backup '{name}' disappeared")
        safety_name: str | None = None
        if safety_backup and self._existing_targets():
            try:
                safety_name = self.create(note=f"auto before restoring {name}").name
            except (ValueError, RuntimeError) as exc:
                logger.warning("backup: safety backup failed: %s", exc)

        wanted = set(only) if only else set(info.members)
        restored: list[str] = []
        skipped: list[str] = []
        reverse = {arc: logical for logical, arc in _MEMBER_ALIASES.items()}
        handle, tmp_name = tempfile.mkstemp(dir=str(self.directory), prefix=".restore-", suffix=".tmp")
        try:
            import os

            os.close(handle)
            with zipfile.ZipFile(info.path) as archive:
                for arc in sorted(archive.namelist()):
                    logical = reverse.get(arc)
                    if logical is None or logical not in wanted or logical not in self.sources:
                        continue
                    target = self.sources[logical]
                    data = archive.read(arc)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    Path(tmp_name).write_bytes(data)
                    Path(tmp_name).replace(target)
                    restored.append(str(target))
        except (OSError, zipfile.BadZipFile) as exc:
            Path(tmp_name).unlink(missing_ok=True)
            raise RuntimeError(f"restore failed: {exc}") from exc
        Path(tmp_name).unlink(missing_ok=True)
        return {"restored": restored, "skipped": skipped, "safety_backup": safety_name, "dry_run": False}

    # ------------------------------------------------------------------ export
    def export_json(self, path: str | Path) -> int:
        """فهرست بکاپ‌ها را به JSON می‌برد (برای گزارش)."""
        dest = Path(path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        items = [info.as_dict() for info in self.list()]
        dest.write_text(json.dumps({"backups": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(items)

    def remove(self, name: str) -> bool:
        """حذف یک بکاپ."""
        info = self.find(name)
        if info is None:
            return False
        try:
            info.path.unlink()
        except OSError as exc:
            logger.warning("backup: cannot remove %s: %s", info.path, exc)
            return False
        return True

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/backup``."""
        items = self.list()
        total = sum(item.bytes for item in items)
        return {
            "directory": str(self.directory),
            "count": len(items),
            "keep": self.keep,
            "total_bytes": total,
            "human_total": format_size(total),
            "sources": {name: str(path) for name, path in sorted(self.sources.items())},
            "backups": [item.as_dict() for item in items[:10]],
        }

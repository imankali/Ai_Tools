"""بسته‌های Skill: دستورالعمل‌های قابل‌حمل با ``SKILL.md``.

تفاوت skill و tool
------------------
*tool* یک تابع است (``terminal_run``). *skill* یک **دانش + روش** است: یک فایل
مارک‌داون با frontmatter که می‌گوید «برای انجام X این مرحله‌ها را برو و فقط از
این ابزارها استفاده کن». سه پروژه‌ی مرجع هر سه این را دارند:

* OpenClaw → ``src/skills`` (۱۳۸ فایل) + ClawHub
* Agent-Zero → ``skills_scan`` / ``skills_import`` / ``skills_import_preview``
* IronClaw → ``skills/`` + ``registry/``

قالب انتخابی عمداً همان قالب رایج ``SKILL.md`` است تا بسته‌های موجودِ اکوسیستم
بدون تغییر قابل استفاده باشند::

    ---
    name: postgres-backup
    description: Take a consistent logical backup of a PostgreSQL database.
    version: 1.0.0
    tags: [db, ops]
    allowed_tools: [terminal_run, read_file]
    ---
    # PostgreSQL backup
    1. …

نکته‌ی ایمنی: محتوای skill یک *دستورالعمل* است که وارد prompt می‌شود؛ پس
پیش از ورود، از :mod:`src.utils.injection` عبور می‌کند و skill آلوده رد می‌شود.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.utils.injection import PromptInjectionScanner
from src.utils.logger import get_logger

__all__ = ["Skill", "SkillLibrary", "parse_skill_file"]

logger = get_logger("core.skills")

SKILL_FILENAME = "SKILL.md"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


class Skill(BaseModel):
    """یک بسته‌ی skill.

    Attributes:
        name: شناسه (kebab-case).
        description: یک جمله که کی استفاده شود.
        version: نسخه.
        tags: برچسب‌ها برای جست‌وجو.
        allowed_tools: اگر غیرخالی، skill فقط باید از این ابزارها استفاده کند.
        body: بدنه‌ی مارک‌داون (دستورالعمل).
        source: مسیر فایل مبدأ.
        enabled: فعال؟
        checksum: sha256 محتوا (برای تشخیص تغییر).
    """

    name: str
    description: str = ""
    version: str = "0.0.0"
    tags: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    body: str = ""
    source: str = ""
    enabled: bool = True
    checksum: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> str:
        """نام باید kebab-case و امن باشد (چون بخشی از مسیر است)."""
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-")
        # «..» در هر کجای نام یعنی احتمال path traversal؛ حتی اگر «/» حذف شده باشد.
        if not text or "/" in text or ".." in text or text in {".", "-"}:
            raise ValueError("skill name must be a safe kebab-case identifier without '..'")
        return text[:80]

    @field_validator("tags", "allowed_tools", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        """رشته‌ی کاماجدا را هم می‌پذیرد."""
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(value)

    @property
    def size(self) -> int:
        """طول بدنه."""
        return len(self.body)

    def allows_tool(self, tool_name: str) -> bool:
        """آیا این ابزار در دامنه‌ی skill مجاز است؟"""
        if not self.allowed_tools:
            return True
        return tool_name in self.allowed_tools

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize (بدون بدنه‌ی کامل)."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "tags": self.tags,
            "allowed_tools": self.allowed_tools,
            "source": self.source,
            "enabled": self.enabled,
            "checksum": self.checksum[:16],
            "size": self.size,
        }


@dataclass
class ParseResult:
    """نتیجه‌ی پارس یک فایل."""

    skill: Skill | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        """آیا پارس موفق بود؟"""
        return self.skill is not None


def _parse_scalar(text: str) -> Any:
    """پارس یک مقدار ساده‌ی YAML (رشته/عدد/bool/لیست درون‌خطی)."""
    value = text.strip()
    if not value:
        return ""
    if value[0] in "\"'" and value[-1] == value[0] and len(value) >= 2:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in inner.split(",")]
    low = value.lower()
    if low in {"true", "yes"}:
        return True
    if low in {"false", "no"}:
        return False
    if low in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """پارس frontmatter ساده (بدون وابستگی به PyYAML).

    پشتیبانی: ``key: value``، لیست درون‌خطی ``[a, b]`` و لیست بلوکی ``- item``.
    این عمداً یک زیرمجموعه است — نه یک پارسر YAML کامل.

    Returns:
        ``(metadata, body)``.
    """
    raw = str(text or "").lstrip("\ufeff")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    head, body = match.group(1), match.group(2)
    meta: dict[str, Any] = {}
    current_key: str | None = None
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_key is not None:
            existing = meta.get(current_key)
            if not isinstance(existing, list):
                existing = []
                meta[current_key] = existing
            existing.append(_parse_scalar(stripped[2:]))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        if not value.strip():
            meta[key] = []
            current_key = key
            continue
        meta[key] = _parse_scalar(value)
        current_key = None
    return meta, body.strip()


def parse_skill_file(path: str | Path, *, scanner: PromptInjectionScanner | None = None) -> ParseResult:
    """یک فایل ``SKILL.md`` را می‌خواند و به :class:`Skill` تبدیل می‌کند.

    Args:
        path: مسیر فایل.
        scanner: در صورت ارائه، بدنه اسکن می‌شود و skill آلوده رد می‌شود.

    Returns:
        :class:`ParseResult`.
    """
    file_path = Path(path).expanduser()
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ParseResult(error=f"cannot read: {exc}")
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or file_path.parent.name or "").strip()
    if not name:
        return ParseResult(error="missing 'name' in frontmatter")
    import hashlib

    try:
        skill = Skill(
            name=name,
            description=str(meta.get("description") or "")[:500],
            version=str(meta.get("version") or "0.0.0")[:40],
            tags=meta.get("tags") or [],
            allowed_tools=meta.get("allowed_tools") or [],
            body=body[:40000],
            source=str(file_path.resolve()),
            checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    except Exception as exc:  # noqa: BLE001 - pydantic validation
        return ParseResult(error=f"invalid skill metadata: {exc}")
    if scanner is not None:
        report = scanner.scan(f"{skill.description}\n{skill.body}")
        if report.at_least("high"):
            return ParseResult(error=f"rejected by injection scanner: {report.summary()}")
    return ParseResult(skill=skill)


@dataclass
class _LibraryState:
    """وضعیت فعال/غیرفعال که روی دیسک نگه داشته می‌شود."""

    disabled: set[str] = field(default_factory=set)


class SkillLibrary:
    """مجموعه‌ی skillها با کشف خودکار از چند پوشه.

    نمونه::

        library = SkillLibrary([Path.home() / ".universal-agent-hub" / "skills"])
        library.scan()
        prompt_block = library.context_block(query="backup postgres")
    """

    def __init__(
        self,
        directories: Sequence[str | Path] | None = None,
        *,
        scanner: PromptInjectionScanner | None = None,
        state_file: str | Path | None = None,
        max_context_chars: int = 6000,
    ) -> None:
        """Args:
        directories: پوشه‌هایی که در آن‌ها ``SKILL.md`` جست‌وجو می‌شود.
        scanner: اسکنر injection (پیش‌فرض یک نمونه‌ی تازه).
        state_file: فایل JSON برای وضعیت فعال/غیرفعال.
        max_context_chars: سقف متن تزریق‌شده به prompt.
        """
        self.directories = [Path(d).expanduser() for d in (directories or [])]
        self.scanner = scanner or PromptInjectionScanner()
        # نکته: state *داخل* اولین پوشه‌ی skill می‌نشیند، نه در والدش.
        # اگر در والد باشد، از اسکوپ پیکربندی بیرون می‌زند و چند نصب مختلف
        # (یا چند تست) ناخواسته وضعیت همدیگر را می‌بینند.
        self.state_file = (
            Path(state_file).expanduser()
            if state_file
            else (self.directories[0] / "skills-state.json" if self.directories else None)
        )
        self.max_context_chars = max(200, int(max_context_chars))
        self._skills: dict[str, Skill] = {}
        self._errors: dict[str, str] = {}
        self._state = _LibraryState()
        self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        """وضعیت ذخیره‌شده را می‌خواند."""
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self._state.disabled = {str(name) for name in data.get("disabled", [])}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skills: cannot read state %s: %s", self.state_file, exc)

    def _save_state(self) -> None:
        """وضعیت را می‌نویسد."""
        if self.state_file is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"disabled": sorted(self._state.disabled)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("skills: cannot write state %s: %s", self.state_file, exc)

    # ------------------------------------------------------------------ discovery
    def scan(self, *, reset: bool = True) -> list[str]:
        """پوشه‌ها را می‌گرد و skillها را بار می‌کند.

        Args:
            reset: اگر ``True``، مجموعه‌ی فعلی دور ریخته می‌شود.

        Returns:
            نام skillهای بارگذاری‌شده.
        """
        if reset:
            self._skills.clear()
            self._errors.clear()
        for directory in self.directories:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob(SKILL_FILENAME)):
                result = parse_skill_file(path, scanner=self.scanner)
                if result.skill is None:
                    self._errors[str(path)] = result.error
                    logger.warning("skills: skipping %s: %s", path, result.error)
                    continue
                result.skill.enabled = result.skill.name not in self._state.disabled
                self._skills[result.skill.name] = result.skill
        return sorted(self._skills)

    def load_one(self, path: str | Path) -> Skill | None:
        """یک فایل مشخص را بار می‌کند (بدون اسکن کل پوشه)."""
        result = parse_skill_file(path, scanner=self.scanner)
        if result.skill is None:
            self._errors[str(path)] = result.error
            return None
        self._skills[result.skill.name] = result.skill
        return result.skill

    def import_skill(self, source: str | Path, *, overwrite: bool = False) -> Skill:
        """یک skill را از مسیری بیرون به کتابخانه وارد می‌کند.

        Args:
            source: فایل ``SKILL.md`` یا پوشه‌ی حاوی آن.
            overwrite: اگر ``True``، نسخه‌ی موجود بازنویسی می‌شود.

        Returns:
            skill واردشده.

        Raises:
            ValueError: اگر پارس نشود یا بدون ``overwrite`` تکراری باشد.
            OSError: اگر کپی فایل شکست بخورد.
        """
        src = Path(source).expanduser()
        skill_file = src if src.is_file() else src / SKILL_FILENAME
        result = parse_skill_file(skill_file, scanner=self.scanner)
        if result.skill is None:
            raise ValueError(result.error or "unparsable skill")
        skill = result.skill
        if skill.name in self._skills and not overwrite:
            raise ValueError(f"skill '{skill.name}' already exists (pass overwrite=True)")
        target_root = self.directories[0] if self.directories else Path.cwd() / "skills"
        target_dir = target_root / skill.name
        target_file = target_dir / SKILL_FILENAME
        if target_file.exists() and not overwrite:
            raise ValueError(f"{target_file} already exists")
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_file, target_file)
        # فایل‌های همراه (assets) هم کپی می‌شوند، ولی بدون خروج از مبدأ.
        if src.is_dir():
            for extra in sorted(src.rglob("*")):
                if extra.is_file() and extra.name != SKILL_FILENAME:
                    rel = extra.relative_to(src)
                    dest = target_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(extra, dest)
        skill.source = str(target_file.resolve())
        self._skills[skill.name] = skill
        return skill

    # ------------------------------------------------------------------ access
    def get(self, name: str) -> Skill | None:
        """یافتن با نام."""
        return self._skills.get(name)

    def all(self, *, include_disabled: bool = True) -> list[Skill]:
        """همه‌ی skillها."""
        items = list(self._skills.values())
        if not include_disabled:
            items = [s for s in items if s.enabled]
        return sorted(items, key=lambda s: s.name)

    def enabled(self) -> list[Skill]:
        """فقط فعال‌ها."""
        return self.all(include_disabled=False)

    def __len__(self) -> int:
        """تعداد skillها."""
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        """آیا این skill وجود دارد؟"""
        return str(name) in self._skills

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """فعال/غیرفعال کردن و ذخیره‌ی وضعیت."""
        skill = self._skills.get(name)
        if skill is None:
            return False
        skill.enabled = bool(enabled)
        if skill.enabled:
            self._state.disabled.discard(name)
        else:
            self._state.disabled.add(name)
        self._save_state()
        return True

    def remove(self, name: str, *, delete_files: bool = False) -> bool:
        """حذف از کتابخانه (و اختیاری از دیسک)."""
        skill = self._skills.pop(name, None)
        if skill is None:
            return False
        self._state.disabled.discard(name)
        self._save_state()
        if delete_files and skill.source:
            parent = Path(skill.source).parent
            try:
                if parent.name == name:
                    shutil.rmtree(parent, ignore_errors=True)
                else:
                    Path(skill.source).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("skills: cannot delete %s: %s", skill.source, exc)
        return True

    def search(self, query: str, *, limit: int = 5) -> list[Skill]:
        """جست‌وجوی ساده در نام/توضیح/برچسب.

        Args:
            query: متن پرس‌وجو.
            limit: سقف نتیجه.
        """
        text = str(query or "").casefold().strip()
        if not text:
            return self.enabled()[: max(1, limit)]
        tokens = [t for t in re.split(r"\s+", text) if t]
        scored: list[tuple[int, Skill]] = []
        for skill in self.enabled():
            haystack = " ".join([skill.name, skill.description, " ".join(skill.tags)]).casefold()
            score = sum(3 if token in skill.name.casefold() else 1 for token in tokens if token in haystack)
            if score:
                scored.append((score, skill))
        scored.sort(key=lambda kv: (-kv[0], kv[1].name))
        return [skill for _, skill in scored[: max(1, limit)]]

    def context_block(self, *, query: str = "", max_chars: int | None = None) -> str:
        """متنی که وارد system prompt می‌شود.

        اگر ``query`` داده شود، فقط skillهای مرتبط می‌آیند؛ وگرنه فهرست خلاصه
        (نام + توضیح) می‌آید تا مدل بداند چه چیزهایی در دسترس است و در صورت
        نیاز کاملش را بخواهد.

        Args:
            query: پرس‌وجوی فعلی.
            max_chars: سقف طول.

        Returns:
            بلوک متنی (خالی اگر skill فعالی نیست).
        """
        cap = max_chars or self.max_context_chars
        active = self.enabled()
        if not active:
            return ""
        selected = self.search(query, limit=3) if query else []
        parts: list[str] = ["<available_skills>"]
        if selected:
            for skill in selected:
                parts.append(f"## skill: {skill.name} (v{skill.version})")
                parts.append(skill.description)
                if skill.allowed_tools:
                    parts.append(f"tools: {', '.join(skill.allowed_tools)}")
                parts.append(skill.body.strip())
                if sum(len(p) for p in parts) > cap:
                    break
        else:
            parts.append("Load a skill by name when its description matches the task.")
            for skill in active:
                line = f"- {skill.name}: {skill.description}"
                if sum(len(p) for p in parts) + len(line) > cap:
                    break
                parts.append(line)
        parts.append("</available_skills>")
        return "\n".join(parts)

    def skill_body(self, name: str) -> str:
        """بدنه‌ی کامل یک skill (وقتی مدل درخواستش را می‌کند)."""
        skill = self._skills.get(name)
        if skill is None or not skill.enabled:
            return ""
        return skill.body

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/skills``."""
        return {
            "directories": [str(d) for d in self.directories],
            "count": len(self._skills),
            "enabled": len(self.enabled()),
            "skills": [s.as_dict() for s in self.all()],
            "errors": dict(self._errors),
        }

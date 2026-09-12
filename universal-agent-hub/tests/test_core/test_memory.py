"""تست حافظه‌ی بلندمدت (:mod:`src.core.memory`).

هیچ مسیری بیرون از ``tmp_path`` نوشته نمی‌شود؛ fixture مربوطه کش سراسری را هم
پاک می‌کند تا تست‌ها به هم وابسته نشوند.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.config import Config
from src.core.memory import (
    MEMORY_KINDS,
    AgentMemory,
    MemoryRecord,
    memory_for_config,
    reset_memory_cache,
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    """کش storeها قبل و بعد از هر تست خالی باشد."""
    reset_memory_cache()
    yield
    reset_memory_cache()


def store(tmp_path: Path, **kwargs: Any) -> AgentMemory:
    """یک store با فایل موقت."""
    return AgentMemory(tmp_path / "memory.jsonl", **kwargs)


# ---------------------------------------------------------------------------
# نوشتن و dedupe
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# جست‌وجوی برداری / ترکیبی (G05)
# ---------------------------------------------------------------------------
def test_search_mode_keyword_is_unchanged_default(tmp_path: Path) -> None:
    """``mode`` پیش‌فرض ``keyword`` است؛ رفتار قبلی نباید عوض شود."""
    memory = store(tmp_path)
    memory.add("deploy runs scripts/deploy.sh on prod1", kind="procedure")
    memory.add("prefers concise Persian answers", kind="preference")

    assert [r.content for r in memory.search("deploy", limit=5)] == [
        r.content for r in memory.search("deploy", limit=5, mode="keyword")
    ]


def test_search_mode_unknown_falls_back_to_keyword(tmp_path: Path) -> None:
    """مقدار ناشناخته ⇒ ``keyword``؛ هرگز استثنا، هرگز رفتار بی‌صدا متفاوت."""
    memory = store(tmp_path)
    memory.add("deploy runs scripts/deploy.sh on prod1", kind="procedure")
    assert [r.id for r in memory.search("deploy", mode="nonsense")] == [
        r.id for r in memory.search("deploy", mode="keyword")
    ]


def test_hybrid_ranks_semantic_match_first(tmp_path: Path) -> None:
    """hybrid باید رکورد مرتبط را *اول* بیاورد، جایی که keyword اشتباه رتبه می‌دهد.

    این کلِ دلیلِ وجودِ کانال برداری است. توجه: ``MemoryRecord.score`` تازگی را
    هم جمع می‌کند، پس رکوردهای تازه حتی با «صفر» واژه‌ی مشترک امتیاز بالایی
    می‌گیرند و keyword آن‌ها را بالا می‌آورد. کانال لغویِ RRF عمداً از
    ``_lexical_overlap`` ساخته می‌شود نه از ``score`` — وگرنه hybrid از vector
    بدتر می‌شد (این با آزمون دستی روی همین داده‌ها دیده و اصلاح شد).
    """
    memory = store(tmp_path)
    memory.add("database migration checklist for the webshop", kind="procedure")
    memory.add("prefers concise Persian answers", kind="preference")
    memory.add("the webshop runs on prod1 and prod2", kind="procedure")
    memory.add("rotate staging credentials every week", kind="procedure")
    memory.add("nightly report is emailed to the team", kind="note")

    query = "migrations of databases"
    top = {mode: memory.search(query, limit=2, mode=mode)[0].content for mode in ("keyword", "vector", "hybrid")}

    assert top["hybrid"] == "database migration checklist for the webshop"
    assert top["vector"] == "database migration checklist for the webshop"
    assert top["keyword"] != top["hybrid"]  # کانال برداری واقعاً چیزی اضافه کرده است


def test_hybrid_beats_vector_when_words_do_match(tmp_path: Path) -> None:
    """وقتی واژه‌ها *دقیقاً* می‌خورند، hybrid باید همان را نگه دارد.

    محافظت در برابر جهتِ خطای دیگر: اگر وزن کانال لغوی صفر بود، hybrid
    در ساده‌ترین حالت هم نتیجه‌ی درست را از دست می‌داد.
    """
    memory = store(tmp_path)
    memory.add("database migration checklist for the webshop", kind="procedure")
    memory.add("prefers concise Persian answers", kind="preference")
    memory.add("rotate staging credentials every week", kind="procedure")

    query = "database migration checklist"
    expected = "database migration checklist for the webshop"
    assert memory.search(query, limit=1, mode="keyword")[0].content == expected
    assert memory.search(query, limit=1, mode="hybrid")[0].content == expected


def test_vector_index_invalidates_after_forget(tmp_path: Path) -> None:
    """رگرسیون: کش شاخص برداری نباید بعد از حذف رکورد کهنه بماند.

    کش با مقایسه‌ی «مجموعه‌ی idها» باطل می‌شود، نه با شمارنده‌ی mutation —
    چون رکوردها از چند مسیر (add/forget/import/prune) تغییر می‌کنند.
    """
    memory = store(tmp_path)
    keep = memory.add("database backup job runs every night on prod1", kind="procedure")
    drop = memory.add("rotate the staging database password weekly", kind="procedure")

    assert memory.search("database password", mode="vector", limit=2)[0].id == drop.id

    memory.forget(drop.id)
    results = memory.search("database password", mode="vector", limit=2)
    # اگر کش کهنه بماند، ``drop`` همچنان برمی‌گردد (و بعد KeyError می‌دهد).
    assert [r.id for r in results] == [keep.id]

    # و جهتِ مقابل: وقتی هیچ رکورد مرتبطی نمانده، vector به‌درستی خالی می‌دهد —
    # برخلاف keyword که با recency رکورد بی‌ربط را بالا می‌آورد.
    memory.forget(keep.id)
    assert memory.search("zebra quarantine", mode="vector", limit=2) == []


def test_hybrid_respects_kinds_filter(tmp_path: Path) -> None:
    """فیلتر ``kinds`` در هر سه حالت باید یکسان عمل کند."""
    memory = store(tmp_path)
    memory.add("database migration checklist", kind="procedure")
    memory.add("database password rotation preference", kind="preference")

    for mode in ("keyword", "vector", "hybrid"):
        results = memory.search("database", kinds=["preference"], mode=mode, limit=5)
        assert results and all(r.kind == "preference" for r in results), mode


def test_add_returns_record_and_persists(tmp_path: Path) -> None:
    """رکورد نوشته و روی دیسک می‌ماند (حالت فایل امن)."""
    memory = store(tmp_path)
    record = memory.add("deploy runs scripts/deploy.sh on prod1", kind="procedure", tags=["project:webshop"])
    assert isinstance(record, MemoryRecord)
    assert record.kind == "procedure" and record.tags == ["project:webshop"]
    assert memory.stats()["records"] == 1
    reloaded = store(tmp_path)
    assert [item.content for item in reloaded.all()] == ["deploy runs scripts/deploy.sh on prod1"]
    if not _is_windows():
        assert (tmp_path / "memory.jsonl").stat().st_mode & 0o777 == 0o600


def test_same_content_updates_instead_of_duplicating(tmp_path: Path) -> None:
    """متن یکسان رکورد جدید نمی‌سازد؛ فقط امتیاز/برچسب‌ها تقویت می‌شوند."""
    memory = store(tmp_path)
    first = memory.add("user prefers Persian answers", kind="preference", confidence=0.4)
    again = memory.add("user prefers Persian answers", kind="preference", tags=["lang:fa"], confidence=0.9)
    assert memory.stats()["records"] == 1
    current = memory.find(first.id)
    assert current is not None and again is not None
    assert current.confidence == pytest.approx(0.9)
    assert "lang:fa" in current.tags
    assert current.hits >= first.hits


def test_empty_and_whitespace_content_rejected(tmp_path: Path) -> None:
    """یادداشت خالی ارزش ماندن ندارد."""
    memory = store(tmp_path)
    assert memory.add("   ") is None
    assert memory.add("") is None
    assert memory.stats()["records"] == 0


def test_secrets_are_redacted_on_write(tmp_path: Path) -> None:
    """کلید API هرگز در حافظه ذخیره نمی‌شود."""
    memory = store(tmp_path)
    record = memory.add("the key is sk-abcdefghijklmnopqrstuvwxyz123456", kind="note")
    assert record is not None
    assert "abcdefghijklmnopqrstuvwxyz" not in record.content
    raw = (tmp_path / "memory.jsonl").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in raw


def test_unknown_kind_falls_back_to_note(tmp_path: Path) -> None:
    """kind ناشناخته مخزن را خراب نمی‌کند."""
    memory = store(tmp_path)
    record = memory.add("something", kind="gossip")
    assert record is not None and record.kind == "note"


def test_kinds_constants_match_model(tmp_path: Path) -> None:
    """هر kind مستند شده قابل ذخیره است."""
    memory = store(tmp_path)
    for index, kind in enumerate(MEMORY_KINDS):
        assert memory.add(f"note number {index}", kind=kind) is not None
    assert {item.kind for item in memory.all()} == set(MEMORY_KINDS)


# ---------------------------------------------------------------------------
# خواندن و جست‌وجو
# ---------------------------------------------------------------------------
def test_search_ranks_keyword_match_over_noise(tmp_path: Path) -> None:
    """جست‌وجو رکورد مرتبط را جلوتر می‌آورد."""
    memory = store(tmp_path)
    memory.add("the payment gateway needs a commercial code and tax docs", kind="procedure")
    memory.add("the weather in tehran is mild", kind="note")
    hits = memory.search("payment gateway documents")
    assert hits and "payment gateway" in hits[0].content


def test_search_filters_by_kind_and_empty_query_returns_recent(tmp_path: Path) -> None:
    """فیلتر kind کار می‌کند و کوئری خالی یعنی تازه‌ترین‌ها."""
    memory = store(tmp_path)
    memory.add("plan: apply for the gateway", kind="plan")
    memory.add("user likes brief answers", kind="preference")
    plans = memory.search("gateway", kinds=["plan"])
    assert [item.kind for item in plans] == ["plan"]
    assert {item.kind for item in memory.search("")} == {"plan", "preference"}


def test_pinned_records_carry_weight_and_survive_prune(tmp_path: Path) -> None:
    """رکورد pinned در رتبه‌بندی جلو می‌افتد و با prune حذف نمی‌شود."""
    memory = store(tmp_path, max_records=40)
    pinned = memory.add("core preference: never delete user data", kind="preference", pin=True)
    for index in range(60):
        memory.add(f"routine observation {index}", kind="note")
    assert pinned is not None
    context = memory.context_block(max_chars=2000)
    assert "never delete user data" in context
    assert memory.find(pinned.id) is not None
    assert memory.stats()["records"] <= 45
    assert memory.stats()["pinned"] == 1


def test_context_block_respects_char_budget(tmp_path: Path) -> None:
    """بلوک حافظه از سقف کاراکتر رد نمی‌شود و سرصفحه دارد."""
    memory = store(tmp_path)
    for index in range(30):
        memory.add("a fairly long note about deployment details and rollback " + str(index), kind="procedure")
    block = memory.context_block(max_chars=600)
    assert len(block) <= 700  # سرصفحه + خط‌ها
    assert "Long-term memory" in block
    assert memory.context_block(max_chars=0) == ""


def test_context_block_reorders_for_the_current_query(tmp_path: Path) -> None:
    """وقتی کوئری داده شود، رکوردهای مرتبط بالا می‌آیند."""
    memory = store(tmp_path)
    memory.add("gateway documents: commercial code, tax, enamad", kind="procedure")
    memory.add("unrelated note about fonts", kind="note")
    block = memory.context_block(max_chars=500, query="gateway documents")
    assert block.index("gateway documents") < block.index("fonts")


def test_touch_bumps_hits(tmp_path: Path) -> None:
    """استفاده‌ی واقعی از رکورد، شمارنده‌اش را بالا می‌برد."""
    memory = store(tmp_path)
    record = memory.add("the staging host is prod1", kind="fact")
    assert record is not None
    before = record.hits
    memory.touch([record])
    assert memory.find(record.id).hits == before + 1  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# به‌روزرسانی، حذف، پایش
# ---------------------------------------------------------------------------
def test_update_only_touches_allowed_fields(tmp_path: Path) -> None:
    """update فیلدهای مجاز را عوض می‌کند و id را دست‌نخوردنی می‌گذارد."""
    memory = store(tmp_path)
    record = memory.add("original text", kind="note", tags=["a"])
    assert record is not None
    updated = memory.update(record.id, content="edited text", kind="decision", id="hacked")
    assert updated is not None
    assert updated.content == "edited text" and updated.kind == "decision"
    assert updated.id == record.id
    assert memory.stats()["records"] == 1


def test_forget_removes_record(tmp_path: Path) -> None:
    """حذف رکورد از حافظه و از فایل."""
    memory = store(tmp_path)
    record = memory.add("temporary", kind="note")
    assert record is not None
    assert memory.forget(record.id) is True
    assert memory.forget("nope") is False
    assert memory.all() == []
    assert store(tmp_path).all() == []


def test_find_accepts_unique_id_prefix(tmp_path: Path) -> None:
    """find با پیشوند یکتای id هم کار می‌کند (راحت برای مدل)."""
    memory = store(tmp_path)
    record = memory.add("findable", kind="note")
    assert record is not None
    found = memory.find(record.id[:6])
    assert found is not None and found.id == record.id


# ---------------------------------------------------------------------------
# capture_run و برنامه‌ها
# ---------------------------------------------------------------------------
def test_capture_run_skips_trivial_exchange(tmp_path: Path) -> None:
    """گفت‌وگوی بدون ابزار و تک‌دوره، حافظه را پر نمی‌کند."""
    memory = store(tmp_path)
    trivial = SimpleNamespace(ok=True, iterations=0, tool_names=[], text="hi", error="", duration_ms=12)
    assert memory.capture_run(trivial, prompt="hi") is None
    assert memory.all() == []


def test_capture_run_records_tools_and_outcome(tmp_path: Path) -> None:
    """خلاصه‌ی یک اجرای واقعی با ابزار ثبت می‌شود."""
    memory = store(tmp_path)
    result = SimpleNamespace(
        ok=True,
        iterations=2,
        tool_names=["terminal_run", "read_file"],
        text="done, three files changed",
        error="",
        duration_ms=4200,
    )
    record = memory.capture_run(result, prompt="tidy the repo", profile="developer")
    assert record is not None and record.kind == "decision"
    assert record.source == "agent:capture_run"
    assert "terminal_run" in record.content and "profile:developer" in record.tags
    assert "4.2s" in record.content


def test_capture_run_keeps_failures_as_notes(tmp_path: Path) -> None:
    """اجرای شکست‌خورده هم ثبت می‌شود (برای اینکه تکرار نشود)."""
    memory = store(tmp_path)
    result = SimpleNamespace(
        ok=False, iterations=1, tool_names=["terminal_run"], text="", error="model 429", duration_ms=900
    )
    record = memory.capture_run(result, prompt="run tests")
    assert record is not None and record.kind == "note"
    assert "error=model 429" in record.content


def test_plans_returns_open_plans_newest_first(tmp_path: Path) -> None:
    """plans فقط kind=plan را و از تازه به قدیم می‌دهد."""
    memory = store(tmp_path)
    memory.add("plan one", kind="plan")
    memory.add("a preference", kind="preference")
    time.sleep(0.01)
    memory.add("plan two", kind="plan")
    plans = memory.plans()
    assert [item.content for item in plans] == ["plan two", "plan one"]


# ---------------------------------------------------------------------------
# export/import و فایل‌های خراب
# ---------------------------------------------------------------------------
def test_export_import_roundtrip_dedupes(tmp_path: Path) -> None:
    """انتقال آگاهانه بین دو ماشین، بدون ایجاد رکورد تکراری."""
    source = store(tmp_path)
    source.add("shared preference", kind="preference", tags=["x"])
    target = AgentMemory(tmp_path / "other" / "memory.jsonl")
    path = tmp_path / "export.json"
    assert source.export_json(path) == 1
    assert target.import_json(path) == 1
    assert target.import_json(path) == 0
    assert [item.content for item in target.all()] == ["shared preference"]
    if not _is_windows():
        assert path.stat().st_mode & 0o777 == 0o600


def test_import_rejects_garbage_but_accepts_partial(tmp_path: Path) -> None:
    """فایل نامعتبر خطای خوانا می‌دهد؛ ورودی نیمه‌صحف فقط رد می‌شود."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    memory = store(tmp_path)
    with pytest.raises(ValueError, match="memory export"):
        memory.import_json(bad)
    assert memory.all() == []
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"records": ["nope", {"content": "real one"}]}), encoding="utf-8")
    assert memory.import_json(other) == 1


def test_corrupt_lines_are_skipped_on_load(tmp_path: Path) -> None:
    """خطوط نصفه‌نیمه (ریس) در فایل، بقیه را نمی‌شکنند."""
    path = tmp_path / "memory.jsonl"
    path.write_text('{"content": "kept", "kind": "note"}\n{oops\n\n', encoding="utf-8")
    memory = AgentMemory(path)
    assert [item.content for item in memory.all()] == ["kept"]


def test_disabled_store_does_nothing(tmp_path: Path) -> None:
    """store غیرفعال نه می‌خواند نه می‌نویسد."""
    memory = AgentMemory(tmp_path / "memory.jsonl", enabled=False)
    assert memory.add("should not be saved") is None
    assert not (tmp_path / "memory.jsonl").exists()
    assert memory.context_block() == ""
    assert "disabled" in memory.describe()


# ---------------------------------------------------------------------------
# اتصال به config
# ---------------------------------------------------------------------------
def test_for_config_uses_shared_cache(tmp_path: Path) -> None:
    """برای یک config یک نمونه‌ی مشترک ساخته می‌شود."""
    config = Config(openai_api_key="sk-test", memory_dir=str(tmp_path / "mem"), memory_enabled=True)
    first = AgentMemory.for_config(config)
    second = AgentMemory.for_config(config)
    assert first is not None and first is second
    assert memory_for_config(config) is first


def test_for_config_returns_none_when_disabled(tmp_path: Path) -> None:
    """حافظه خاموش یعنی ``None`` (ابزارها پیام روشن می‌دهند)."""
    config = Config(openai_api_key="sk-test", memory_dir=str(tmp_path / "mem"), memory_enabled=False)
    assert AgentMemory.for_config(config) is None
    assert AgentMemory.for_config(None) is None


def test_memory_path_prefers_configured_dir(tmp_path: Path) -> None:
    """``MEMORY_DIR`` می‌تواند خودش مسیر فایل باشد."""
    config = Config(openai_api_key="sk-test", memory_dir=str(tmp_path / "notes.jsonl"), memory_enabled=True)
    assert config.memory_path == tmp_path / "notes.jsonl"


def test_memory_stats_shape(tmp_path: Path) -> None:
    """stats کلیدهایی را که CLI/UI مصرف می‌کند داشته باشد."""
    stats = store(tmp_path).stats()
    for key in ("enabled", "path", "records", "pinned", "by_kind", "total_hits", "max_records"):
        assert key in stats


def _is_windows() -> bool:
    """روی ویندوز chmod معنا ندارد؛ بررسی مجوز فقط روی یونیکس سخت‌گیرانه است."""
    return os.name != "posix"

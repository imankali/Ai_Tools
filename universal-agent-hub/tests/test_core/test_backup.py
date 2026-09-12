"""تست مدیر بکاپ."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from src.core.backup import BackupManager


@pytest.fixture
def sources(tmp_path: Path) -> dict[str, Path]:
    data = tmp_path / "data"
    data.mkdir()
    memory = data / "memory.jsonl"
    memory.write_text('{"id":"m1","content":"prefers Persian"}\n', encoding="utf-8")
    routines = data / "routines.json"
    routines.write_text('{"routines":[]}', encoding="utf-8")
    return {"memory": memory, "routines": routines}


@pytest.fixture
def manager(tmp_path: Path, sources: dict[str, Path]) -> BackupManager:
    return BackupManager(tmp_path / "backups", sources)


def test_create_and_list(manager: BackupManager) -> None:
    info = manager.create(note="before upgrade")
    assert info.path.exists()
    assert info.note == "before upgrade"
    assert set(info.members) == {"memory", "routines"}
    assert len(manager.list()) == 1


def test_create_requires_existing_sources(tmp_path: Path) -> None:
    empty = BackupManager(tmp_path / "b", {"memory": tmp_path / "missing.jsonl"})
    with pytest.raises(ValueError, match="nothing to back up"):
        empty.create()


def test_create_rejects_bad_name(manager: BackupManager) -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        manager.create(name="../escape")


def test_create_rejects_duplicate(manager: BackupManager) -> None:
    manager.create(name="snap")
    with pytest.raises(ValueError, match="already exists"):
        manager.create(name="snap")


def test_keys_excluded_by_default(tmp_path: Path, sources: dict[str, Path]) -> None:
    keys = tmp_path / "keys.json"
    keys.write_text('{"k":"v"}', encoding="utf-8")
    sources["keys"] = keys
    manager = BackupManager(tmp_path / "b", sources)
    info = manager.create()
    assert "keys" not in info.members
    info2 = manager.create(name="withkeys", include_keys=True)
    assert "keys" in info2.members
    assert info2.include_keys is True


def test_test_verifies_checksums(manager: BackupManager) -> None:
    manager.create(name="good")
    verdict = manager.test("good")
    assert verdict["ok"] is True
    assert verdict["checked"] == 2


def test_test_detects_corruption(manager: BackupManager) -> None:
    manager.create(name="tampered")
    info = manager.find("tampered")
    assert info is not None
    with zipfile.ZipFile(info.path, "a") as archive:
        archive.writestr("memory/memory.jsonl", '{"id":"hacked"}')
    verdict = manager.test("tampered")
    assert verdict["ok"] is False
    assert "memory" in verdict["mismatch"]


def test_test_missing(manager: BackupManager) -> None:
    assert manager.test("nope")["ok"] is False


def test_inspect(manager: BackupManager) -> None:
    manager.create(name="i")
    info = manager.inspect("i")
    assert info["integrity"]["ok"] is True
    assert info["human_size"]


def test_inspect_missing(manager: BackupManager) -> None:
    with pytest.raises(KeyError):
        manager.inspect("nope")


def test_preview_is_dry_run(manager: BackupManager, sources: dict[str, Path]) -> None:
    manager.create(name="p")
    plan = manager.preview("p")
    assert plan.ok is True
    assert len(plan.would_overwrite) == 2
    # هیچ فایلی تغییر نکرده:
    assert sources["memory"].read_text(encoding="utf-8").startswith('{"id":"m1"')


def test_preview_missing(manager: BackupManager) -> None:
    assert manager.preview("nope").ok is False


def test_preview_only_subset(manager: BackupManager) -> None:
    manager.create(name="p")
    plan = manager.preview("p", only=["memory"])
    assert len(plan.would_write) + len(plan.would_overwrite) == 1


def test_restore_requires_confirm(manager: BackupManager, sources: dict[str, Path]) -> None:
    manager.create(name="r")
    sources["memory"].write_text("CORRUPTED", encoding="utf-8")
    out = manager.restore("r")
    assert out["dry_run"] is True
    assert sources["memory"].read_text(encoding="utf-8") == "CORRUPTED"


def test_restore_with_confirm(manager: BackupManager, sources: dict[str, Path]) -> None:
    manager.create(name="r")
    sources["memory"].write_text("CORRUPTED", encoding="utf-8")
    out = manager.restore("r", confirm=True)
    assert out["dry_run"] is False
    assert len(out["restored"]) == 2
    assert "prefers Persian" in sources["memory"].read_text(encoding="utf-8")
    assert out["safety_backup"] is not None  # از وضعیت خراب، بکاپ اضطراری گرفت


def test_restore_subset(manager: BackupManager, sources: dict[str, Path]) -> None:
    manager.create(name="r")
    sources["memory"].write_text("X", encoding="utf-8")
    before = sources["routines"].read_text(encoding="utf-8")
    manager.restore("r", confirm=True, only=["memory"])
    assert "prefers Persian" in sources["memory"].read_text(encoding="utf-8")
    assert sources["routines"].read_text(encoding="utf-8") == before


def test_restore_corrupt_raises(manager: BackupManager, sources: dict[str, Path]) -> None:
    info = manager.create(name="bad")
    with zipfile.ZipFile(info.path, "a") as archive:
        archive.writestr("memory/memory.jsonl", "tampered")
    with pytest.raises(ValueError, match="checksum"):
        manager.restore("bad", confirm=True)


def test_explicit_source_survives_refresh(tmp_path: Path) -> None:
    """رگرسیون: ``set_source`` باید بر provider اولویت داشته باشد.

    وقتی ``ServiceHub`` یک ``source_provider`` می‌دهد، ``create()`` منابع را
    بازخوانی می‌کند (تا فایل‌های دیرهنگام جا نیفتند). اگر این بازخوانی منبعی
    را که فراخوان دستی وصل کرده بود پاک کند، بکاپ بی‌صدا ناقص می‌شود.
    """
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"id":"explicit"}', encoding="utf-8")

    manager = BackupManager(
        tmp_path / "b",
        sources={"provided": tmp_path / "nope.jsonl"},  # موجود نیست
        source_provider=lambda: {"provided": tmp_path / "nope.jsonl"},
    )
    manager.set_source("extra", outside)

    info = manager.create()
    assert set(info.members) == {"extra"}  # منبع صریح باقی می‌ماند
    # members نگاشت «نام منطقی → sha256» است؛ پس محتوای واقعی هم سنجیده می‌شود
    assert info.members["extra"] == hashlib.sha256(outside.read_bytes()).hexdigest()


def test_prune_keeps_limit(tmp_path: Path, sources: dict[str, Path]) -> None:
    manager = BackupManager(tmp_path / "b", sources, keep=2)
    for index in range(4):
        manager.create(name=f"s{index}")
    assert len(manager.list()) == 2


def test_remove(manager: BackupManager) -> None:
    manager.create(name="gone")
    assert manager.remove("gone") is True
    assert manager.remove("gone") is False


def test_manifest_missing_is_skipped(tmp_path: Path, sources: dict[str, Path]) -> None:
    manager = BackupManager(tmp_path / "b", sources)
    manager.create(name="ok")
    with zipfile.ZipFile(tmp_path / "b" / "broken.zip", "w") as archive:
        archive.writestr("junk.txt", "x")
    assert len(manager.list()) == 1


def test_corrupt_zip_is_skipped(tmp_path: Path, sources: dict[str, Path]) -> None:
    manager = BackupManager(tmp_path / "b", sources)
    manager.create(name="ok")
    (tmp_path / "b" / "corrupt.zip").write_bytes(b"not a zip at all")
    assert len(manager.list()) == 1


def test_export_json(manager: BackupManager, tmp_path: Path) -> None:
    manager.create(name="e")
    assert manager.export_json(tmp_path / "out.json") == 1
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["backups"][0]["name"] == "e"


def test_describe(manager: BackupManager) -> None:
    manager.create(name="d")
    info = manager.describe()
    assert info["count"] == 1
    assert info["keep"] == 20
    assert "memory" in info["sources"]


def test_set_source(tmp_path: Path) -> None:
    manager = BackupManager(tmp_path / "b", {})
    target = tmp_path / "x.jsonl"
    target.write_text("data", encoding="utf-8")
    manager.set_source("extra", target)
    info = manager.create(name="s")
    assert list(info.members) == ["extra"]
    assert info.sizes["extra"] == len("data")


def test_human_size_is_a_property(manager: BackupManager) -> None:
    """رگرسیون: CLI به ``info.human_size`` تکیه می‌کند، نه فقط ``as_dict()``."""
    from src.utils.helpers import format_size

    info = manager.create(name="hs")
    assert info.human_size == format_size(info.bytes)
    assert info.as_dict()["human_size"] == info.human_size

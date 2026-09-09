"""آزمون‌های استاتیک UI — بدون سرور و بدون شبکه.

چرا؟ چون پوشه‌ی ``web`` یک PWA است که مرورگر آن را «همان‌طور که هست» بار می‌کند:
هر ارجاع شکسته (آیکونِ نبود در manifest، کلید i18nِ تعریف‌نشده، ``$("#id")`` که
در HTML وجود ندارد) فقط روی گوشی کاربر به‌صورت ۴۰ یا متن خامِ ``tab_reports``
ظاهر می‌شود. این آزمون‌ها همان طبقه باگ را در CI می‌گیرند.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src" / "server" / "web"


@pytest.fixture(scope="module")
def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dictionary(app: str) -> dict[str, dict[str, str]]:
    """``{en: {...}, fa: {...}}`` — فقط کلیدهای سطح‌یک، همان‌طور که در فایل نوشته شده."""

    def parse(lang: str) -> dict[str, str]:
        match = re.search(rf"^  {lang}: \{{\n(.*?)\n  \}},?$", app, re.S | re.M)
        assert match, f"i18n block for {lang!r} not found"
        out: dict[str, str] = {}
        for line in match.group(1).splitlines():
            item = re.match(r"^    ([a-z0-9_]+): (\"(?:[^\"\\]|\\.)*\"),?$", line)
            if item:
                out[item.group(1)] = item.group(2)
        assert len(out) > 40, "i18n block looks truncated"
        return out

    return {"en": parse("en"), "fa": parse("fa")}


# --------------------------------------------------------------------- فایل‌ها
def test_all_web_files_present() -> None:
    for name in ("index.html", "styles.css", "app.js", "manifest.webmanifest", "sw.js", "offline.html", "icon.svg"):
        assert (WEB / name).is_file(), f"missing UI asset {name}"


def test_manifest_icons_exist_and_are_pngs() -> None:
    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["icons"], "manifest must declare at least one icon"
    assert manifest["display"] == "standalone"
    for entry in manifest["icons"]:
        source = entry["src"]
        path = WEB / source
        if source.endswith(".png"):
            assert path.is_file(), f"manifest references missing icon {source}"
            blob = path.read_bytes()
            assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{source} is not a PNG"
            width, height = struct.unpack(">II", blob[16:24])
            assert width == height > 0
            for declared in re.findall(r"(\d+)", source):
                assert width == int(declared), f"{source}: declared {declared}x{declared}, drawn {width}px"
    assert (WEB / "icon.svg").read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_generated_icons_match_generator() -> None:
    """``scripts/make_icons.py`` باید همان فایل‌های موجود را دوباره بسازد (تولید قطعی است)."""
    import sys

    sys.path.insert(0, str(WEB.parents[2] / "scripts"))
    try:
        make_icons = __import__("make_icons")
    except Exception as exc:  # pragma: no cover - فقط اگر اسکریپت جابه‌جا شده باشد
        pytest.skip(f"icon generator unavailable: {exc}")
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512)):
        fresh = make_icons.draw(size)
        on_disk = (WEB / "icons" / name).read_bytes()
        assert len(fresh) > 1000
        assert struct.unpack(">II", fresh[16:24]) == (size, size)
        assert fresh == on_disk, f"{name} is stale — run scripts/make_icons.py"


def test_service_worker_shell_files_exist() -> None:
    sw = (WEB / "sw.js").read_text(encoding="utf-8")
    block = re.search(r"const SHELL = \[(.*?)\];", sw, re.S)
    assert block, "sw.js must precache a SHELL list"
    files = [item.strip().strip('"').lstrip("./") for item in block.group(1).split(",")]
    assert files, "SHELL must not be empty"
    for name in files:
        if name:
            assert (WEB / name).is_file(), f"sw.js precaches missing file {name}"
    version = re.search(r'const VERSION = "([^"]+)"', sw)
    assert version and version.group(1), "sw.js needs a cache VERSION so updates roll out"
    assert "API/" in sw or "/api/" in sw, "sw.js must never cache /api/"


# ----------------------------------------------------------------------- i18n
def test_every_markup_key_is_translated(html: str, dictionary: dict[str, dict[str, str]]) -> None:
    used = set(re.findall(r'data-i18n(?:-[a-z]+)?="([a-z0-9_]+)"', html))
    assert len(used) > 20
    missing = sorted(key for key in used if key not in dictionary["en"])
    assert not missing, f"data-i18n keys without an English entry: {missing}"


def test_every_js_key_is_translated(app: str, dictionary: dict[str, dict[str, str]]) -> None:
    used = set(re.findall(r'\bt\("([a-z0-9_]+)"\)', app))
    assert used, "app.js should translate its runtime strings"
    missing = sorted(key for key in used if key not in dictionary["en"])
    assert not missing, f"t(...) keys without an English entry: {missing}"


def test_languages_have_the_same_keys(dictionary: dict[str, dict[str, str]]) -> None:
    en, fa = dictionary["en"], dictionary["fa"]
    assert set(en) == set(
        fa
    ), f"en/fa mismatch: en-only={sorted(set(en) - set(fa))} fa-only={sorted(set(fa) - set(en))}"


def test_persian_strings_are_not_cjk_contaminated(dictionary: dict[str, dict[str, str]]) -> None:
    """مقادیر فارسی نباید نویسه‌ی چینی/ژاپنی/کره‌ای داشته باشند (اشتباه رایج اسکریپت‌های متنی)."""
    cjk = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
    offenders = {key: value for key, value in dictionary["fa"].items() if cjk.search(json.loads(value))}
    assert not offenders, f"CJK characters in Persian strings: {offenders}"


# ------------------------------------------------------------------ ساختار DOM
def test_every_selector_resolves(html: str, app: str) -> None:
    """هر ``$("#id")`` باید به عنصری در HTML (یا یک id داینامیک در خود app.js) برسد."""
    referenced = set(re.findall(r'\$\("#([a-z0-9_-]+)"\)', app))
    assert referenced, "app.js should wire the UI through $('#id')"
    unresolved = sorted(
        node_id for node_id in referenced if f'id="{node_id}"' not in html and f'id="{node_id}"' not in app
    )
    assert not unresolved, f"selectors without a matching element: {unresolved}"


def test_tabs_match_views(html: str) -> None:
    tabs = re.findall(r'<button class="tab[^"]*" data-view="([a-z-]+)"', html)
    # view-chat با کلاس اضافه (is-active) شروع می‌شود، پس الگو باید منعطف باشد
    views = re.findall(r'<section id="view-([a-z-]+)" class="view[^"]*"', html)
    assert tabs and sorted(tabs) == sorted(views), f"tabs={tabs} views={views}"
    for tab in tabs:
        assert f"tab_{tab}" in html, f"tab {tab} has no data-i18n label"

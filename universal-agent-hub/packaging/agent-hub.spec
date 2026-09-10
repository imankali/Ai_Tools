# -*- mode: python ; coding: utf-8 -*-
"""Spec پایمر (PyInstaller) برای ساخت باینلی تک‌فایلی Universal Agent Hub.

اجرا از ریشه‌ی پروژه (بعد از `pip install pyinstaller`):

    python -m PyInstaller packaging/agent-hub.spec --noconfirm

خروجی:

    dist/agent-hub            (لینوکس/macOS)
    dist/agent-hub.exe        (ویندوز)

هر سه رفتار یکسان است: همان CLI، همان `--serve`، همان UI وب. UI به‌عنوان داده
داخل بسته می‌آید (`src/server/web`) و ابزارها هم در حالت فریزشده کشف می‌شوند
(`BUILTIN_TOOL_MODULES` در `src/core/tool_registry.py`).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH توسط PyInstaller تزریق می‌شود
WEB_ROOT = PROJECT_ROOT / "src" / "server" / "web"

# بسته‌ی خودمان: همه‌ی زیرماژول‌ها (ابزارها، سرور، هسته) باید داخل archive باشند
hiddenimports = collect_submodules("src")
# وابستگی‌های با import داینامیک
hiddenimports += ["aiohttp", "multidict", "yarl", "psutil", "ddgs", "dotenv"]
hiddenimports += [name for name in collect_submodules("openai") if name.endswith("._client")]

# فایل‌های داده: UI + فایل‌های بسته‌ی pydantic/aiohttp که با import کشف نمی‌شوند
datas = [(str(WEB_ROOT), "src/server/web")] if WEB_ROOT.is_dir() else []
datas += collect_data_files("pydantic")
datas += collect_data_files("aiohttp")

# اختیاری‌های سنگین که در باینلی لازم نیستند
excludes = ["tkinter", "pytest", "IPython", "PIL", "matplotlib", "playwright", "pyarrow"]

a = Analysis(
    [str(PROJECT_ROOT / "src" / "cli.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[str(Path(SPECPATH) / "hooks")],  # noqa: F821 - SPECPATH پوشه‌ی خودِ spec است
    runtime_hooks=[str(Path(SPECPATH) / "hooks" / "rthook_agent_hub.py")],  # noqa: F821
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agent-hub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # CLI است؛ برای پنجره‌ی گرافیکی بدون ترمینال False کنید
    # icon="packaging/agent-hub.ico",  # اگر خواستید، یک .ico 256×256 کنار همین فایل بگذارید
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="agent-hub",
)

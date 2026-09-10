# بسته‌بندی دسکتاپ (ویندوز / macOS / لینوکس)

سه راه دارید؛ هر سه همان UI را می‌دهند.

## ۱. `pip install` (توصیه‌شده)

```bash
python -m venv .venv && . .venv/bin/activate      # ویندوز: .venv\Scripts\activate
pip install universal-agent-hub                   # یا: pip install ./universal-agent-hub[all]
agent-hub --serve --desktop                       # پنجره‌ی بومی یا مرورگر پیش‌فرض
```

برای پنجره‌ی واقعی (به‌جای تب مرورگر):

```bash
pip install pywebview     # ویندوز: WebView2 · macOS: WKWebView · لینوکس: WebKitGTK
```

## ۲. باینلی تک‌پوشه‌ای (بدون نیاز به پایتون کاربر)

```bash
pip install pyinstaller
python -m PyInstaller packaging/agent-hub.spec --noconfirm
# dist/agent-hub/agent-hub[.exe]
```

CI در تگ `v*.*.*` همین کار را روی هر سه سیستم‌عامل انجام می‌دهد و خروجی را به
GitHub Release می‌چسباند (بخش Downloads):

    https://github.com/imankali/Ai_Tools/releases/latest

نام فایل‌ها:

| فایل | سیستم‌عامل |
|---|---|
| `agent-hub-<version>-windows-x64.zip` | ویندوز ۱۰/۱۱ ۶۴بیت |
| `agent-hub-<version>-macos-arm64.zip` | Apple silicon |
| `agent-hub-<version>-macos-intel.zip` | اینتل |
| `agent-hub-<version>-linux-x64.tar.gz` | توزیع‌های ۶۴بیت |
| `agent-hub-android-debug.apk` | اندروید ۷ به بالا |
| `universal_agent_hub-<version>-py3-none-any.whl` | هر جا پایتون ۳.۱۰+ هست |

باینلی امضا/notarize نشده است؛ macOS ممکن است بگوید «unidentified developer» —
با `right-click → Open` یا `xattr -d com.apple.quarantine agent-hub` باز کنید.

## ۳. داکر

```bash
docker compose up -d hub-server      # API + UI روی http://localhost:8765
```

## افزودن ابزار به باینلی

کنار فایل اجرایی پوشه‌ی `tools/` بسازید و فایل `myscript.py` را داخلش بگذارید؛
`packaging/hooks/rthook_agent_hub.py` همان مسیر را به `sys.path` اضافه می‌کند، پس
نیازی به بازسازی باینلی نیست (همان قرارداد `src/tools/template.py`).

## نکته درباره‌ی `.env`

باینلی اول `.env` موجود در پوشه‌ی جاری را می‌خواند؛ اگر برنامه را با double-click
اجرا می‌کنید، یک `.env` کنار فایل اجرایی بگذارید (کار می‌کند، چون `PROJECT_ROOT`
روی پوشه‌ی اجرا قفل می‌شود). کلید API در همین فایل می‌ماند و هرگز به گوشی یا
مرورگر فرستاده نمی‌شود.

## آیکون‌ها

آیکون‌های PNG که PWA و باندل‌ها استفاده می‌کنند در `src/server/web/icons/` هستند و
با اسکریپت استاندارد-کتابخانه‌ای ساخته می‌شوند (بدون PIL/ImageMagick):

```bash
make icons            # یا: python scripts/make_icons.py
python scripts/make_icons.py --check
```

خروجی قطعی (deterministic) است؛ یک آزمون در `tests/test_server/test_web_assets.py`
بررسی می‌کند فایل‌های موجود با بازتولید اسکریپت مو به مو یکی‌اند، پس «آیکون قدیمی»
در repo باقی نمی‌ماند. برای آیکون APK/IPA همین فایل‌ها کپی می‌شوند
(`apps/android/README.md` و `apps/ios/README.md`).

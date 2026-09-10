# اپ‌ها — گوشی و دسکتاپ

همه‌ی اپ‌ها کلاینتِ **همان سرور** هستند (`src/server/`): هسته‌ی ایجنت روی کامپیوتر
شما اجرا می‌شود و اپ فقط رابط است. یعنی:

* کلید API مدل هرگز به گوشی/مرورگر فرستاده نمی‌شود — فقط فرم ماسک‌شده نمایش داده می‌شود؛
* ابزارها با همان مجوزهای واقعی شما روی همان ماشین اجرا می‌شوند و هر کار خطرناک
  پیش از اجرا از شما تأیید می‌خواهد؛
* یک بار سرور را بالا می‌آورید و همه‌جا (اندروید، iOS، ویندوز، macOS، لینوکس،
  Termux، داکر) همان UI را می‌بینید.

## شروع سریع (۶۰ ثانیه)

```bash
cd universal-agent-hub
make install                                        # یا: pip install -e ".[all]"
.venv/bin/python -m src.server --lan --print-token  # یا: make serve-lan
```

خروجی:

```
Universal Agent Hub 1.0.0 → http://192.168.1.42:8765
  token: 7f3c9a…c91a
```

آن آدرس را در اپ باز کنید و توکن را وارد کنید. تمام.

| پلتفرم | راه | کجا | راهنما |
|---|---|---|---|
| اندروید | APK (پوسته‌ی WebView) | `apps/android/` | [`apps/android/README.md`](android/README.md) |
| اندروید، سرور روی گوشی | Termux | `scripts/install-termux.sh` | [`docs/apps.md`](../docs/apps.md) |
| iOS | اپ SwiftUI (Xcode) | `apps/ios/` | [`apps/ios/README.md`](ios/README.md) |
| iOS و هر مرورگر دیگری | PWA «Add to Home Screen» | — | [`docs/apps.md`](../docs/apps.md) |
| ویندوز | `install.ps1` یا باینلی `.zip` | `scripts/`, `packaging/` | [`packaging/README.md`](../packaging/README.md) |
| macOS | `install.sh` یا باینلی `arm64/intel` | همان‌ها | همان |
| لینوکس | `install.sh` یا باینلی `.tar.gz` | همان‌ها | همان |
| همه | Docker | `Dockerfile`, `docker-compose.yml` | `docker compose up hub-server` |

## دانلود آماده

CI در هر تگ `v*.*.*` این فایل‌ها را می‌سازد و به Release می‌چسباند:

    https://github.com/imankali/Ai_Tools/releases/latest

* `agent-hub-1.0.0-windows-x64.zip`
* `agent-hub-1.0.0-macos-arm64.zip` · `agent-hub-1.0.0-macos-intel.zip`
* `agent-hub-1.0.0-linux-x64.tar.gz`
* `agent-hub-android-debug.apk`
* `universal_agent_hub-1.0.0-py3-none-any.whl` (پایتون ۳.۱۰+)
* `universal_agent_hub-1.0.0.tar.gz` (sdist)

iOS باینلی آماده ندارد؛ ساختنش Xcode و یک Apple ID می‌خواهد. راه بدون‌استور روی
آیفون همان PWA است.

## بخش مدیریتی (کلیدها، session ها، تأییدها)

* تب **Keys** → مدیریت پروفایل‌های کلید API (`/api/keys`): افزودن، فعال‌سازی،
  آدرس base متفاوت برای هر مدل، و «Import from .env».
* تب **Settings** → وضعیت سرور، محدودساز نرخ، پروفایل‌های ایجنت
  (`read_only` … `full_access`) و فهرست session های فعال (`/api/sessions`).
* کارت‌های **تأیید** → هر ابزار خطرناک قبل از اجرا اینجا می‌ایستد
  (`/api/sessions/{id}/approvals`).

همه‌ی نوشتن‌ها فقط در دستگاهی که سرور روی آن اجراست ذخیره می‌شوند. برای
چندنفره‌کردن (چند کاربر با توکن‌های جدا) TLS + reverse proxy لازم است:
[`docs/apps.md`](../docs/apps.md#چندنفره-کردن-hub).

## نقشه‌ی سرور

```
src/server/
  app.py          مسیرهای HTTP + WebSocket + سرو UI
  sessions.py     session های ایجنت، صف اجرا، پل تأیید
  keystore.py     پروفایل‌های کلید API (ماسک‌شده در همه‌ی پاسخ‌ها)
  auth.py         بررسی توکن، محدودساز نرخ، محافظت Host header
  protocol.py     مدل‌های ورودی/خروجی (قرارداد API)
  web/            PWA (HTML/CSS/JS بدون build) — آفلاین، RTL، تیره/روشن
```

قرارداد کامل API: [`docs/server_api.md`](../docs/server_api.md).

# اپ اندروید — Universal Agent Hub (WebView shell)

پوسته‌ی بومی اندروید که UI وبِ سرویس‌شده از کامپیوتر را داخل `WebView` نشان می‌دهد.
گوشی «کنترل از راه دور» است: ایجنت و ابزارها روی کامپیوتر اجرا می‌شوند و **کلید API
هرگز روی گوشی نمی‌نشیند**. روی گوشی فقط دو چیز ذخیره می‌شود — آدرس سرور و توکن
دسترسی — و آن‌ها هم با کلید Android Keystore رمزنگاری می‌شوند (`Prefs.kt`).

## ۱. اول سرور را روی کامپیوتر بالا بیاورید

```bash
cd universal-agent-hub
.venv/bin/python -m src.server --lan --print-token      # یا: make serve-lan
```

خروجی چیزی مثل این است:

```
  Local:   http://192.168.1.42:8765
  Token:   7f3c…c91a          # ← همین را در اپ وارد کنید
```

`--lan` یعنی `SERVER_HOST=0.0.0.0` + تولید خودکار توکن. بدون توکن، سرور روی
آدرس غیر لوپ‌بکک بالا **نمی‌آید** (عمدی است).

## ۲. نصب از روی APK (سریع‌ترین راه)

APK امضاشده‌ی debug در GitHub Release می‌آید:

    https://github.com/imankali/Ai_Tools/releases/latest   →  agent-hub-android-debug.apk

روی گوشی: تنظیمات → امنیت → «نصب از منابع ناشناخته» → بازکردن فایل APK.

## ۳. ساخت از سورس

پیش‌نیاز: Android SDK 35 + JDK 17 (Android Studio کافی است).

```bash
cd apps/android
gradle :app:assembleDebug          # یا make apk از ریشه‌ی پروژه
# خروجی: app/build/outputs/apk/debug/app-debug.apk
```

اگر `gradle` را نصب ندارید، همین پوشه را در Android Studio باز کنید (File → Open)؛
خودش wrapper را می‌سازد. فایل `local.properties` لازم نیست؛ `ANDROID_HOME` کافی است.

امضای نسخه‌ی release (اختیاری، برای انتشار عمومی):

```bash
keytool -genkeypair -keystore hub.keystore -alias agenthub -keyalg RSA -keysize 2048 -validity 10000
export HUB_KEYSTORE_PATH=$PWD/hub.keystore HUB_KEYSTORE_PASSWORD=… HUB_KEY_ALIAS=agenthub HUB_KEY_PASSWORD=…
gradle :app:assembleRelease
```

## ۴. اتصال

1. اپ را باز کنید → همان شبکه‌ی Wi‑Fi کامپیوتر (یا همین IP در ترمینال).
2. «آزمودن اتصال» → پیام سبز: `Connected · 19 tool(s) · gpt-6-astra`.
3. «اتصال» → UI کامل: چت، ابزارها، کلیدها، تنظیمات، صف تأیید.

اگر خطا داد:
* `Cannot reach the server` → فایروال/VPN، یا سرور با `--lan` اجرا نشده؛
* `Wrong token (401)` → توکن را دوباره از ترمینال کپی کنید؛
* می‌خواهید فقط روی خود گوشی اجرا شود (Termux) → آدرس `http://127.0.0.1:8765`.

### لینک عمیق (QR)

```
agenthub://connect?url=http://192.168.1.42:8765&token=7f3c…c91a&session=abc123
```

با «Paste» در صفحه‌ی اتصال هم همان اثر را دارد. QR را خودتان بسازید:
`python -c "import qrcode; qrcode.make('agenthub://connect?url=…').save('hub.png')"`

## ۵. PWA بدون ساخت اپ

اگر نمی‌خواهید APK بسازید: در Chrome آدرس سرور را باز کنید → منو →
*Add to Home screen*. آیکون، splash، حالت آفلاین و اعلان‌ها از قبل در
`src/server/web/manifest.webmanifest` تعریف شده‌اند.

## ساختار فایل‌ها

| فایل | نقش |
|---|---|
| `MainActivity.kt` | WebView، منو، مدیریت آفلاین/SSL، back |
| `PairActivity.kt` | فرم اتصال، QR/link عمیق، تست `/api/status` |
| `HubBridge.kt` | `window.HubNative`: اعلان، لرزش، کپی |
| `Prefs.kt` | ذخیره‌ی رمزنگاری‌شده‌ی آدرس + توکن (Android Keystore) |
| `ServerApi.kt` | REST سبک بدون کتابخانه‌ی خارجی |
| `res/values-fa/` | رشته‌های فارسی + RTL خودکار |
| `res/xml/network_security_config.xml` | مجوز http روی شبکه‌ی محلی |

## حریم خصوصی و امنیت

* فقط `INTERNET`، `POST_NOTIFICATIONS`، `VIBRATE`، `WAKE_LOCK` — بدون دسترسی به
  فایل، مخاطب، مکان یا دوربین.
* بکاپ ابری و انتقال به دستگاه جدید برای تنظیمات بسته است (`xml/backup_rules.xml`).
* توکن با `?token=` به صفحه داده می‌شود و UI آن را با `history.replaceState` از
  تاریخچه بیرون می‌کند (`hydrateFromLocation()` در `app.js`).
* `cleartextTrafficPermitted=true` فقط برای شبکه‌ی محلی است؛ برای استفاده‌ی عمومی،
  TLS را جلوی سرور بگذارید (مثلاً Caddy: `reverse_proxy 127.0.0.1:8765`).

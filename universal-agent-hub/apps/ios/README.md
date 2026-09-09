# اپ iOS — Universal Agent Hub (SwiftUI + WKWebView)

پوسته‌ی SwiftUI که همان UI وبِ سرویس‌شده از کامپیوتر را داخل `WKWebView` نشان می‌دهد
و برای «اتصال» از Keychain استفاده می‌کند. کلید API مدل روی کامپیوتر می‌ماند.

## ۰. اول سرور

```bash
cd universal-agent-hub
.venv/bin/python -m src.server --lan --print-token      # یا: make serve-lan
```

آدرس `http://192.168.1.42:8765` و توکن چاپ‌شده را نگه دارید.

> iOS برای بازکردن http روی LAN به `NSAllowsLocalNetworking` نیاز دارد؛ همین
> گزینه در `UniversalAgentHub/Info.plist` و `project.yml` تنظیم شده است. روی
> شبکه‌ی عمومی، TLS بگذارید (پایین‌تر).

## ۱. ساخت پروژه با XcodeGen

```bash
brew install xcodegen
cd apps/ios
xcodegen generate
open UniversalAgentHub.xcodeproj
```

در Xcode:

1. Target **UniversalAgentHub** → *Signing & Capabilities* → Team خودتان
   (Bundle ID: `com.agenthub.ios`).
2. دستگاه را انتخاب کنید → **Run** (⌘R).

حداقل: iOS ۱۶، Xcode ۱۵+.

## ۲. انتشار بدون App Store (TestFlight)

```bash
xcodebuild -project UniversalAgentHub.xcodeproj -scheme UniversalAgentHub \
  -configuration Release -archivePath build/Hub.xcarchive archive

xcodebuild -exportArchive -archivePath build/Hub.xcarchive \
  -exportPath build/ipa -exportOptionsPlist ExportOptions.plist

xcrun notarytool submit build/ipa/UniversalAgentHub.ipa \
  --keychain-profile AppStoreConnect --wait
```

`ExportOptions.plist` (کلیدهای لازم: `method = app-store-connect`,
`signingStyle = automatic` را کنار همین فایل بگذارید). سپس از App Store Connect
نسخه‌ی TestFlight را منتشر کنید.

## ۳. راه بدون ساخت اپ (PWA)

در **Safari** آدرس سرور را باز کنید → Share → **Add to Home Screen**. آیکون،
splash، RTL و حالت آفلاین از قبل در `src/server/web/manifest.webmanifest` و
`sw.js` تعریف شده‌اند. محدودیت iOS: اعلان بومی از PWA عملاً در دسترس نیست — اگر
اعلانِ «تأیید لازم است» را می‌خواهید، اپ را بسازید.

## ۴. اتصال در اپ

1. صفحه‌ی «Connect to your computer» → آدرس + توکن (یا Paste از کلیپ‌بورد).
2. **Test connection** → `Connected · 19 tool(s) · gpt-6-astra`.
3. **Connect** → چت، ابزارها، Keys، Settings، صف تأیید.

لینک عمیق (مثلاً از QR) هم کار می‌کند:

```
agenthub://connect?url=http://192.168.1.42:8765&token=7f3c9a…c91a
```

## ساختار

| فایل | نقش |
|---|---|
| `UniversalAgentHub/App/UniversalAgentHubApp.swift` | ورودی، `HubModel`، لینک عمیق |
| `UniversalAgentHub/App/RootView.swift` | جابه‌جایی «اتصال ↔ هاب» |
| `UniversalAgentHub/Views/PairingView.swift` | فرم آدرس/توکن + سنجش زنده |
| `UniversalAgentHub/Views/HubContainerView.swift` | نوار ابزار، پوشش آفلاین، About |
| `UniversalAgentHub/Views/HubWebView.swift` | `WKWebView` + پل `HubNative` (اعلان/کپی/لرزش) |
| `UniversalAgentHub/Services/Credentials.swift` | توکن در Keychain (نه UserDefaults) |
| `UniversalAgentHub/Services/ServerAPI.swift` | `GET /api/status` + تحمل گواهی self-signed محلی |
| `UniversalAgentHub/Models/HubStatus.swift` | قرارداد پاسخ سرور |
| `UniversalAgentHub/Resources/{en,fa}.lproj/Localizable.strings` | انگلیسی/فارسی (RTL خودکار) |
| `project.yml` | تعریف پروژه برای XcodeGen |

پل `HubNative` همان قراردادی است که نسخه‌ی اندروید با `JavascriptInterface` می‌دهد،
پس `app.js` برای هر دو یک کد دارد: `notify(payload)`، `copy(text)`، `vibrate()`.

## خطاها و راه‌حل‌ها

* **Cannot reach the server** → گوشی و کامپیوتر روی یک شبکه؛ روی macOS به
  `python` اجازه‌ی inbound بدهید؛ سرور با `--lan` اجرا شده باشد.
* **Wrong token (401)** → توکن را دوباره کپی کنید (کاراکترهای آخر نیفتند).
* **Agent offline** → پوشش آفلاین دکمه‌ی Try again دارد؛ یا ⋮ → Reload.
* **پنجره‌ی «Local Network» نیامد** → Settings → Privacy & Security → Local
  Network → Agent Hub را روشن کنید (iOS این اجازه را یک‌بار می‌پرسد).
* **صفحه سفید/خطای TLS** → اگر reverse proxy با گواهی self-signed دارید،
  گواهی را در Keychain گوشی trust کنید یا موقتاً `SERVER_HOST=127.0.0.1` را با
  تانل (`ssh -L 8765:127.0.0.1:8765 host`) استفاده کنید.

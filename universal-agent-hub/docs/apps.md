# راهنمای اپ‌ها — گوشی، دسکتاپ، شبکه

معماری ساده است: **ایجنت روی کامپیوتر اجرا می‌شود؛ گوشی/مرورگر فقط کنترل از راه
دورند.** هر اپلیکیشنی که می‌سازید یا نمی‌سازید، همان UI وب سرویس‌شده از
`src/server/` را می‌بینید.

```
┌──────────────── phone / tablet / laptop ─────────────────┐
│  Android APK · iOS app · PWA · browser · Tauri window     │
│      chat · tools · approvals · keys · settings           │
└───────────────▲───────────────────────────┬───────────────┘
                │ REST + WebSocket (:8765)  │  فقط توکن، هرگز کلید API
┌───────────────┴───────────────────────────▼───────────────┐
│  your computer — src/server (aiohttp)                     │
│   auth (token) · rate limit · sessions · approval broker   │
│   AgentCore → tools (terminal, files, browser, search…)   │
│   KeyStore (~/.universal-agent-hub/keys.json)              │
└───────────────────────────────────────────────────────────┘
```

## ۱. سرور را بالا بیاورید

| هدف | دستور |
|---|---|
| فقط همین دستگاه | `agent-hub --serve` (یا `make serve`) |
| اتصال گوشی از same Wi‑Fi | `agent-hub --serve --lan --print-token` |
| شبکه‌ی کاملاً مطمئن، بدون توکن | `agent-hub --serve --host 0.0.0.0 --insecure` |
| پنجره‌ی دسکتاپ | `agent-hub --serve --desktop` |
| بدون UI (فقط API) | `agent-hub --serve --no-ui` |

منطق توکن (عمدی و امن‌پیشانه):

* `127.0.0.1` / `localhost` → توکن لازم نیست (همین دستگاه امن است).
* هر آدرس دیگر (`0.0.0.0`, `192.168.x.x`) → **بدون توکن سرور بالا نمی‌آید**؛
  `--lan` خودش یک توکن تصادفی می‌سازد و چاپ می‌کند.
* `python -m src.server` روی bind باز بدون توکن، `exit 2` می‌دهد و توضیح می‌دهد.

پورت پیش‌فرض `8765` است (`SERVER_PORT`).

## ۲. اندروید

دو راه دارید:

1. **APK** — [`apps/android/README.md`](../apps/android/README.md) (پوسته‌ی
   WebView، اعلان بومی، QR/لینک عمیق، توکن در Keystore).
2. **PWA بدون نصب چیزی** — Chrome → آدرس سرور → منو → *Add to Home screen*.

اعلان‌ها: اندروید ۱۳+ اجازه‌ی `POST_NOTIFICATIONS` را می‌پرسد. وقتی صفحه در
پس‌زمینه باشد و ایجنت تأیید بخواهد، UI به `window.HubNative.notify()` دست می‌زند
(`web/app.js` → `nativeEvent`).

### Termux: ایجنت روی خود گوشی

```bash
pkg install curl -y
curl -fsSL https://raw.githubusercontent.com/imankali/Ai_Tools/main/universal-agent-hub/scripts/install-termux.sh | bash
cd ~ && agent-hub --serve
```

در Chrome گوشی: `http://127.0.0.1:8765`. ابزار `terminal_run` و `filesystem_*`
داخل sandbox ترموکس اجرا می‌شوند (`/data/data/com.termux/files/home`); دسترسی به
`/sdcard` با `termux-setup-storage`. اجرای ریشه‌ای (root) لازم نیست و اگر گوشی
root باشد هم خودکار استفاده نمی‌شود.

## ۳. iOS

* اپ SwiftUI: [`apps/ios/README.md`](../apps/ios/README.md) (Xcode + XcodeGen،
  TestFlight برای استفاده‌ی روزانه بدون استور).
* PWA: Safari → Share → **Add to Home Screen** — آیکون/اسپلش/آفلاین از قبل آماده
  است؛ اعلان بومی روی PWA عملاً ممکن نیست، پس برای «تأیید از راه دور» اپ را بسازید.

## ۴. ویندوز / macOS / لینوکس

```bash
# ویندوز (PowerShell)
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

# macOS / لینوکس
curl -fsSL https://raw.githubusercontent.com/imankali/Ai_Tools/main/universal-agent-hub/scripts/install.sh | bash
```

یا باینلی آماده از Releases (نیازمند نصب پایتون نیست):

```
https://github.com/imankali/Ai_Tools/releases/latest
  agent-hub-1.0.0-windows-x64.zip      ←  agent-hub\\agent-hub.exe --serve --desktop
  agent-hub-1.0.0-macos-arm64.zip      ←  xattr -d com.apple.quarantine agent-hub  (یک‌بار)
  agent-hub-1.0.0-linux-x64.tar.gz
```

جزئیات build: [`packaging/README.md`](../packaging/README.md).

`--desktop` اگر `pip install pywebview` نصب باشد یک پنجره‌ی بومی باز می‌کند
(WebView2 / WKWebView / WebKitGTK)؛ اگر نباشد مرورگر پیش‌فرض باز می‌شود. توکن
را همان لحظه داخل URL می‌گذارد و UI پس از خواندن، آن را از تاریخچه پاک می‌کند.

### به‌عنوان سرویس همیشه‌روشن

Linux (systemd, کاربر معمولی):

```ini
# ~/.config/systemd/user/agent-hub.service
[Unit]
Description=Universal Agent Hub
After=network-online.target

[Service]
WorkingDirectory=%h
ExecStart=%h/.agent-hub/venv/bin/agent-hub --serve --lan
Environment=PYTHONUNBUFFERED=1
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now agent-hub
journalctl --user -u agent-hub -f
```

macOS (launchd): `launchctl plist` با همان `ExecStart` و `KeepAlive=true`.
ویندوز: Task Scheduler → *At log on* → `agent-hub.cmd --serve --lan`.

## ۵. pairing: QR و لینک عمیق

هر دو اپ، لینک `agenthub://connect?url=…&token=…&session=…` را می‌فهمند:

```bash
# ساخت QR روی کامپیوتر (pip install qrcode[pil])
python - <<'PY'
import qrcode
qrcode.make("agenthub://connect?url=http://192.168.1.42:8765&token=YOUR_TOKEN").save("hub-qr.png")
PY
xdg-open hub-qr.png        # یا open / start
```

در اپ هم می‌توانید آدرس را از کلیپ‌بورد Paste کنید (متن شامل `://` → فیلد آدرس،
در غیر این صورت → فیلد توکن).

## ۶. UI چه چیزهایی دارد

| تب | کار | API پشتش |
|---|---|---|
| Chat | گفت‌وگو، جریان زنده‌ی رویدادها، Stop، Reset context | `WS /ws` + `POST /api/run` |
| Approvals | کارت «این ابزار اجرا شود؟» با ریسک و ورودی‌ها | `GET/POST /api/sessions/{id}/approvals` |
| Tools | جست‌وجو/فیلتر بر اساس دسته، اسکیما، اجرای دستی | `GET /api/tools`, `POST /api/tools/{name}/invoke` |
| Reports | گزارش کار انجام‌شده، «قدم بعدی»، برنامه‌های باز، افزودن یادداشت | `GET /api/reports`, `GET/POST/DELETE /api/memory` |
| Keys | پروفایل‌های کلید API، فعال‌سازی، Import from .env | `/api/keys*` |
| Settings | وضعیت سرور، پروفایل ایجنت، session های فعال، تم/زبان | `/api/status`, `/api/profiles`, `/api/sessions` |

* RTL فارسی خودکار است (`dir` روی `<html>` ست می‌شود) و فاصله‌ها با logical props
  نوشته شده‌اند تا در هر دو جهت درست بماند.
* Safe-area برای نوچ/Home indicator لحاظ شده؛ حالت تیره/روشن با `prefers-color-scheme`
  و دکمه‌ی تم.
* آفلاین: `sw.js` فقط پوسته را کش می‌کند و `/api/` و `/ws` هرگز کش نمی‌شوند؛
  قطع شدن WS با backoff (۰٫۷s → ۱۵s) دوباره وصل می‌شود و `pending_approvals`
  در `hello` دوباره پخش می‌شوند.

## ۷. چند کاربر و TLS

پیش‌فرض «توکن مشترک» است — برای خودتان و دستگاه‌هایتان کافی است. برای تیم:

1. TLS جلوی سرور (Caddy نمونه):

```
# Caddyfile
hub.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

```bash
SERVER_HOST=127.0.0.1 SERVER_TOKEN=<long-random> caddy run
```

2. برای هر کاربر یک session با پروفایل و محدودیت جدا (`POST /api/sessions` با
   `profile: "read_only"`، `enable_confirmation: true`).
3. در `agent-hub --serve`، توکن را در محیط سرویس (نه فایل world-readable) بگذارید؛
   `systemd` → `Environment=` یا `LoadCredential`.
4. `SERVER_ALLOW_DIRECT_TOOLS=false` بماند مگر برای ادمین‌های مورد اعتماد
   (این فلگ یعنی «هر کس با توکن می‌تواند ابزار را مستقیم اجرا کند»).

برداشتن `--insecure` روی شبکه‌ی عمومی = دادن shell شما به همان شبکه. سرور عمداً
`Host` ناشناس را `403 bad_host` می‌کند و مسیرهای پرهزینه را نرخ‌بندی می‌کند، ولی
این‌ها جای TLS را نمی‌گیرند.

## ۸. عیب‌یابی

| نشانه | علت و کار |
|---|---|
| `Cannot reach the server` | فایروال inbound، `--lan` نزده‌اید، VLAN متفاوت، `192.168.x` اشتراک‌یافته نه |
| `401` در اپ | توکن کپی‌شده کامل نیست؛ سرور ریستارت شده و توکن تازه ساخته |
| اپ باز می‌شود ولی «No API key» | تب Keys → افزودن کلید یا `POST /api/keys/import-env` |
| تأیید می‌آید ولی دکمه‌ها کار نمی‌کنند | session عوض شده (X-Agent-Session)؛ از صفحه‌ی Settings سشن را انتخاب کنید |
| ابزار اجرا شد ولی تأیید نیامد | `auto_confirm_all=true` در آن سشن، یا `poll_grace` بدون کلاینت متصل → deny خودکار |
| iOS صفحه‌ی سفید | Local Network permission؛ یا http روی LTE (فقط Wi‑Fi محلی) |
| باینلی macOS باز نمی‌شود | `xattr -d com.apple.quarantine agent-hub` (امضا نشده) |
| پورت اشغال است | `SERVER_PORT=8766` یا `--port 8766` |
| لاگ می‌خواهید | `LOG_FILE=…` + `agent-hub --serve`؛ خروجی‌ها در `~/.universal-agent-hub/` |

چک‌لیست سلامت:

```bash
curl -s http://127.0.0.1:8765/healthz                 # {"ok":true,"version":…}
curl -s -H "Authorization: Bearer $TOKEN" http://…/api/status | python -m json.tool
```

`/api/status` باید `ready: true`، `tool_count: 23` و `stats: {sockets,runs,approved,denied}` بدهد.

## ۹. حریم خصوصی

* آنچه روی گوشی ذخیره می‌شود: آدرس سرور + توکن (اندروید: AES-GCM با Android
  Keystore؛ iOS: Keychain با `AfterFirstUnlockThisDeviceOnly`). بکاپ ابری برای
  هر دو خاموش است.
* آنچه روی کامپیوتر می‌ماند: کلیدهای API (ماسک‌شده در همه‌ی پاسخ‌ها), تاریخچه‌ی
  سشن‌ها در حافظه تا `SERVER_SESSION_TTL`، لاگ‌ها با masking.
* هیچ telemetri‌ای از اپ یا سرور بیرون نمی‌رود؛ تنها ترافیک خروجی، درخواست‌های
  خودِ ابزارها (LLM API، جست‌وجو، مرورگر) است.

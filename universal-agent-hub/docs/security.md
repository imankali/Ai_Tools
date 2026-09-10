# امنیت

ایجنت **عمداً** دسترسی واقعی به سیستم دارد — این کل ایده است. پس امنیت در
«حذف دسترسی» نیست، در **چند لایه‌ی مستقل** است که هیچ‌کدام به دیگری اعتماد ندارد.

```
┌ لایه ۱  پیکربندی ────────── پیش‌فرض امن، bind loopback، توکن اجباری روی شبکه
├ لایه ۲  HTTP ────────────── توکن · محدودساز نرخ · سقف حجم · بررسی Host · no-referrer
├ لایه ۳  Session ─────────── اجرای سریالی · TTL · deny در قطع اتصال · deny با timeout
├ لایه ۴  SafetyGuard ─────── مسیر/دستور/شبکه · ریسک · سیاست confirm یا deny
├ لایه ۵  ابزار ───────────── validate_input · sensitive_parameters · سقف خروجی
├ لایه ۶  لاگ/پاسخ‌ها ─────── redact کلیدها در لاگ، history، خطا و API
└ لایه ۷  دیسک ────────────── keystore ۰۶۰۰ + امضای HMAC · بدون بکاپ ابری در اپ‌ها
```

## ۱. پیش‌فرض‌ها (secure by default)

| تنظیم | پیش‌فرض | چرا |
|---|---|---|
| `SERVER_HOST` | `127.0.0.1` | هیچ چیزی بیرون از دستگاه دیده نمی‌شود |
| bind غیرلوپ‌بک + بدون توکن | **سرور بالا نمی‌آید** | فراموشی توکن ≠ دادن shell به LAN |
| `SERVER_ALLOW_DIRECT_TOOLS` | `false` | اجرای مستقیم ابزار فقط با تصمیم صریح کاربر |
| `SERVER_RATE_LIMIT` | `30` | کند کردن brute-force و سوءمصرف هزینه‌ی API |
| `SERVER_APPROVAL_TIMEOUT` | `180s` → deny | تأیید معلق نمی‌ماند؛ «سکوت = رد» |
| `ENABLE_SAFETY_GUARD` | `true` | خاموش‌کردنش یک flag صریح است، نه تصادفی |
| `DANGEROUS_COMMAND_POLICY` | `confirm` | `deny` هم هست؛ `off` فقط با آگاهی |
| `UNRESTRICTED_FILESYSTEM` | `false` | ابزار فایل در `ALLOWED_DIRECTORIES` قفل است |
| `ENABLE_AUTO_CONFIRM_ALL` | `false` | حالت «بدون سؤال» انتخابی و در لاگ مشخص است |

`agent-hub --serve --lan` یک توکن تصادفی ۳۲ بایتی می‌سازد و یک‌بار چاپ می‌کند؛
`python -m src.server` روی bind باز بدون توکن `exit 2` می‌دهد.

## ۲. نگهبان (`src/utils/safety.py`)

سه نقطه‌ی ارزیابی، همگی بر رشته‌ی **خام** (نه خروجی پارس‌شده) کار می‌کنند تا
دورزدن با quoting سخت شود:

* `assess_command(cmd)` → تجزیه با `shlex` + تشخیص ویژگی‌ها (`ShellFeature`:
  pipe, redirect, subshell, env-assignment, `sudo`, …) و تطبیق regex روی کل
  رشته. `rm -rf /`, `mkfs`, `dd of=/dev/…`, fork-bomb, `curl | sh`, نوشتن روی
  `/etc`, `chmod 777 /` و ده‌ها مورد دیگر → `blocked` یا `needs_confirmation`.
* `assess_path(path, action=)` → resolve (بدون follow کورِ symlink)، بررسی
  `allowed_directories`، read-only patterns، مسیرهای سیستمی محافظت‌شده
  (`/etc`, `/usr`, `/System`, `C:\\Windows`, `HKLM\\…`) و فایل‌های حساس
  (`.ssh/`, `.aws/credentials`, `.env`, `id_rsa`, `keychain`, `shadow`).
* `assess_network(url)` → scheme، DNS-rebinding، `file://`/`gopher://`، و
  میزبان‌های خصوصی برای ابزار مرورگر (جلوگیری از SSRF به `169.254.169.254`).

`SafetyDecision` همیشه `allowed`, `risk`, `reasons` را دارد و همان دلایل در کارت
تأیید به کاربر نشان داده می‌شود — کاربر با دانستن «چرا خطرناک است» تصمیم می‌گیرد.

سیاست‌ها:

```
DANGEROUS_COMMAND_POLICY=confirm   # پیش‌فرض: سؤال از کاربر
DANGEROUS_COMMAND_POLICY=deny        # هر چیز خطرناک، بدون سؤال رد
DANGEROUS_COMMAND_POLICY=off         # فقط برای sandbox‌های یک‌بارمصرف
```

## ۳. تأیید انسانی (approval)

```
tool.run(arguments, context)
  └─ safety_check → requires_confirmation?
        ├─ CLI:      prompt (y/N/always)
        ├─ سرور:     ApprovalBroker.request() → approval_request روی WS + /api/…/approvals
        │             await Future (تا SERVER_APPROVAL_TIMEOUT)
        └─ هیچ‌کدام:  ToolResult.fail(error_code="confirmation_unavailable")
```

* **بدون کلاینت = deny.** اگر گوشی خواب است و WS قطع، درخواست بلافاصله رد می‌شود؛
  ایجنت ادامه می‌دهد و به کاربر می‌گوید تأیید نشد.
* قطع شدن WS → `cancel_all(reason="no client connected")`.
* `poll_grace`: کلاینتی که فقط REST polling می‌کند هم مهلت دارد؛ خواندن
  `GET …/approvals` پنجره‌ی رأفت را باز می‌کند.
* `request_id` کهنه → `409 stale_approval` (دوباره‌زدن، اجرای تکراری نمی‌سازد).
* «Approve for this run» (= `always`) فقط برای همان سشن و همان اجرا معتبر است و
  در `safety_summary()` قابل مشاهده است.

## ۴. کلید API و keystore

* کلید مدل فقط روی کامپیوتر اجراست: `OPENAI_API_KEY` در `.env` یا فایل keystore.
* **هرگز** از HTTP به بیرون نمی‌رود. `/api/keys` و `/api/status` فقط `masked_key`
  می‌دهند (`sk-a…9f2c`).
* فایل `~/.universal-agent-hub/keys.json` با `chmod 600` و امضای HMAC
  (کلید امضا کنارش، `…-keys.sig`); دست‌کاری دستی → هشدار و رد بارگذاری.
* `max 512` کاراکتر برای کلید؛ کاراکترهای کنترلی/خط جدید مجاز نیست (تزریق در فایل).
* ماسک‌کردن در همه‌جا: `redact_secrets()` برای لاگ و `redact_text()` برای پاسخ‌ها —
  الگوها `sk-…`, `gsk_…`, `ghp_…`, `github_pat_…`, `xox…`, `AIza…`, `tvly-…`.
  تاریخچه‌ی گفت‌وگو هم پیش از سرو شدن به کلاینت‌ها ردکس می‌شود.

## ۵. حملات مرورگری (PWA)

* UI هیچ‌وقت `innerHTML` را با متن خام مدل پر نمی‌کند؛ همه‌چیز از `esc()` رد می‌شود.
* خروجی ابزار در `<pre>` فرار‌شده نمایش داده می‌شود (پیش‌فرض Markdown رندر نمی‌شود).
* CSP در `index.html`: `default-src 'self'`, `connect-src 'self' ws: wss:`,
  بدون `eval`, بدون اسکریپت third-party، بدونfont/CDN.
* Service Worker فقط فایل‌های استاتیک را کش می‌کند؛ `/api/` و `/ws` **هرگز**.
* توکن در `localStorage` است (نه cookie) → CSRF عملاً منتفی است؛ چون هیچ درخواست
  خودکار با هدر احراز هویت ساخته نمی‌شود. `handle_status` هم `Host` را می‌سنجد.
* لینک‌های داخل چت فقط با `https?://` کلیک‌پذیرند (`rel="noopener"`).

## ۶. اپ‌های موبایل

* اندروید: توکن با AES-GCM و کلید Android Keystore؛ `allowBackup=false` و
  `dataExtractionRules` → نه بکاپ Google، نه Transfer دستگاه جدید.
* iOS: Keychain با `AfterFirstUnlockThisDeviceOnly`.
* هر دو: بدون دسترسی فایل/مخاطب/مکان/دوربین؛ فقط `INTERNET` (+ اعلان و لرزش).
* پل بومی (`HubNative`) فقط اعلان/کپی/لرزش است — هیچ API اجرای کد ندارد.
* لینک عمیق `agenthub://connect?url=…&token=…`: `url` فقط بعد از موفقیت
  `GET /api/status` مصرف می‌شود، پس یک QR مخرب نمی‌تواند توکن شما را به سرور
  مهاجم بفرستد (توکن در همان صفحه دستی تأیید می‌شود).

## ۷. شبکه‌ی واقعی: TLS

برای استفاده‌ی خارج از خانه/دفتر، TLS الزامی است (طرح: سرور روی loopback، proxy
بیرونی):

```
# Caddyfile
hub.example.com {
    header Strict-Transport-Security "max-age=31536000"
    reverse_proxy 127.0.0.1:8765
}
```

```bash
SERVER_HOST=127.0.0.1 SERVER_TOKEN="$(openssl rand -base64 24)" caddy run
```

یا تانل ساده: `ssh -L 8765:127.0.0.1:8765 host` / `tailscale serve`.

## ۸. چک‌لیست سخت‌سازی

- [ ] `SERVER_TOKEN` بلند و تصادفی (`openssl rand -base64 24`)، در محیط سرویس نه در shell history
- [ ] TLS یا SSH tunnel برای هر چیز غیر LAN قابل‌اعتماد
- [ ] `DANGEROUS_COMMAND_POLICY=deny` برای سشن‌های تیمی / `read_only` برای دموها
- [ ] `ALLOWED_DIRECTORIES=./workspace` + `UNRESTRICTED_FILESYSTEM=false`
- [ ] `SERVER_ALLOW_DIRECT_TOOLS=false` (مگر برای خودتان)
- [ ] `MAX_TOOL_ITERATIONS` و `RATE_LIMIT` با بودجه‌ی API شما هم‌راستا
- [ ] `LOG_FILE` + `EVENTS_LOG_FILE` روی دیسک رمزنگاری‌شده (لاگ‌ها خروجی دستور را دارند)
- [ ] کاربر سیستمی جدا برای سرور؛ نه root/administrator
- [ ] بعد از کار حساس: `rm -rf ~/.universal-agent-hub` (keystore و لاگ‌های تست)
- [ ] مرور دوره‌ای `GET /api/status` → `stats` (چند اجرا، چند تأیید، چند رد)

## ۹. تهدیدهای شناخته‌شده و موضع ما

| تهدید | موضع |
|---|---|
| Prompt injection از وب/فایل که ایجنت می‌خواند | واقعیت دارد و قابل حذف نیست؛ راه ما: تأیید انسانی برای نوشتن/اجرا، `read_only` برای مرور، سقف iterate، و `tool.requested` در لاگ |
| سرور روی LAN بدون توکن | bind باز بدون توکن رد می‌شود؛ `--insecure` صریح و پرخطر است |
| توکن لو‌رفته | کنترل کامل همان سشن‌ها → `SERVER_TOKEN` را بچرخانید (restart) و سشن‌ها را ببندید؛ فقط `~/.universal-agent-hub` را پاک کنید |
| بدنه‌ی بزرگ / flood | `SERVER_MAX_BODY_BYTES` + محدودساز نرخ + صف سریالی هر سشن |
| DNS rebinding | بررسی `Host` (ناشناخته → `403 bad_host`) |
| SSRF از ابزار مرورگر | `assess_network` + مسدودسازی metadata IPs + فقط http/https |
| لاگ‌خوانی کلید | redact در logger و در همه‌ی پاسخ‌ها؛ فایل‌ها ۰۶۰۰ |
| بدافزار روی دستگاه شما | خارج از تهدید مدل ماست: ایجنت همان‌قدر دسترسی دارد که شما به آن دادید؛ اگر ماشین آلوده است، اول آن را تمیز کنید |

## ۱۰. گزارش آسیب‌پذیری

برای گزارش خصوصی، Issue عمومی نسازید: از بخش Security Advisory در
`https://github.com/imankali/Ai_Tools` استفاده کنید. تغییرات امنیتی در CHANGELOG
و `pyproject.toml` (version bump) اعلام می‌شوند؛ هیچ‌وقت با «خاموش‌کردن ایمنی»
باگ را حل نمی‌کنیم.

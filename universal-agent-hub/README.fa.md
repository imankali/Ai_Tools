# یونیورسال اجنت هاب (Universal Agent Hub)

**یک ایجنت تولیدی با دسترسی واقعی — و قابل‌کنترل — به سیستم؛ روی همه‌ی سیستم‌عامل‌ها،
از طریق ترمینال، پنجره‌ی دسکتاپ، یا گوشی.**

به ماشین خودتان یک کلید API می‌دهید. ایجنت محیط را می‌خواند، نقشه می‌کشد، از ابزارها
(شل، فایل، مرورگر، جست‌وجو، اطلاعات سیستم) استفاده می‌کند، یاد می‌گیرد چه چیزی جواب داد و
پیش از هر کار خطرناک از شما اجازه می‌گیرد — و بعد گزارش می‌دهد دقیقاً چه کرده است.

```
        کامپیوتر شما                              گوشی / لپ‌تاپ
┌───────────────────────────────┐          ┌────────────────────────────────┐
│ UniversalAgent · SafetyGuard  │  REST+WS │ اپ اندروید · اپ iOS · PWA      │
│ ۲۳ ابزار · حافظه · گزارش‌ها    │ ◄──────► │ چت · تأییدها · ابزارها ·       │
│ src/server (aiohttp, توکن)    │  توکن    │ کلیدها · تنظیمات · گزارش‌ها    │
└───────────────────────────────┘          └────────────────────────────────┘
        کلید API هرگز از این ماشین بیرون نمی‌رود
```

---

## شروع ۶۰ ثانیه‌ای

```bash
git clone https://github.com/imankali/Ai_Tools && cd Ai_Tools/universal-agent-hub
make install                                   # venv + وابستگی‌ها
cp .env.example .env && $EDITOR .env           # OPENAI_API_KEY=…

make run                                       # گفت‌وگوی تعاملی در ترمینال
make serve-lan                                 # UI گوشی: آدرس + توکن یک‌بارمصرف چاپ می‌کند
```

بعد آدرس چاپ‌شده را در گوشی (یا مرورگر) باز کنید و توکن را وارد کنید. تمام.

نصب با یک خط فرمان:

```bash
curl -fsSL https://raw.githubusercontent.com/imankali/Ai_Tools/main/universal-agent-hub/scripts/install.sh | bash          # macOS / لینوکس
powershell -c "irm https://raw.githubusercontent.com/imankali/Ai_Tools/main/universal-agent-hub/scripts/install.ps1 | iex"  # ویندوز
```

## چه چیزی به شما می‌دهد

| | |
|---|---|
| **هسته‌ی ایجنت** | حلقه‌ی async ابزار-خوانی مدل، با fallback مدل، retry، شمارش توکن، پنجره‌ی تاریخچه، جریان رویداد |
| **۲۳ ابزار** | شل: `terminal_run` · فایل: `read_file` `write_file` `list_directory` `search_files` `move_file` `delete_file` · سیستم: `os_info` `cpu_info` `memory_info` `disk_info` `network_info` · وب: `web_search` `search_advanced` `browser_browse` `browser_screenshot` `browser_extract_text` `browser_click` `browser_fill_form` · حافظه: `memory_write` `memory_read` `memory_search` `memory_forget` |
| **لایه‌ی ایمنی** | `SafetyGuard`: ارزیابی ریسک دستور/مسیر/شبکه، allow-list، سیاست deny یا confirm، ماسک‌کردن کلیدها در هر لاگ و پاسخ |
| **تأیید انسانی** | هر کار پرخطر می‌ایستد تا شما در ترمینال/مرورگر/گوشی تأیید کنید؛ timeout یا قطع اتصال = **رد** |
| **حافظه‌ی بلندمدت** | فایل JSONL در `src/core/memory.py`: ترجیحات، رویه‌ها، تصمیم‌ها، برنامه‌های باز — خودکار به prompt اضافه و پس از اجرا ثبت می‌شود |
| **گزارش خودبینی** | `agent-hub --report` و تب Reports: هر روز چه کرده، به کدام ابزارها تکیه کرده، قدم بعدی‌اش چیست و کجا قفل شده |
| **خودابزاری** | ابزارهای نوشته‌شده توسط ایجنت از پوشه‌ی پلاگین (`AGENT_HUB_TOOL_DIRS`) بارگذاری می‌شوند و نوشتنشان نیازمند تأیید شماست؛ بازخورد در حافظه ثبت می‌شود (`--remember`) و ایجنت می‌تواند پیشنهاد پچ را در `proposals/` بنویسد و `make test` را اجرا کند — commit را انسان می‌زند، هرگز خود ایجنت |
| **سرور API** | REST + WebSocket + PWA روی aiohttp؛ احراز توکن، محدودساز نرخ، ایزولاسیون session، پروفایل‌های کلید API (ماسک‌شده) |
| **اپ‌ها** | پوسته‌ی اندروید، اپ iOS، پنجره‌ی `--desktop`، و PWA روی همه‌جا |
| **کیفیت** | ۸۰۱ آزمون سبز، پوشش ≥ ۹۲٪، mypy + ruff + black تمیز، CI، داکر، باینلی هر سیستم‌عامل |

## CLI

```bash
agent-hub                                    # گفت‌وگوی تعاملی — /help فهرست دستورهای داخل چت
agent-hub --prompt "ديسك من را چه پر كرده؟"   # یک اجرا
agent-hub --prompt "…" --json                # خروجی ماشین‌خوان
agent-hub --prompt "…" --profile read_only    # مجموعه ابزار محدود
agent-hub --prompt "…" --dry-run              # فقط نشان بده چه چیزی صدا زده می‌شود
agent-hub --serve --lan --print-token         # API + UI برای گوشی و دسکتاپ
agent-hub --serve --desktop                   # پنجره‌ی بومی (pywebview) یا مرورگر
agent-hub --doctor                            # خودسنجی: محیط، کلید، ابزارها، پورت، حافظه
agent-hub --report                            # چه کرد، چه می‌کند، چه هزینه‌ای داشت
agent-hub --remember "پاسخ‌های کوتاه فارسی"     # نوشتن مستقیم در حافظه‌ی بلندمدت
```

دستورهای داخل چت: `/tools` · `/schema` · `/config` · `/safety` · `/model` · `/profile` ·
`/cd` · `/history` · `/events` · `/transcript` · `/save` · `/load` · `/clear` ·
`/memory [list|search|add|forget]` · `/report` · `/doctor`.

## کتابخانه

```python
import asyncio
from src.core.agent_factory import AgentFactory

async def main() -> None:
    agent = AgentFactory.create("developer")          # پروفایل = ابزارها + محدودیت‌ها
    result = await agent.ask("این ریپو را بررسی کن و سه فایل پرریسک را بگو")
    print(result.text)
    print(result.tool_names, result.duration_ms, result.usage.total_tokens)
    await agent.close()

asyncio.run(main())
```

## اپ‌ها و دانلودها

| پلتفرم | راه | راهنما |
|---|---|---|
| اندروید | `agent-hub-android-debug.apk` (اعلان، pairing با QR) | [`apps/android/README.md`](apps/android/README.md) |
| iOS | اپ SwiftUI (Xcode + XcodeGen) یا Safari → Add to Home Screen | [`apps/ios/README.md`](apps/ios/README.md) |
| Termux | ایجنت روی خود گوشی: `scripts/install-termux.sh` | [`docs/apps.md`](docs/apps.md) |
| ویندوز / macOS / لینوکس | `agent-hub --serve --desktop` یا باینلی‌های Release | [`packaging/README.md`](packaging/README.md) |
| داکر | `docker compose up hub-server` | [`Dockerfile`](Dockerfile) |

باینلی آماده‌ی هر سیستم‌عامل به هر Release پیوست می‌شود:
<https://github.com/imankali/Ai_Tools/releases/latest>

گوشی فقط کنترل از راه دور است: ایجنت روی کامپیوتر شما اجرا می‌شود و کلید API هرگز به
گوشی یا مرورگر فرستاده نمی‌شود — در اپ فقط «آدرس سرور» و «توکن» ذخیره می‌شوند
(Android Keystore / iOS Keychain).

## پیکربندی

همه‌چیز با متغیرهای محیطی (`.env` با pydantic-settings) — فهرست کامل با توضیح در
[`.env.example`](.env.example).

| متغیر | پیش‌فرض | معنا |
|---|---|---|
| `OPENAI_API_KEY` | – | کلید مدل (با `OPENAI_BASE_URL` برای هر endpoint سازگار با OpenAI) |
| `MODEL_NAME` / `MODEL_FALLBACKS` | `gpt-6-astra` / فهرست ترتیبی | چه مدلی، به چه ترتیب |
| `ENABLE_SAFETY_GUARD` | `true` | نگهبان؛ خاموش‌کردنش تصمیم صریح شماست |
| `DANGEROUS_COMMAND_POLICY` | `confirm` | `confirm` · `deny` · `off` |
| `ALLOWED_DIRECTORIES` | `.` (ریشه‌ی پروژه) | مجوز نوشتن ابزار فایل |
| `UNRESTRICTED_FILESYSTEM` | `false` | `true` = کل دیسک، تأییدها همچنان فعال |
| `MAX_COMMAND_TIMEOUT` / `MAX_TOOL_ITERATIONS` | `60` / `10` | محافظ در برابر حلقه |
| `MEMORY_ENABLED` / `MEMORY_AUTO_CAPTURE` | `true` / `true` | حافظه‌ی بلندمدت |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8765` | bind سرور |
| `SERVER_TOKEN` | – | برای هر bind غیر از loopback اجباری |
| `SERVER_ALLOW_DIRECT_TOOLS` | `false` | `POST /api/tools/{name}/invoke` (پنل مدیریت) |
| `SERVER_APPROVAL_TIMEOUT` | `180` | ثانیه انتظار برای تأیید شما، سپس رد |

## مدل امنیتی در یک پاراگراف

ایجنت دسترسی‌های واقعی شما را دارد، پس محافظ «سندباکس» نیست؛ **ارزیابی + رضایت +
قابل‌بازبینی بودن** است. `SafetyGuard` هر دستور/مسیر/آدرس را ریسک‌بندی می‌کند
(`safe → critical`)، غیرقابل‌بازگشت‌ها را می‌بندد (`rm -rf /`، `dd of=/dev/sda`، نوشتن در
`/etc`، IP متادیتای ابر)، بقیه را می‌پرسد؛ درخواست‌های تأیید با timeout **رد** می‌شوند نه
قبول؛ و هر کلید پیش از رسیدن به لاگ، پیام خطا یا گوشی شما ماسک می‌شود.
متن کامل، مدل تهدید و چک‌لیست سخت‌سازی: [`docs/security.md`](docs/security.md) و
[`docs/autonomy.md`](docs/autonomy.md).

## مستندات

| سند | محتوا |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | لایه‌ها، چرخه‌ی درخواست، نقطه‌های توسعه، تصمیم‌های طراحی |
| [`docs/server_api.md`](docs/server_api.md) | همه‌ی مسیرهای REST، پروتکل WebSocket، کدهای خطا |
| [`docs/apps.md`](docs/apps.md) | pairing، اندروید/iOS/دسکتاپ/Termux/داکر، TLS، عیب‌یابی |
| [`docs/security.md`](docs/security.md) | درون نگهبان، تأییدها، keystore، سخت‌سازی |
| [`docs/autonomy.md`](docs/autonomy.md) | حافظه، برنامه‌ریزی، گزارش‌ها، خودابزاری، سطح خودمختاری |
| [`docs/adding_tools.md`](docs/adding_tools.md) | ساخت یک ابزار در ۲۰ دقیقه، تست، انتشار |
| [`docs/api_reference.md`](docs/api_reference.md) | کلاس‌ها و تابع‌های عمومی |
| [`examples/`](examples) | اسکریپت‌های قابل‌اجرا |

## توسعه

```bash
make install && make test      # pytest + سقف پوشش ۸۰٪
make lint                      # ruff + black --check + mypy
make fmt                       # black + ruff --fix
make serve                     # سرور روی loopback
```

آزمون‌ها هرگز شبکه یا مدل واقعی را صدا نمی‌زنند: `tests/fakes.py` یک کلاینت جعلی
OpenAI می‌دهد و تست‌های ابزار همه‌ی فراخوانی‌های خروجی را monkeypatch می‌کنند.

## حریم خصوصی

Local-first. هیچ telemetri‌ای وجود ندارد؛ no analytics، no CDN در UI. ترافیک خروجی فقط
همان چیزی است که کار شما ایجا می‌کند (endpoint مدل، سایت‌هایی که ابزار مرورگر/جست‌وجو
باز می‌کنند). لاگ‌ها، حافظه و پروفایل‌های کلید در `~/.universal-agent-hub/` هستند و با
`rm -rf ~/.universal-agent-hub` پاک می‌شوند.

## سلب مسئولیت

این ابزار می‌تواند دستور دلخواه روی همان ماشین اجرا کند. فقط روی سیستمی استفاده‌اش کنید که
مالک آن هستید یا اجازه‌ی صریح دارید، `SERVER_TOKEN` را مخفی نگه دارید، و نگهبان ایمنی را
خاموش نکنید. هر عمل غیرقابل‌بازگشت (پول، حذف، انتشار، ثبت‌نام) عمداً به تأیید انسانی نیاز
دارد؛ مسئولیت نتیجه با شماست.

## لایسنس

MIT — ببینید [LICENSE](LICENSE).

# معماری

Universal Agent Hub یک هسته‌ی پایتون است که اطراف آن سه چیز ساخته شده: یک
**رجیستری ابزار**، یک **لوله‌ی ایمنی**، و یک **لایه‌ی سرویس (REST + WebSocket + PWA)**.
هیچ لایه‌ای هسته‌ی ایجنت را دور نمی‌زند؛ همه از همان `UniversalAgent` استفاده می‌کنند.

```
                         ┌──────────────────────────────────────────────┐
   اپ‌ها / CLI          │  src/server  (aiohttp)                       │
 ┌─────────────┐   JSON │   auth.py · protocol.py · app.py · sessions.py│
 │ Android APK │◄──────►│   keystore.py   web/ (PWA)                    │
 │ iOS · PWA   │        └───────────────┬──────────────────────────────┘
 │ desktop     │                        │ AgentFactory.create(profile=…)
 └─────────────┘                        ▼
                         ┌──────────────────────────────────────────────┐
                         │  src/agent.py  UniversalAgent                │
                         │   ask() → حلقه‌ی tool-calling با fallback مدل │
                         └───┬───────────────┬────────────────┬─────────┘
                             │               │                │
                    ┌────────▼───────┐ ┌─────▼──────┐ ┌───────▼────────┐
                    │ core/          │ │ utils/     │ │ models/        │
                    │ tool_registry  │ │ safety.py  │ │ tool_models    │
                    │ base_tool      │ │ validators │ │ agent_models   │
                    │ event_bus      │ │ helpers    │ │ config_models  │
                    │ agent_factory  │ │ logger.py  │ └────────────────┘
                    └────────┬───────┘ └─────┬──────┘
                             │               │
                    ┌────────▼───────────────▼──────┐
                    │ src/tools/*  (۲۳ ابزار ثبت‌شده)│
                    │ terminal · filesystem · browser│
                    │ web_search · system_info       │
                    └───────────────────────────────┘
```

## ۱. چرخه‌ی یک درخواست

```
کاربر → agent.ask("…")
  1. llm.requested      Payload: messages + tool schemas (OpenAI function calling)
  2. llm.responded      مدل یا متن نهایی می‌دهد یا tool_calls
  3. tool.requested     برای هر فراخوانی: اعتبارسنجی ورودی با JSON Schema
  4. safety_check       نگهبان: blocked / needs_confirmation / ok
  5. confirm            اگر لازم بود: CLI prompt یا approval کارت در اپ
  6. execute            کار واقعی (async → run_in_thread برای I/O بلاک‌کننده)
  7. tool.completed     نتیجه → پیام role="tool"
  8. (۲..۷ تا max_tool_iterations یا پایان)
  9. agent.completed    AgentRunResult(text, tool_calls, usage, duration_ms, events)
```

هر مرحله روی `EventBus` منتشر می‌شود؛ `src/server` همان رویدادها را بدون تغییر
به `event` در WebSocket تبدیل می‌کند، پس UI دسکتاپ و موبایل دقیقاً همان چیزی را
می‌بینند که لاگ می‌نویسد.

## ۲. لایه‌ها

| لایه | فایل‌ها | مسئولیت |
|---|---|---|
| Config | `src/config.py` | pydantic-settings؛ `.env` + env؛ validaterها؛ مسیرهای حل‌شده؛ بلوک `server_*` |
| Models | `src/models/*.py` | `ToolResult`, `ToolCall`, `AgentRunResult`, `UsageStats`, `AgentEvent`, `RiskLevel` |
| Core | `src/core/*.py` | رجیستری ابزار، `BaseTool`، ایونت‌باس، `AgentFactory` + پروفایل‌ها |
| Safety | `src/utils/safety.py` | `SafetyGuard`: دستور/مسیر/شبکه، ریسک، سیاست confirm/deny |
| Utils | `src/utils/{helpers,validators,logger}.py` | truncate/redact/validate، لاگ masking |
| Tools | `src/tools/*.py` | ابزارهای واقعی؛ هر فایل خودثبت‌شده با `@register_tool` |
| Agent | `src/agent.py` | حلقه‌ی LLM، retry/fallback، تاریخچه، تأیید |
| CLI | `src/cli.py` | گفت‌وگوی تعاملی، تک‌اجرایی، پروفایل‌ها، `--serve` |
| Server | `src/server/*` | REST + WS + احراز هویت + keystore + سرو PWA |
| UI | `src/server/web/` | PWA بدون build (چت/ابزارها/کلیدها/تنظیمات) |
| Apps | `apps/android`, `apps/ios` | پوسته‌های native برای همان UI |

## ۳. رجیستری ابزار (`src/core/tool_registry.py`)

`ToolRegistry` یک dict کلاس‌هاست (نه نمونه‌ها). نمونه‌سازی هنگام اجرا و با `config`
همان لحظه انجام می‌شود؛ به همین دلیل تغییر تنظیمات نیازی به ریستارت ندارد.

| متد | کاربرد |
|---|---|
| `register(cls)` / `@register_tool` | ثبت کلاس (نام از `ToolInfo.name` می‌آید) |
| `unregister(name)` · `clear()` | برای تست و پروفایل‌ها |
| `discover(package, force=)` | اسکن پکیج + import هر ماژول |
| `names()` · `classes()` · `iter_names()` | بازتاب |
| `get(name, config=…)` · `get_all()` · `instances(only=…)` | ساخت نمونه |
| `schemas(only=…)` · `get_schemas()` | آرایه‌ی `{"type":"function","function":{…}}` برای SDK |
| `info()` | `ToolInfo` ها (risk، requires_confirmation، category) |
| `register_module(module, prefix=…)` | ثبت یک ماژول سفارشی (افزونه) |
| `build_context(config, safety, bus, confirm)` | ساخت `ToolContext` |

اگر پکیج قابل اسکن نباشد (باینلی PyInstaller/zipapp)، `discover` به
`BUILTIN_TOOL_MODULES` برمی‌گردد تا برنامه بی‌ابزار نشود.

## ۴. `BaseTool` (`src/core/base_tool.py`)

قرارداد: یک کلاس با `name`, `description`, `parameters`, `category`, `risk_level`,
`requires_confirmation`, و یک `async execute(**kwargs) -> ToolResult`.

`BaseTool.run()` کارهای عرضی را انجام می‌دهد تا ابزارها تمیز بمانند:

1. اعتبارسنجی ورودی با همان JSON Schema (خطا → `invalid_input`)؛
2. `safety_check()` و نگهبان (`blocked` / `declined` / `confirmation_unavailable`)؛
3. تأیید کاربر از طریق `context.confirm` (CLI/اپ/هر چیز دیگر)؛
4. `execute()` با سقف خروجی (`max_output_chars`, `truncated=True`) و masking؛
5. انتشار `tool.started/completed/failed` روی باس؛
6. همیشه `ToolResult` برمی‌گرداند — هیچ استثنایی به حلقه‌ی ایجنت نشت نمی‌کند.

## ۵. پروفایل‌ها (`src/core/agent_factory.py`)

`AgentProfile` = `enabled_tools` / `disabled_tools` / `temperature` /
`max_tool_iterations` / `model` / `auto_confirm_all` / `system_prompt` / config overrides.

| پروفایل | ابزار | نکته |
|---|---|---|
| `generalist` | همه (۲۳) | حالت پیش‌فرض CLI |
| `read_only` | همه به‌جز ۸ ابزار نوشتن/حذف/شِل | مرور امن، آموزش، دموی زنده |
| `developer` | terminal + فایل‌ها + جست‌وجو | بدون اتوماسیون مرورگر |
| `ops` | terminal + system_info | تأیید همیشه روشن |

`AgentFactory.create("developer", tools=[…])` و `AgentFactory.tools_for(profile)`
(دقیقاً همان چیزی که `/api/profiles` و UI نشان می‌دهند). پروفایل تازه را می‌توان
با `AgentFactory.register(AgentProfile(...))` در کد، یا با `AgentFactory.load_profiles_from_dir(dir)` از فایل‌های JSON اضافه کرد
(`load_profiles_from_dir`).

## ۶. رویدادها (`src/core/event_bus.py`)

* الگوی subscription با wildcard: `subscribe("tool.*", handler)`، `"#"` برای همه.
* `publish_nowait` برای مسیرهای حساس به تأخیر، `emit` برای await کردن هندلرها
  (هر هندلر با timeout ۱۰s و خطای خورده → لاگ warning، نه سقوط).
* `history` / `recent(pattern, limit=)` → بازپخش برای کلاینت تازه‌متصل‌شده
  (`hello.recent_events`).
* `wait_for("agent.completed", timeout=…)` — ابزار تست و همگام‌سازی.
* `install_default_observers(bus, log=…, jsonl=…)` — لاگ Rich + فایل JSONL
  (`EVENTS_LOG_FILE`).

## ۷. سشن‌ها در سرور (`src/server/sessions.py`)

```
SessionRegistry ── id → AgentSession
                        ├─ UniversalAgent (lazily built, invalidated on key/profile change)
                        ├─ events: deque (برای replay)
                        ├─ subscribers: set[Queue]  ← fan-out WS
                        └─ ApprovalBroker ─ request_id → Future
```

* **سریالی‌سازی اجرا**: هر سشن یک `asyncio.Lock` دارد؛ دو `run` هم‌زمان روی یک
  سشن مجاز نیست (تاریخچه خراب نشود). `POST /api/run/stop` و `cancel` در WS،
  تسک جاری را لغو می‌کنند.
* **ApprovalBroker**: ابزار `await` می‌کند تا کاربر پاسخ دهد؛ `SERVER_APPROVAL_TIMEOUT`
  که بگذرد → deny. هیچ‌وقت «اجرای خودکار در غیاب کاربر» رخ نمی‌دهد.
* **Publisher در `__init__`**: حتی اگر ایجنت ساخته نشده باشد، درخواست تأیید به
  کلاینت‌ها می‌رسد (خطای قبلی که با `has_channel=False` تأیید را بی‌صدا رد می‌کرد).
* **TTL**: `SessionRegistry.prune()` بعد از `SERVER_SESSION_TTL` سشن را می‌بندد.

## ۸. کلیدهای API (`src/server/keystore.py`)

* فایل JSON `~/.universal-agent-hub/keys.json` (مسیر قابل تغییر با `SERVER_KEYSTORE`).
* هر پروفایل: `name, masked_key, base_url, model, fallbacks, profile, created_at`.
* **کلید خام در فایل ذخیره می‌شود** (روی دیسکِ دستگاه شما، با chmod ۶۰۰) و امضای
  HMAC integrity دارد؛ در **هیچ** پاسخ HTTP نمی‌آید — فقط `masked_key`.
* `merged_config_values()` هنگام ساخت ایجنت در حافظه اعمال می‌شود؛ override دستی
  کاربر اولویت دارد.
* `redact_text()` برای لاگ/خطاها (الگوهای `sk-`, `gsk_`, `ghp_`, `AIza`, …).

## ۹. UI وب (`src/server/web/`)

`index.html` + `styles.css` + `app.js` — بدون framework و بدون build stage:

* `Hub` = کلاینت REST/WS (توکن از `localStorage`، reconnect با backoff، صف پیام
  وقتی قطع است، `?session=` برای چسبیدن به سشن قبلی).
* `hydrateFromLocation()` لینک بومی/QR (`?server=&token=&session=`) را می‌خواند و
  URL را از تاریخچه پاک می‌کند.
* `nativeEvent()` رویدادها را به `HubNative` (اندروید `JavascriptInterface` / iOS
  `WKScriptMessageHandler`) می‌فرستد: اعلان، لرزش، کپی.
* i18n با `data-i18n` + `dir=rtl` برای فارسی؛ logical properties در CSS؛
  safe-area insets؛ service worker که `/api/` و `/ws` را هرگز کش نمی‌کند.

## ۱۰. تست‌ها

```
tests/
  conftest.py          fixture های مشترک + fakes (FakeLLMClient, response builders)
  test_core/           رجیستری، base_tool، event bus، factory، agent loop
  test_tools/          هر ابزار با monkeypatch (هیچ تستی شبکه واقعی نمی‌زند)
  test_utils/          safety، validators، helpers، logger masking
  test_cli.py          خروجی‌ها، حالت‌های non-interactive، dry-run
  test_server/         auth، keystore، sessions، API، WebSocket، CLI --serve (۱۸۳ تست)
```

`make test` با coverage ≥ ۸۰٪ رد می‌شود (اکنون ≈ ۹۲٪). تست‌های شبکه/LLM با
fake‌های `tests/fakes.py` اجرا می‌شوند؛ هیچ آزمون live‌ای در CI نیست تا سریع و
تکرارپذیر بماند.

## ۱۱. نقطه‌های توسعه

| می‌خواهید | کجا |
|---|---|
| ابزار تازه | `src/tools/template.py` را کپی کنید → [`adding_tools.md`](adding_tools.md) |
| پروفایل تازه | `AgentFactory.register(AgentProfile(...))` یا `AgentFactory.load_profiles_from_dir(path)` |
| مدل/SDK تازه | `OPENAI_BASE_URL` + `MODEL_FALLBACKS`؛ یا `agent_class=` در `AgentFactory.create` |
| تأیید سفارشی | `agent.set_confirmation_handler(async (req) -> bool)` |
| شنونده‌ی رویداد | `agent.event_bus.subscribe("#", handler)` (Sentry، Slack، JSONL) |
| API تازه | هندلر در `src/server/app.py` + مدل در `protocol.py` + تست در `tests/test_server` |
| صفحه‌ی UI | `index.html` (markup) + `app.js` (render) + `I18N` (en/fa) |
| دسترسی بیشتر سیستمی | `src/utils/safety.py` (نگهبان) — نه حذفش! |

## ۱۲. تصمیم‌های طراحی

* **رجیستری کلاس‌محور** تا `Config` دیررس به ابزارها برسد (سرور می‌تواند پروفایل
  کلید را بدون ریستارت عوض کند).
* **`ToolResult` به‌جای استثنا**؛ حلقه‌ی ایجنت باید خطای ابزار را به مدل برگرداند
  تا خودش اصلاح کند.
* **احراز هویت لایه‌ی HTTP، ایمنی لایه‌ی ابزار**؛ هیچ‌کدام به دیگری اعتماد ندارد،
  پس CLI و API رفتار یکسان دارند.
* **UI بدون build**؛ برای Termux/داکر/ویندوز یک‌سان سرو می‌شود و آفلاین کار می‌کند.
* `Application` در aiohttp پس از start فریز است؛ state قابل‌تغییر (`ServerStats`)
  یک‌بار در `create_app` ساخته می‌شود — نه `app[KEY] = …` داخل هندلر.

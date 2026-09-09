# قرارداد API سرور (`src/server/`)

مرجع کامل مسیرها، بدنه‌ها و کدهای خطا. پیاده‌سازی: `src/server/app.py`، مدل‌های
ورودی/خروجی: `src/server/protocol.py`، احراز هویت: `src/server/auth.py`.

* Base URL: `http://<host>:8765` (پیش‌فرض `SERVER_HOST=127.0.0.1`, `SERVER_PORT=8765`)
* همه‌ی پاسخ‌ها JSON با `charset=utf-8` هستند (به‌جز فایل‌های UI).
* ورودی‌ها با pydantic اعتبارسنجی می‌شوند؛ بدنه‌ی نامعتبر → `422`.

## احراز هویت

```
Authorization: Bearer <SERVER_TOKEN>
```

یا `?token=<SERVER_TOKEN>` در query (همان چیزی که PWA/اپ‌ها استفاده می‌کنند، چون
`WebSocket` هدر سفارشی نمی‌تواند بفرستد).

| وضعیت سرور | رفتار |
|---|---|
| bind روی loopback و بدون توکن | همه‌ی مسیرها باز، فقط از همان دستگاه |
| bind غیرلوپ‌بک و بدون توکن | `create_app` بالا نمی‌آید (ارور امنیتی) |
| توکن تنظیم‌شده | هر درخواست غیر `/healthz` با ۴۰۱ رد می‌شود |

* `GET /healthz` تنها مسیر بدون احراز هویت است (برای uptime/probe هنگام pairing).
* سرور `Host` ناشناس را `403 bad_host` می‌کند (محافظت در برابر DNS rebinding).
* محدودساز نرخ: `POST /api/run`, `POST /api/tools/{name}/invoke`, `POST /api/keys*`
  → `SERVER_RATE_LIMIT_PER_MINUTE` (پیش‌فرض ۱۲۰). خطا: `429 rate_limited` با
  `details.retry_after` (ثانیه).

### خطاهای استاندارد

بدنه‌ی هر خطا:

```json
{ "error": "متن قابل‌نمایش", "error_code": "machine_code", "details": { } }
```

| `error_code` | HTTP | کِی |
|---|---|---|
| `unauthorized` | 401 | توکن لازم/غلط |
| `bad_host` | 403 | `Host` غیرمنتظره |
| `no_route` | 404 | مسیر `/api/*` ناشناخته |
| `not_found` | 404 | فایل/مسیر UI نبود |
| `no_session` | 404 | `session` نامعلوم |
| `missing_api_key` | 503 | کلید API تنظیم نشده |
| `rate_limited` | 429 | سقف نرخ |
| `invalid_json` / `invalid_message` / `invalid_request` | 400 | بدنه‌ی خراب |
| `unknown_kind` | 400 | نوع پیام WS ناشناخته |
| `invalid_update` | 422 | فیلد غیرمجاز در PATCH سشن |
| `stale_approval` | 409 | `request_id` دیگر باز نیست |
| `nothing_to_import` | 409 | `.env` کلیدی نداشت |
| `disabled` | 403 | `SERVER_ALLOW_DIRECT_TOOLS=false` و کاربر سراغ invoke رفته |
| `unknown_tool` | 404 | نام ابزار در رجیستری نیست |
| `tool_error` / `invalid_input` / `blocked` / `declined` / `confirmation_unavailable` / `non_zero_exit` | داخل `error_code` پاسخ ابزار (HTTP 200) | لایه‌ی ابزار |
| `run_failed` | 500 | استثنا داخل اجرا |
| `ui_broken` | 500 | `web_root` هست ولی `index.html` نه |

## سشن‌ها

شناسه‌ی سشن به سه روش شناخته می‌شود (به همین ترتیب اولویت): مسیر
`/api/sessions/{id}`، هدر `X-Agent-Session: <id>`، و `session_id` در بدنه.

سشن = یک `AgentCore` زنده با تاریخچه‌ی گفت‌وگو، پروفایل ابزار، و صف تأیید خودش.
بی‌استفاده بماند → پس از `SERVER_SESSION_TTL` (پیش‌فرض ۳۶۰۰ ثانیه) بسته می‌شود.

## مسیرها

### سلامت و کشف

| متد | مسیر | توضیح |
|---|---|---|
| `GET` | `/healthz` | بدون احراز هویت؛ `{"status":"ok","version":"1.0.0","model":"gpt-6-astra","tools":19,"auth_required":true,"profiles":[…]}` |
| `GET` | `/api/status` | وضعیت کامل (پایین‌تر) |
| `GET` | `/api/config` | تنظیمات safe (هیچ کلیدی نیست) |
| `GET` | `/api/safety` | خلاصه‌ی pipeline ایمنی از سشن فعال |
| `GET` | `/api/profiles` | پروفایل‌های ایجنت + ابزارهای مؤثر هرکدام |

`GET /api/status`:

```json
{
  "version": "1.0.0",
  "ready": true,
  "ready_hint": null,
  "config": { "model_name": "gpt-6-astra", "safety_enabled": true, "…": "…" },
  "safety": { "blocked_patterns": 12, "auto_confirm_all": false, "…": "…" },
  "auth": { "enabled": true, "mode": "token", "rate_limit_per_minute": 120 },
  "keystore": { "active": "phone", "profiles": [ { "name": "phone", "masked_key": "sk-…3f9a", "model": "gpt-6-astra", "base_url": null } ] },
  "ui_available": true,
  "tool_count": 23,
  "stats": { "sockets": 1, "runs": 7, "approved": 3, "denied": 1 },
  "session": { "id": "…", "profile": "generalist", "active_tools": ["…"], "history_length": 4 },
  "sessions": [ … ],
  "profiles": ["generalist", "read_only", "developer", "ops"],
  "capabilities": { "direct_tool_calls": false, "approvals": true, "websocket": true, "max_body_bytes": 1048576 }
}
```

`ready` دقیقاً یعنی «کلید API تنظیم است»؛ اگر `false` باشد `ready_hint` می‌گوید چه
بکنید (اپ‌ها با دیدن `missing_api_key` کاربر را به تب Keys می‌برند).

### ابزارها

| متد | مسیر | توضیح |
|---|---|---|
| `GET` | `/api/tools?category=&q=` | فهرست ابزارها؛ `q` نام **یا** توضیح را می‌گردد |
| `GET` | `/api/tools/{name}` | اسکیما + متادیتا (risk، requires_confirmation) |
| `POST` | `…/api/tools/{name}/invoke` | اجرای مستقیم ابزار — فقط با `SERVER_ALLOW_DIRECT_TOOLS=true` |

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
     "http://127.0.0.1:8765/api/tools?category=filesystem&q=list" | jq '.tools[].name'
```

اجرای مستقیم (قدرت کامل؛ برای ادمین‌های مورد اعتماد):

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"arguments":{"path":".","limit":20},"session_id":"abc123"}' \
  http://127.0.0.1:8765/api/tools/list_directory/invoke
```

پاسخ دقیقاً همان `ToolResult` هسته است (HTTP 200، حتی وقتی ابزار شکست خورده):

```json
{
  "success": true,
  "data": { "entries": ["…"] },
  "error": null,
  "error_code": null,
  "metadata": { "path": "/home/me/project" },
  "tool": "list_directory",
  "duration_ms": 12,
  "truncated": false,
  "requires_confirmation": false
}
```

مسیر مستقیم هم از لایه‌ی guard رد می‌شود؛ اگر ابزار تأیید بخواهد و کلاینت متصلی
نباشد `{"success":false,"error_code":"confirmation_unavailable"}` و در صورت رد
`{"success":false,"error_code":"declined"}` برمی‌گردد.

### اجرا

| متد | مسیر | بدنه | خروجی |
|---|---|---|---|
| `POST` | `/api/run` | `{"prompt":"…","session_id":"…","profile":"generalist","tools":[…],"system_note":"…","timeout":900,"new_session":false}` | نتیجه‌ی کامل اجرا |
| `POST` | `/api/run/stop` | `{"session_id":"…"}` (هدر سشن هم کافی است) | `{"cancelled":true,"session_id":"…"}` |

```json
{
  "ok": true,
  "text": "سه فایل پیدا شد…",
  "error": null,
  "iterations": 2,
  "duration_ms": 4123,
  "tool_calls": [ { "tool": "list_directory", "succeeded": true, "duration_ms": 12 } ],
  "usage": { "prompt_tokens": 812, "completion_tokens": 96, "total_tokens": 908 },
  "model": "gpt-6-astra",
  "session_id": "abc123",
  "events": [ { "type": "tool.requested", "at": 1757… } ]
}
```

`ok:false` با `422` برمی‌گردد (خطای مدل/ابزار در `error` است و redact شده).
`timeout` را برای کارهای بلند می‌توان بالا برد (سقف ۳۶۰۰ ثانیه)؛ با مهلت، پاسخ
`{"ok":false,"error":"run timed out after …s"}` است و اجرا لغو می‌شود.
`503 missing_api_key` اگر کلیدی تنظیم نباشد.

### سشن‌ها

| متد | مسیر | توضیح |
|---|---|---|
| `GET` | `/api/sessions` | `{"count":n,"sessions":[{…describe()…}]}` |
| `POST` | `/api/sessions` | ساخت سشن؛ بدنه‌ی اختیاری: `{"id":"…","profile":"read_only","system_note":"…"}` |
| `GET` | `/api/sessions/{id}` | توضیح سشن (پروفایل، ابزارهای فعال، محدودیت‌ها) |
| `PATCH` | `/api/sessions/{id}` | تغییر بی‌مخاطره‌ی تنظیمات (فهرست پایین) |
| `DELETE` | `/api/sessions/{id}` | بستن سشن (`{"closed":id}`؛ تأییدهای باز → deny) |
| `POST` | `/api/sessions/{id}/reset` | `{"reset":id,"session":{…}}` — تاریخچه می‌رود، ابزارها می‌مانند |
| `GET` | `/api/sessions/{id}/history?limit=50` | `{"session_id":…,"messages":[…]}` (سقف ۲۰۰) |
| `GET` | `/api/sessions/{id}/events?limit=40&since=0` | `{"session_id":…,"events":[…]}` — همان قالب flat رویدادهای WS (پولینگ برای کلاینت‌های بدون WS) |
| `GET` | `/api/sessions/{id}/approvals` | تأییدهای باز |
| `POST` | `/api/sessions/{id}/approvals` | پاسخ به یک تأیید |

بدنه‌ی `PATCH` همان فیلدهای تخت `SessionUpdate` است (داخل `settings` نیست). فیلدهای مجاز:

```
profile · tools · system_note · model_name · temperature · max_tool_iterations ·
max_output_tokens · enable_confirmation · parallel_tool_calls · max_output_chars ·
dangerous_command_policy
```

هر چیز دیگر → `422` (`cannot change 'x' from the app`)؛ پروفایل نامعتبر → `422
unknown profile`. عمداً `openai_api_key`, مسیرها و فلگ‌های ایمنی هسته‌ای این‌جا
نیستند.

تأییدها:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/sessions/abc123/approvals
# { "pending": [
#   { "kind":"approval_request", "request_id":"req-7", "tool":"terminal_run",
#     "action":"shell", "summary":"rm -rf build/", "risk":"high", "details":{…}, "created_at":1757…
# ] }

curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"request_id":"req-7","approved":true,"reason":"ok"}' \
  http://127.0.0.1:8765/api/sessions/abc123/approvals
# {"resolved":"req-7","approved":true}
```

* `request_id` کهنه → `409 stale_approval`.
* خواندن `GET …/approvals` یک «پنجره‌ی رأفت» (`poll_grace`) باز می‌کند: کلاینتی که
  با polling کار می‌کند و WS ندارد، مهلت می‌گیرد تا پاسخ بدهد؛ در غیر این صورت
  تأیید بی‌کلاینت سریع deny می‌شود.
* هیچ کلاینتی متصل نباشد، تأیید پس از `SERVER_APPROVAL_TIMEOUT` (پیش‌فرض ۱۸۰s)
  خودکار **deny** می‌شود — هیچ‌وقت باز نمی‌ماند.

### حافظه و گزارش‌ها

| متد | مسیر | ورودی | خروجی |
|---|---|---|---|
| `GET` | `/api/reports?days=7` | `days` عدد ۰..۳۶۵۰ (۰ = کل تاریخچه) | `{"report": {...}, "text": "…"}` |
| `GET` | `/api/memory?q=&kind=&limit=` | `limit` ۱..۵۰ (پیش‌فرض ۲۰) | `{"enabled":true,"query":"…","stats":{…},"kinds":[…],"records":[…]}` |
| `POST` | `/api/memory` | `{"content":"…","kind":"preference","tags":["a"],"pin":false}` | ۲۰۱ `{"saved":{…},"stats":{…}}` |
| `DELETE` | `/api/memory/{id}` | `?force=1` برای رکورد pinned | `{"deleted":"id","stats":{…}}` |

* `report.report` همان dict :func:`src.core.reports.build_report` است:
  `generated_at` `window_days` `runs{total,ok,failed,tool_calls,tokens,duration_ms,duration_human}`
  `tools[{tool,calls,failures,denied,blocked}]` `safety{blocked,denied,tool_failures,last_denied,last_blocked}`
  `recent[…]` `memory{…}` `plans[…]` `notes[…]` `next_actions[…]` `warnings[…]` `files{activity,memory}`.
  `report.text` همان گزارش، رندرشده برای نمایش در یک `<pre>` (بدون نیاز به فرانت‌اند خاص).
* `records` در `GET /api/memory` همان `MemoryRecord.as_dict()` است
  (`id` `kind` `content` `tags` `source` `confidence` `pinned` `created_at` `updated_at` `hits`).
* خواندن، رکورد را «دیده‌شده» می‌کند (`hits+1`) چون رتبه‌بندی جست‌وجو به آن وابسته است.
* متن یادداشت در `POST` پیش از ذخیره redact می‌شود؛ اگر `sk-…` بفرستید، چیزی که
  ذخیره و برگردانده می‌شود ماسک‌شده است.
* حافظه خاموش (`MEMORY_ENABLED=false`) → `409 memory_disabled`. `kind` نامعتبر → `note`؛
  `content` خالی یا بیش از ۴۰۰۰ کاراکتر → `400 invalid_request`.
* این مسیرها هیچ ابزاری اجرا نمی‌کنند؛ فقط خواندن/نوشتن همان فایل‌های محلی
  (`~/.universal-agent-hub/memory/memory.jsonl` و `activity.jsonl`).

### پروفایل‌های کلید API

| متد | مسیر | بدنه |
|---|---|---|
| `GET` | `/api/keys` | فقط فرم ماسک‌شده |
| `POST` | `/api/keys` | `{"name":"phone","api_key":"sk-…","model":"gpt-6-astra","base_url":null,"activate":true}` |
| `DELETE` | `/api/keys/{name}` | حذف |
| `POST` | `/api/keys/{name}/activate` | انتخاب پروفایل فعال |
| `POST` | `/api/keys/import-env` | کپی کلید/مدل از `.env` پروژه (۴۰۹ اگر چیزی نبود) |

`api_key` را می‌توان در `POST` خالی گذاشت تا کلید ذخیره‌شده حفظ شود و فقط
`model`/`base_url` به‌روز شوند. **کلید خام هرگز از HTTP بیرون نمی‌رود** و فقط در
حافظه‌ی همان پروسه با سشن فعال ادغام می‌شود. فایل keystore امضای HMAC دارد تا
دست‌کاری بیرونی مشخص شود.

### UI و فایل‌های استاتیک

`GET /` → `src/server/web/index.html` (اگر نباشد: JSON `{"ui":"not installed…"}`)؛
`/styles.css`, `/app.js`, `/manifest.webmanifest`, `/sw.js`, `/icon.svg`,
`/offline.html`, `/icons/*` ثبت می‌شوند و هر مسیر ناآشنای (SPA fallback) همان
`index.html` را می‌گیرد. همه با `Cache-Control: no-cache`.
`SERVER_STATIC_DIR=off` (یا `--no-ui`) یعنی فقط API.

## WebSocket — `GET /ws`

ارتباط JSON متنی؛ هر پیام یک `kind` دارد. با توکن: `?token=…` (با `session=…` هم
می‌توان سشن را چسباند).

### inbound

| `kind` | بدنه | پاسخ |
|---|---|---|
| `run` / `ask` / `prompt` | `{"kind":"run","payload":{"prompt":"…","profile":"read_only","timeout":600}}` | `accepted` → `event`* → `result` |
| `approve` | `{"kind":"approve","payload":{"request_id":"req-7","approved":true,"reason":""}}` | `ack {"resolved":"req-7","ok":true}` (و `result` آزاد می‌شود) |
| `cancel` | `{}` | `ack` (اجرای جاری لغو می‌شود) |
| `reset` | `{}` | `ack` |
| `update` | `{"kind":"update","payload":{"temperature":0.2,"profile":"read_only"}}` | `ack {"changed":[…],"session":{…}}` یا `error` با `invalid_update` |
| `subscribe` / `unsubscribe` | `{}` | `ack` (اشتراک رویدادهای دیگر سشن‌ها) |
| `ping` | `{}` | `pong` |

### outbound

| `kind` | شکل | توضیح |
|---|---|---|
| `hello` | `{"kind":"hello","payload":{…}}` | بلافاصله بعد از اتصال: `session`, `ready`, `pending_approvals`, `recent_events` |
| `accepted` | `{"kind":"accepted","payload":{"prompt":"…","session_id":"…"}}` | اجرا پذیرفته شد (قبل از اولین `event`) |
| `event` | **flat**: `{"kind":"event","type":"tool.requested","tool":"…","at":…}` | همان `agent.events` (order: `agent.started, llm.requested, llm.responded, tool.requested, tool.completed, …, agent.completed`) |
| `approval_request` | **flat**: `{"kind":"approval_request","request_id":"req-7","tool":"…","action":"…","summary":"…","risk":"high","created_at":…}` | نیاز به تأیید |
| `result` | `{"kind":"result","payload":{…}}` | فقط به کسی که `run` فرستاد (بدنه‌ی `/api/run`) |
| `ack` | `{"kind":"ack","payload":{"ok":true,…}}` | پاسخ فرمان‌های کنترلی |
| `error` | `{"kind":"error","payload":{"error":"…","error_code":"invalid_request"}}` | خطای پروتکل/اجرا |
| `pong` | `{"kind":"pong","payload":{"t":…}}` | keepalive |

`event` و `approval_request` عمداً flat هستند تا با `GET /api/sessions/{id}/events`
و `/approvals` یک‌شکل بمانند.

### نمونه (python / `websockets`)

```python
import asyncio
import json

import websockets

URL = "ws://127.0.0.1:8765/ws?token=SECRET"


async def main() -> None:
    """یک اجرای کامل، با تماشای رویدادها و پاسخ به درخواست تأیید."""
    async with websockets.connect(URL) as ws:
        hello = json.loads(await ws.recv())                     # kind=hello
        print("session:", hello["payload"]["session"]["id"], "ready:", hello["payload"]["ready"])

        await ws.send(json.dumps({"kind": "run", "payload": {"prompt": "list files in ."}}))
        while True:
            message = json.loads(await ws.recv())
            kind = message["kind"]
            if kind == "event":
                print("·", message.get("type"), message.get("tool", ""))
            elif kind == "approval_request":
                await ws.send(json.dumps({
                    "kind": "approve",
                    "payload": {"request_id": message["request_id"], "approved": True},
                }))
            elif kind == "result":
                print(message["payload"]["text"])
                break
            elif kind == "error":
                raise SystemExit(message["payload"])


asyncio.run(main())
```

`run` همان `RunRequest` داخل `payload` است (فیلد `prompt`، نه `text`) و `approve`
هم `ApprovalDecision` داخل `payload` می‌خواهد.

### نمونه (curl + `websocat`)

```bash
websocat -t "ws://127.0.0.1:8765/ws?token=$TOKEN" <<< '{"kind":"run","payload":{"prompt":"whoami"}}'
```

## اتصال هم‌زمان و fan-out

* هر اتصال WS یک مشترک رویداد سشن است؛ `event` و `approval_request` برای همه‌ی
  مشترک‌های همان سشن پخش می‌شود (تب‌های مختلف روی یک کامپیوتر صحنه‌ی یکسان می‌بینند).
* صف هر مشترک `maxsize=500` است؛ اگر گوشی sleep کند و صف پر شود، آن مشترک
  حذف می‌شود (سرور هرگز بلاک نمی‌شود) و برنامه با backoff دوباره وصل می‌شود.
* قطع شدن WS = deny برای تأییدهای بازِ آن لحظه (امنیت پیش‌فرض).

## حد و مرزها

| مورد | مقدار | کلید |
|---|---|---|
| بدنه‌ی درخواست | ۱ MiB | `SERVER_MAX_BODY_BYTES` |
| نرخ | ۱۲۰/دقیقه | `SERVER_RATE_LIMIT_PER_MINUTE` |
| عمر تأیید | ۹۰۰s | `SERVER_APPROVAL_TIMEOUT` |
| عمر سشن | ۳۶۰۰s | `SERVER_SESSION_TTL` |
| طول کلید API | ≤ ۵۱۲ | `MAX_KEY_LENGTH` در keystore |

## نسخه‌بندی و سازگاری

پروتکل در `src/server/protocol.py` (pydantic) قفل شده و آزمون‌هایش در
`tests/test_server/` است (۱۸۳ آزمون). تغییر breaking در `kind`ها یا فیلدهای
`sessions` انجام نمی‌شود؛ افزوده‌ها backwards-compatible می‌مانند و `/api/status`
با `version` + `capabilities` به کلاینت اجازه می‌دهد قابلیت را تشخیص بدهد.

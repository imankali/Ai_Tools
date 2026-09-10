# مرجع API

امضاهای عمومی، به‌ترتیب لایه‌ها. این سند *مرجع* است برای خواندن کد؛ مثال‌های اجرا در
[`../README.md`](../README.md)، [`../examples`](../examples) و
[`autonomy.md`](autonomy.md) هستند. همه‌ی امضاها از خود کد استخراج شده‌اند؛ اگر چیزی
اینجا با `src/` فرق داشت، کد درست است و این سند باید اصلاح شود.

---

## `src.config`

```python
class Config(BaseSettings):                 # پیکربندی کامل، از env/.env با pydantic-settings
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False,
                                      extra="ignore", populate_by_name=True)

def get_config() -> Config                  # singleton با lru_cache
def reset_config() -> None                  # پاک کردن کش (تست‌ها)
```

`Config` پرکاربردترین‌ها:

| فیلد (alias) | پیش‌فرض | معنا |
|---|---|---|
| `openai_api_key` (`OPENAI_API_KEY`) | – | کلید مدل |
| `openai_base_url` (`OPENAI_BASE_URL`) | None | endpoint سازگار با OpenAI |
| `model_name` / `model_fallbacks` (`MODEL_NAME` / `MODEL_FALLBACKS`) | `gpt-6-astra` / فهرست | ترتیب تلاش |
| `temperature`, `max_tool_iterations`, `max_output_chars` | `0.2`, `10`, `12000` | حلقه و سقف خروجی |
| `enable_safety_guard` (`ENABLE_SAFETY_GUARD`) | `True` | کلید روشن نگهبان (خاموش = فقط قواعد بحرانی می‌مانند) |
| `enable_confirmation` (`ENABLE_CONFIRMATION`) | `True` | پرسیدن پیش از عمل حساس |
| `dangerous_command_policy` (`DANGEROUS_COMMAND_POLICY`) | `confirm` | `confirm` · `deny` |
| `allow_shell` (`ALLOW_SHELL`) | `True` | اجازه‌ی `shell=True` در `terminal_run` |
| `allowed_directories` / `unrestricted_filesystem` | `[]` (ریشه‌ی پروژه) / `False` | دامنه‌ی ابزار فایل |
| `max_command_timeout` / `max_file_bytes` | `30` / `512 KiB` | سقف I/O |
| `memory_enabled`, `memory_dir`, `memory_auto_capture`, `memory_context_chars`, `memory_max_records` | `True`, None, `True`, `4000`, `2000` | حافظه‌ی بلندمدت |
| `reports_enabled`, `activity_log_file`, `report_max_runs` | `True`, None, `2000` | فایل گزارش فعالیت |
| `events_log_file` (`EVENTS_LOG_FILE`) | None | JSONL رویدادها (`EventBus(persist_path=…)`) |
| `server_enabled/host/port/token/rate_limit_per_minute/approval_timeout/session_ttl/max_body_bytes/allow_direct_tools/static_dir/keystore` | `False`, `127.0.0.1`, `8765`, `""`, `30`, `180`, `3600`, `2 MiB`, `False`, None, None | سرور اپ |

propertyها: `project_root` · `server_url` · `server_is_open` · `keystore_path` ·
`memory_path` · `activity_path` · `event_log_path` · `resolved_log_file` ·
`safe_model_candidates` · `known_model` · `model_was_downgraded` · `to_safe_dict()`.

## `src.agent`

```python
class UniversalAgent:
    def __init__(
        self,
        config: Config | None = None,
        *,
        tools: Iterable[str] | None = None,          # allow-list نام ابزارها
        event_bus: EventBus | None = None,           # None → EventBus(persist_path=config.event_log_path)
        safety: SafetyGuard | None = None,
        system_prompt: str | None = None,
        profile: AgentProfile | None = None,
        confirm_callback: ConfirmationCallback | None = None,
        client: Any = None,                          # هر کلاینت سازگار با OpenAI (تست: FakeOpenAIClient)
        registry: type[ToolRegistry] = ToolRegistry,
        keep_history: bool = True,
        max_history_messages: int = 40,
        memory: AgentMemory | None = None,           # None → از config
        use_memory: bool | None = None,              # False → حافظه صرف‌نظر حتی اگر config فعال باشد
        recorder: ActivityRecorder | None = None,    # None → از config (REPORTS_ENABLED)
    ) -> None

    async def ask(self, user_input: str, *, system_note: str | None = None,
                  raise_on_error: bool = False) -> AgentRunResult
    async def run(self, user_input: str, *, system_note: str | None = None) -> str
    def set_confirmation_handler(self, handler: ConfirmationCallback | None) -> None
    def memory_block(self, query: str = "") -> str
    def memory_summary(self) -> dict[str, Any]
    def activity_report(self, *, days: float = 7.0) -> dict[str, Any]
    def safety_summary(self) -> dict[str, Any]
    def validate_tool_arguments(self, tool_name: str, arguments: dict[str, Any]) -> str | None
    def export_state(self) -> str
    def load_state(self, payload: dict[str, Any]) -> None
    def add_tool(self, tool: Any) -> None
    async def close(self) -> None
    def describe_tools(self) -> list[dict[str, Any]]

    # properties
    tools: list[Any]; tool_names: list[str]; schemas: list[dict[str, Any]]
    model_info: dict[str, Any]; state: AgentState; event_bus: EventBus
```

`ask()` ترتیب کار: ساخت پیام‌ها (system + profile extra + **بلوک حافظه** + تاریخچه) →
حلقه‌ی `max_tool_iterations` با `_chat_with_retry` (fallback مدل و retry خطاهای
موقوت) → اجرای ابزارها (`_run_tool_calls`: موازی اگر `parallel_tool_calls=true`، و
همیشه به ترتیب درخواست مدل در گفت‌وگو) → `AgentRunResult` → ثبت خودکار در حافظه
(`_capture_run`) و در فایل فعالیت (`_record_activity`).

## `src.core.base_tool`

```python
@dataclass
class ToolContext:
    config: Any = None
    safety: SafetyGuard | None = None
    bus: Any = None
    confirm: ConfirmationCallback | None = None
    session: dict[str, Any] = field(default_factory=dict)
    max_output_chars: int            # property ← config (پیش‌فرض 12000)
    timeout: int                     # property ← config.max_command_timeout (پیش‌فرض 30)

@dataclass
class ConfirmationRequest:
    tool: str; arguments: dict[str, Any]; risk: RiskLevel; reasons: list[str]; preview: str
    call_id: str = ""; request_id: str = ""

class BaseTool(ABC):
    # ClassVarهایی که subclass تنظیم می‌کند
    name: str; description: str; category: ToolCategory; risk_level: RiskLevel
    required_parameters: tuple[str, ...]; optional_parameters: tuple[str, ...]
    sensitive_parameters: tuple[str, ...]; requires_confirmation: bool
    max_result_bytes: int | None

    def __init__(self, config: Any = None) -> None
    @abstractmethod async def execute(self, **kwargs) -> ToolResult
    @abstractmethod def get_schema(self) -> dict[str, Any]
    async def run(self, arguments: dict | None = None, *, context: ToolContext | None = None,
                  call_id: str = "", skip_confirmation: bool = False) -> ToolResult
    def validate_input(self, **kwargs) -> None            # ValidationError روی ورودی نامعتبر
    async def safety_check(self, kwargs: dict, context: ToolContext | None = None) -> SafetyDecision
    async def before_execute(self, kwargs: dict, context: ToolContext | None) -> dict
    def to_info(self) -> ToolInfo
    @staticmethod function_schema(name, description, properties, required=None, *, strict=False) -> dict
    @staticmethod describe_limits(max_bytes: int) -> str
```

ترتیب داخل `run`: `validate_input` → `before_execute` → `safety_check` → (در صورت
نیاز) تأیید → `execute` → `_normalize_result` (redaction + سقف حجم + `duration_ms`).
هیچ استثنایی به بیرون نمی‌رود؛ همه‌چیز به `ToolResult(success=False, error_code=…)`
تبدیل می‌شود.

## `src.models.tool_models` / `src.models.agent_models`

```python
class ToolResult(BaseModel):
    success: bool; data: Any; error: str | None; error_code: str | None
    metadata: dict; duration_ms: int; truncated: bool; requires_confirmation: bool
    @classmethod ok(cls, data, *, tool="", metadata=None) -> ToolResult
    @classmethod fail(cls, message, *, tool="", error_code="tool_error", metadata=None) -> ToolResult

class ToolCall(BaseModel):   id: str; name: str; arguments: dict
class ToolInfo(BaseModel):   name, description, category, requires_confirmation, risk_level, parameters
class ToolCategory(str, Enum): SYSTEM | FILESYSTEM | NETWORK | BROWSER | DEVELOPER | CUSTOM
class RiskLevel(str, Enum):    SAFE | LOW | MEDIUM | HIGH | CRITICAL

class AgentRunResult(BaseModel):
    text: str; ok: bool; error: str | None; iterations: int; tool_calls: list[ToolCallRecord]
    usage: UsageStats; duration_ms: int; model: str; events: list[dict]; session_id: str
    @property tool_names: list[str]

class ToolCallRecord(BaseModel):
    tool: str; arguments: dict; result: ToolResult | None; started_at: float
    duration_ms: int; approved: bool | None; error: str | None
    @property succeeded: bool

class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]; content: str
    name: str | None; tool_call_id: str | None; tool_calls: list[dict]
    def to_api_dict(self) -> dict[str, Any]

class AgentState(BaseModel):   # messages, tool_history, active_profile, created_at, …
class UsageStats(BaseModel):   # prompt_tokens, completion_tokens, total_tokens, …
```

## `src.core.tool_registry`

```python
class ToolRegistry:
    @classmethod register(cls, tool_class: type[BaseTool], *, replace: bool = False) -> type[BaseTool]
    @classmethod unregister(cls, name: str) -> bool
    @classmethod clear(cls) -> None
    @classmethod discover(cls, package: str = "src.tools", *, force: bool = False) -> list[str]
    @classmethod load_plugins(cls, directories: Sequence[str | Path] | None = None, *, force: bool = False) -> list[str]
    @classmethod register_module(cls, module: Any, *, prefix: str = "") -> list[str]
    @classmethod classes(cls) -> dict[str, type[BaseTool]]
    @classmethod names(cls) -> list[str]
    @classmethod iter_names(cls) -> Iterator[str]
    @classmethod get(cls, name: str, *, config: Any = None) -> BaseTool | None
    @classmethod instances(cls, *, config: Any = None, only: Iterable[str] | None = None) -> list[BaseTool]
    @classmethod get_all(cls) -> list[BaseTool]
    @classmethod schemas(cls, *, only: Iterable[str] | None = None, config: Any = None) -> list[dict[str, Any]]
    @classmethod get_schemas(cls, only=None) -> list[dict[str, Any]]
    @classmethod info(cls, *, config: Any = None) -> list[ToolInfo]
    @classmethod build_context(cls, *, config=None, safety=None, bus=None, confirm=None, session=None) -> ToolContext
    @classmethod set_default_config(cls, config: Config | None) -> None

def register_tool(tool_class: type[BaseTool]) -> type[BaseTool]     # دکوریتور
def discover_tools(*, force: bool = False) -> list[str]             # میان‌بر ماژولی
BUILTIN_TOOL_MODULES: tuple[str, ...]                               # fallback کشف در حالت فریز
TOOL_DIRS_ENV = "AGENT_HUB_TOOL_DIRS"
```

## `src.core.event_bus`

```python
@dataclass(slots=True)
class Event:
    kind: str; payload: dict[str, Any]; timestamp: float; event_id: str; source: str = "core"
    @property elapsed_seconds: float
    def to_dict(self) -> dict[str, Any]
    def to_json(self) -> str

class EventBus:
    def __init__(self, *, history_size: int = 200, persist_path: Path | str | None = None,
                 handler_timeout: float = 10.0) -> None
    def subscribe(self, pattern: str, handler: EventHandler) -> str          # fnmatch: "tool.*"
    def unsubscribe(self, subscription_id: str) -> bool
    def listeners(self, pattern: str = "*") -> list[str]
    async def emit(self, kind: str, payload: dict | None = None, *, source: str = "core") -> list[Any]
    def publish_nowait(self, kind: str, payload: dict | None = None, *, source: str = "core") -> asyncio.Future
    def history(self, pattern: str = "*", limit: int = 50) -> list[Event]
    def recent(self, limit: int = 10, *, pattern: str = "*") -> list[Event]
    async def wait_for(self, kind: str, *, timeout: float | None = None,
                       predicate: Callable[[Event], bool] | None = None) -> Event
    def clear(self) -> None

def install_default_observers(bus: EventBus, *, log: bool = True,
                              extra: Iterable[tuple[str, EventHandler]] | None = None) -> list[str]
```

نام‌های رویداد (`EVENTS`): `agent.started` `agent.completed` `agent.failed` ·
`llm.requested` `llm.responded` `llm.failed` · `tool.requested` `tool.approved`
`tool.denied` `tool.completed` `tool.failed` · `safety.blocked`
`safety.confirmation_requested` · `registry.changed` · `error`.

## `src.core.agent_factory`

```python
class AgentFactory:
    @classmethod ensure_loaded(cls) -> None
    @classmethod register(cls, profile: AgentProfile) -> AgentProfile
    @classmethod register_builder(cls, name: str, builder: Callable[..., UniversalAgent]) -> None
    @classmethod profiles(cls) -> dict[str, AgentProfile]
    @classmethod get_profile(cls, name: str) -> AgentProfile | None
    @classmethod load_profiles_from_dir(cls, directory: str | Path, *, prefix: str = "profile") -> list[str]
    @classmethod create(cls, profile: str = "generalist", *, config=None, tools=None, event_bus=None,
                        agent_class=UniversalAgent, log: bool = True, **overrides: Any) -> UniversalAgent
    @classmethod tools_for(cls, name: str) -> list[str]
```

پروفایل‌های داخلی: `generalist` (۲۳ ابزار) · `read_only` (۸ ابزار نوشتن/اجرا غیرفعال) ·
`developer` · `ops`. در `overrides` هر فیلد `AgentProfile` قابل بازنویسی است (`model`,
`temperature`, `system_prompt_extra`, `enabled_tools`, `disabled_tools`, `max_iterations`, …).

## `src.core.memory`

```python
MEMORY_KINDS = ("fact", "preference", "procedure", "decision", "plan", "note")

class MemoryRecord(BaseModel):
    id: str; kind: str; content: str; tags: list[str]; source: str
    confidence: float; pinned: bool; created_at: float; updated_at: float; hits: int
    @property sha: str
    def score(self, query_tokens: set[str], *, now: float | None = None) -> float
    def as_dict(self) -> dict[str, Any]

class AgentMemory:
    def __init__(self, path: str | Path | None, *, max_records: int = 2000, enabled: bool = True) -> None
    @classmethod for_config(cls, config: Any) -> AgentMemory | None    # کش مشترک per-path
    @classmethod clear_cache(cls) -> None
    def add(self, content, *, kind="note", tags=None, source="agent", confidence=0.7, pin=False) -> MemoryRecord | None
    def remember(self, content, **kwargs) -> MemoryRecord | None       # میان‌بر فارسی‌پسند
    def update(self, record_id, **changes) -> MemoryRecord | None
    def forget(self, record_id) -> bool
    def find(self, record_id) -> MemoryRecord | None                    # id کامل یا پیشوند یکتا
    def all(self, *, kinds=None) -> list[MemoryRecord]
    def recent(self, limit=10, *, kinds=None) -> list[MemoryRecord]
    def search(self, query, *, kinds=None, limit=8) -> list[MemoryRecord]
    def touch(self, records) -> None
    def plans(self, limit=10) -> list[MemoryRecord]
    def context_block(self, *, max_chars: int = 4000, query: str = "") -> str
    def capture_run(self, result, *, prompt="", profile="", min_iterations=1) -> MemoryRecord | None
    def export_json(self, path) -> int
    def import_json(self, path) -> int          # ValueError اگر فایل قابل‌خواندن نباشد
    def stats(self) -> dict[str, Any]
    def flush(self) -> None
    def describe(self) -> str

def memory_for_config(config: Any) -> AgentMemory | None
def reset_memory_cache() -> None
```

## `src.core.reports`

```python
class ActivityRecorder:
    def __init__(self, path: str | Path | None, *, max_records: int = 2000, enabled: bool = True) -> None
    @classmethod for_config(cls, config: Any) -> ActivityRecorder | None   # None اگر REPORTS_ENABLED=false
    @classmethod attach(cls, bus: EventBus, config: Any) -> list[str]      # اشتراک روی رویدادهای اجرا/ایمنی
    @classmethod clear_cache(cls) -> None
    def subscribe(self, bus: EventBus) -> list[str]
    def record(self, entry: dict[str, Any]) -> dict[str, Any]              # + ts/iso، یک خط JSONL
    def note(self, text: str, *, kind: str = "note") -> dict[str, Any]
    def entries(self, *, limit: int | None = None, since: float | None = None,
                kinds: Iterable[str] | None = None) -> list[dict[str, Any]]
    def stats(self) -> dict[str, Any]
    def clear(self) -> int

def build_report(config: Any, *, days: float = 7.0, memory: Any = None,
                 recorder: ActivityRecorder | None = None, limit: int = 200) -> dict[str, Any]
def render_report_text(report: dict[str, Any], *, title: str = "Activity report") -> str
def redact_line(value: Any, limit: int = 200) -> str
def reset_recorder_cache() -> None
```

رکوردهای `activity.jsonl`: `kind` ∈ `run` `run_failed` `blocked` `denied` `tool_failed` `note`؛
سطر `run` کلیدهای `ok` `iterations` `tools` `calls[{tool,ok,code,duration_ms}]`
`duration_ms` `tokens` `model` `profile` `prompt` `error` را دارد. خروجی
`build_report` در [`autonomy.md`](autonomy.md) بخش ۶ فهرست شده است.

## `src.utils.safety`

```python
class SafetyDecision:
    allowed: bool = True; requires_confirmation: bool = False
    risk: RiskLevel = RiskLevel.LOW; reasons: list[str] = []; metadata: dict = {}
    @property blocked: bool
    @property reason_text: str
    def as_dict(self) -> dict[str, Any]

class SafetyGuard:
    def __init__(self, *, policy="confirm", allowed_directories=None, unrestricted_filesystem=False,
                 blocked_commands=None, confirm_commands=None, read_only_paths=None, blocked_paths=None,
                 project_root=None, allow_shell=True, enabled=True) -> None
    @classmethod from_config(cls, config: Any) -> SafetyGuard
    def parse_command(self, command: str) -> CommandToken
    def assess_command(self, command: str) -> SafetyDecision
    def assess_path(self, path: str | Path, *, action: str = "read") -> SafetyDecision
    def assess_network(self, url: str) -> SafetyDecision
    def summary(self) -> dict[str, Any]
    CRITICAL_PATTERNS: tuple[str, ...]
    CONFIRM_PATTERNS: tuple[str, ...]
```

`policy="deny"` بحرانی‌ها را می‌بندد؛ `enabled=False` (یعنی `ENABLE_SAFETY_GUARD=false`)
تأییدخواهدنها را آزاد می‌کند اما قواعد CRITICAL و بلوک‌های HIGH را نگه می‌دارد.

## `src.utils.validators` / `src.utils.helpers`

```python
# validators — در صورت نامعتبربودن ValidationError (subclass از ValueError) می‌دهند، None نه
is_valid_url(url) · is_valid_url_strict(url) · safe_host_for_url(url) · is_private_host(host)
is_valid_path(path, *, root=None) · is_within(child, parent)
bounded_int(value, *, name, minimum, maximum, default) · is_valid_cron(expr)
is_safe_selector(selector) · validate_shell_tokens(tokens) · coerce_bool(value)
is_valid_date(text) · clean_text(text, *, max_length=…) · mask_value(value)

# helpers
truncate_text(text, limit, *, suffix="…") -> tuple[str, bool]
shorten_middle(text, limit) · format_size(num) · format_duration(seconds) · now_iso()
safe_json_loads(payload) -> dict      # همیشه dict؛ خطا → {"_parse_error": True, "_raw": …}
strip_html(html) · Timer()            # context manager با .ms
ensure_dir(path) · chunked(seq, size) · redact_secrets(text) -> str
env_flag(name, default=False) · first_existing_dir(candidates)
run_in_thread(fn, /, *args, **kwargs)  # dispatch به thread pool
```

## `src.server`

```python
def create_app(config: Config | None = None) -> web.Application
def build_app(config: Config | None = None) -> web.Application     # نام سازگار
def run_server(config: Config | None = None, *, print_banner: bool = True) -> None
def serve_forever(config: Config | None = None) -> None
```

بقیه (`auth.TokenAuthorizer/RateLimiter/is_loopback_address`، `keystore.KeyStore`،
`sessions.SessionRegistry/AgentSession/ApprovalBroker/serialize_confirmation` و مدل‌های
`protocol`) و همه‌ی مسیرهای HTTP/WS در [`server_api.md`](server_api.md) شرح داده شده‌اند؛
معماری لایه‌ها در [`architecture.md`](architecture.md).

## `src.cli`

```python
def build_parser() -> argparse.ArgumentParser
def main(argv: list[str] | None = None) -> int        # 0 موفق · 1 خطا · 130 Ctrl+C

class CLI:
    def __init__(self, config=None, *, console=None, agent=None, profile="generalist",
                 tools=None, quiet=False) -> None
    async def run(self, *, prompt_once: str | None = None, output_json: bool = False) -> int

def doctor_checks(config: Config | None = None) -> list[tuple[str, str, str]]
def run_doctor(config: Config) -> int
def run_report(config: Config, *, days: float = 7.0, as_json: bool = False) -> int
def run_remember(config: Config, text: str, *, kind: str = "note", pin: bool = False) -> int
def serve_in_window(settings: Any, url: str, console: Any, *, startup_wait: float = 10.0) -> int
def _local_addresses() -> list[str]
def _desktop_page_url(base: str, token: str) -> str
def _wait_for_server(base: str, timeout: float = 10.0, abort: Callable[[], bool] | None = None) -> bool
```

دستورهای داخل چت: `/help` · `/tools` · `/schema` · `/config` · `/safety` · `/model` ·
`/profile` · `/tools only` · `/cd` · `/history` · `/events` · `/transcript` · `/save` ·
`/load` · `/memory` · `/report` · `/doctor` · `/confirm` · `/clear` · `/quit`.

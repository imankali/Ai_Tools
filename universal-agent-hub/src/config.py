"""مدیریت پیکربندی پروژه (Configuration management).

این ماژول تنها نقطه‌ی ورود تنظیمات به کل سیستم است:

* خواندن متغیرهای محیطی از ``.env`` (از طریق ``python-dotenv`` / pydantic-settings)
* اعتبارسنجی و نرمال‌سازی مقادیر با Pydantic v2
* فراهم کردن مقدار پیش‌فرض امن برای همه‌ی کلیدها

نکته‌ی مهم درباره‌ی نام مدل
--------------------------
پروژه به‌صورت پیش‌فرض روی ``gpt-6-astra`` تنظیم شده است، اما این نام مدل
باید توسط *ارائه‌دهنده‌ای* که در ``OPENAI_BASE_URL`` مشخص می‌کنید پشتیبانی
شود. اگر از API رسمی OpenAI استفاده می‌کنید و نام مدل شناخته نشده باشد،
ایجنت به‌صورت خودکار به اولین مدل موجود در ``model_fallbacks`` سوییچ
می‌کند (برای جزئیات ``docs/security.md`` و ``README.md`` را ببینید).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # pydantic-settings >= 2.3: با NoDecode منبع JSON خودکار دور زده می‌شود
    from pydantic_settings import NoDecode
except ImportError:  # pragma: no cover - نسخه‌های قدیمی‌تر
    NoDecode = None  # type: ignore[assignment,misc]

__all__ = ["Config", "get_config", "reset_config"]

#: نام مدل پیش‌فرض درخواست پروژه
DEFAULT_MODEL = "gpt-6-astra"

#: نوع فیلد: لیست رشته‌ای که از env/.env به‌صورت «a, b» یا JSON خوانده می‌شود.
#: NOTE: mypy یک type alias را فقط وقتی می‌شناسد که انتساب ساده باشد، پس شرط
#: runtime را از دید type-checker پنهان می‌کنیم (رفتار اجرا دقیقاً همان است).
if TYPE_CHECKING:
    from typing import TypeAlias

    CommaList: TypeAlias = list[str]
else:
    CommaList = Annotated[list[str], NoDecode] if NoDecode is not None else list[str]

#: فهرست کوتاه مدل‌های عمومی که برای «آگاهی از وجود مدل» استفاده می‌شود.
#: این فهرست فقط جنبه‌ی راهنمایی دارد و دسترسی را محدود نمی‌کند.
KNOWN_PUBLIC_MODELS: frozenset[str] = frozenset(
    {
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "o3-mini",
        "o4-mini",
        "chatgpt-4o-latest",
    }
)


#: مقادیری که در ``SERVER_STATIC_DIR`` یعنی «UI سرو نشود»
UI_OFF_VALUES: frozenset[str] = frozenset({"off", "none", "no", "false", "disable", "disabled"})


class Config(BaseSettings):
    """پیکربندی مرکزی ایجنت.

    تمام فیلدها از متغیرهای محیطی با همان نام (بدون حساسیت به بزرگ/کوچکی
    حروف) خوانده می‌شوند و در نبود آن‌ها مقدار پیش‌فرض به‌کار می‌رود.

    Attributes:
        openai_api_key: کلید API. در حالت تست می‌توان خالی باشد.
        model_name: نام مدل زبانی مورد نظر.
        model_fallbacks: فهرست مدل‌های جایگزین در صورت خطای «model not found».
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ------------------------------------------------------------------
    # OpenAI / LLM
    # ------------------------------------------------------------------
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    model_name: str = Field(default=DEFAULT_MODEL, alias="MODEL_NAME")
    model_fallbacks: CommaList = Field(default_factory=list, alias="MODEL_FALLBACKS")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, alias="TEMPERATURE")
    max_output_tokens: int = Field(default=2048, ge=64, alias="MAX_OUTPUT_TOKENS")
    max_retries: int = Field(default=3, ge=0, le=10, alias="MAX_RETRIES")
    request_timeout: float = Field(default=120.0, gt=0, alias="REQUEST_TIMEOUT")
    max_tool_iterations: int = Field(default=8, ge=1, le=40, alias="MAX_TOOL_ITERATIONS")

    # ------------------------------------------------------------------
    # ایمنی (Safety)
    # ------------------------------------------------------------------
    #: کلید اصلی نگهبان. ``false`` همه‌ی بررسی‌ها را شل می‌کند؛ فقط قواعد بحرانی
    #: (rm -rf /، dd of=/dev/sda، نوشتن روی /etc) حتی در این حالت هم باقی می‌مانند.
    enable_safety_guard: bool = Field(default=True, alias="ENABLE_SAFETY_GUARD")
    enable_confirmation: bool = Field(default=True, alias="ENABLE_CONFIRMATION")
    #: حالت "deny": دستورهای پرخطر کلاً اجرا نمی‌شوند؛ حالت "confirm" فقط تأیید می‌خواهد.
    dangerous_command_policy: str = Field(default="confirm", alias="DANGEROUS_COMMAND_POLICY")
    max_command_timeout: int = Field(default=30, ge=1, le=3600, alias="MAX_COMMAND_TIMEOUT")
    allow_shell: bool = Field(default=True, alias="ALLOW_SHELL")
    #: فهرست دایرکتوری‌های مجاز برای ابزار فایل. خالی = فقط دایرکتوری جاری پروژه.
    allowed_directories: CommaList = Field(default_factory=list, alias="ALLOWED_DIRECTORIES")
    #: اگر True باشد، دسترسی به کل فایل‌سیستم (بدون محدودیت) اجازه داده می‌شود.
    unrestricted_filesystem: bool = Field(default=False, alias="UNRESTRICTED_FILESYSTEM")
    max_file_bytes: int = Field(default=512 * 1024, ge=1024, alias="MAX_FILE_BYTES")
    max_output_chars: int = Field(default=12000, ge=500, alias="MAX_OUTPUT_CHARS")

    # ------------------------------------------------------------------
    # لاگ و رویدادها (Logging / observability)
    # ------------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str | None = Field(default="agent.log", alias="LOG_FILE")
    log_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=3, ge=0, le=50, alias="LOG_BACKUP_COUNT")
    rich_console: bool = Field(default=True, alias="RICH_CONSOLE")
    events_log_file: str | None = Field(default=None, alias="EVENTS_LOG_FILE")
    #: اجرای موازی فراخوانی‌های ابزار مستقل در یک نوبت
    parallel_tool_calls: bool = Field(default=True, alias="PARALLEL_TOOL_CALLS")

    # ------------------------------------------------------------------
    # وب (Search / browser)
    # ------------------------------------------------------------------
    search_backend: str = Field(default="auto", alias="SEARCH_BACKEND")
    search_num_results: int = Field(default=6, ge=1, le=20, alias="SEARCH_NUM_RESULTS")
    search_timeout: float = Field(default=20.0, gt=0, alias="SEARCH_TIMEOUT")
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 UniversalAgentHub/1.0"
        ),
        alias="USER_AGENT",
    )
    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_timeout_ms: int = Field(default=30000, ge=1000, alias="BROWSER_TIMEOUT_MS")
    #: حداکثر مدت زمان زنده ماندن یک session مرورگر (ثانیه)
    browser_session_ttl: int = Field(default=600, ge=10, alias="BROWSER_SESSION_TTL")

    # ------------------------------------------------------------------
    # سرور اپ‌ها (REST + WebSocket) — برای اندروید/iOS/دسکتاپ
    # ------------------------------------------------------------------
    #: روشن‌کردن سرور با ``agent-hub --serve`` (یا ``python -m src.server``)
    server_enabled: bool = Field(default=False, alias="SERVER_ENABLED")
    #: پیش‌فرض فقط حلقه‌ی داخلی؛ برای استفاده از گوشی روی LAN را 0.0.0.0 بگذارید
    server_host: str = Field(default="127.0.0.1", alias="SERVER_HOST")
    server_port: int = Field(default=8765, ge=1, le=65535, alias="SERVER_PORT")
    #: توکن دسترسی اپ‌ها. خالی = فقط از loopback اجازه داده می‌شود (حالت توسعه)
    server_token: str = Field(default="", alias="SERVER_TOKEN")
    #: سقف درخواست run در دقیقه برای هر IP (محافظت در برابر حلقه‌ی بی‌نهایت)
    server_rate_limit_per_minute: int = Field(default=30, ge=1, le=10000, alias="SERVER_RATE_LIMIT")
    #: ثانیه‌های انتظار برای پاسخ «تأیید عملیات» روی اپ؛ پایان = رد خودکار
    server_approval_timeout: int = Field(default=180, ge=10, le=3600, alias="SERVER_APPROVAL_TIMEOUT")
    #: عمر بی‌کار یک session اپ (ثانیه)
    server_session_ttl: int = Field(default=3600, ge=60, le=86400, alias="SERVER_SESSION_TTL")
    server_max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, alias="SERVER_MAX_BODY_BYTES")
    #: محل نگهداری پروفایل‌های کلید API (پیش‌فرض: ``~/.universal-agent-hub/keys.json``)
    server_keystore: Path | None = Field(default=None, alias="SERVER_KEYSTORE")
    #: اجازه‌ی فراخوانی مستقیم ابزار از اپ (بدون مدل) — پنل مدیریت؛ روی شبکه‌ی اشتراکی خاموش بگذارید
    server_allow_direct_tools: bool = Field(default=False, alias="SERVER_ALLOW_DIRECT_TOOLS")

    # ------------------------------------------------------------------
    # حافظه‌ی بلندمدت ایجنت (src/core/memory.py)
    # ------------------------------------------------------------------
    #: ثبت/خواندن یادداشت‌های پایدار بین اجراها
    memory_enabled: bool = Field(default=True, alias="MEMORY_ENABLED")
    #: فایل JSONL حافظه (پیش‌فرض: ``~/.universal-agent-hub/memory/memory.jsonl``)
    memory_dir: Path | None = Field(default=None, alias="MEMORY_DIR")
    #: پس از هر اجرا یک یادداشت خودکار (کار/ابزارها/نتیجه) ثبت شود
    memory_auto_capture: bool = Field(default=True, alias="MEMORY_AUTO_CAPTURE")
    #: سقف کاراکتر بلوک حافظه‌ی تزریق‌شده در system prompt
    memory_context_chars: int = Field(default=4000, ge=0, le=20000, alias="MEMORY_CONTEXT_CHARS")
    #: سقف رکورد (قدیمی‌ترین‌های غیر-pinned حذف می‌شوند)
    memory_max_records: int = Field(default=2000, ge=20, le=200000, alias="MEMORY_MAX_RECORDS")

    # ------------------------------------------------------------------
    # گزارش فعالیت (src/core/reports.py) — پنل «چه کردم و چه می‌کنم»
    # ------------------------------------------------------------------
    #: خلاصه‌ی هر اجرا (ابزارها، توکن، نتیجه) در فایل فعالیت ثبت شود
    reports_enabled: bool = Field(default=True, alias="REPORTS_ENABLED")
    #: فایل JSONL فعالیت (پیش‌فرض: ``~/.universal-agent-hub/activity.jsonl``)
    activity_log_file: Path | None = Field(default=None, alias="ACTIVITY_LOG_FILE")
    #: سقف رکورد اجرا در فایل فعالیت
    report_max_runs: int = Field(default=2000, ge=50, le=100000, alias="REPORT_MAX_RUNS")
    #: ریشه‌ی فایل‌های وب UI (پیش‌فرض: ``src/server/web`` داخل بسته)
    server_static_dir: Path | None = Field(default=None, alias="SERVER_STATIC_DIR")

    # ------------------------------------------------------------------
    # متادیتای runtime (توسط خود کد پر می‌شود)
    # ------------------------------------------------------------------
    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    model_was_downgraded: bool = False
    active_model: str = DEFAULT_MODEL

    # ------------------------------------------------------------------
    # اعتبارسنجی و نرمال‌سازی
    # ------------------------------------------------------------------
    @field_validator("model_name", mode="before")
    @classmethod
    def _normalize_model_name(cls, value: Any) -> str:
        """مدل خالی → پیش‌فرض پروژه (تا درخواست بدون model هرگز ساخته نشود)."""
        text = str(value or "").strip()
        return text or DEFAULT_MODEL

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> str:
        """بزرگ‌نویس کردن سطح لاگ و جایگزینی مقدار نامعتبر."""
        text = str(value or "INFO").strip().upper()
        return text if text in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"} else "INFO"

    @field_validator("dangerous_command_policy", mode="before")
    @classmethod
    def _normalize_policy(cls, value: Any) -> str:
        """مجاز: ``confirm`` یا ``deny``."""
        text = str(value or "confirm").strip().lower()
        return text if text in {"confirm", "deny"} else "confirm"

    @field_validator("search_backend", mode="before")
    @classmethod
    def _normalize_backend(cls, value: Any) -> str:
        """پشتیبان‌های مجاز جست‌وجو: auto/ddgs/html/tavily/offline."""
        text = str(value or "auto").strip().lower()
        allowed = {"auto", "ddgs", "html", "tavily", "offline"}
        return text if text in allowed else "auto"

    @field_validator("model_fallbacks", "allowed_directories", mode="before")
    @classmethod
    def _split_list(cls, value: Any) -> Any:
        """پذیرش لیست در ``.env`` هم به شکل ``a, b`` و هم به شکل JSON."""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith(("[", "{")):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip().strip(chr(34) + chr(39)) for item in text.split(",") if item.strip()]
        return value

    @field_validator("allowed_directories", mode="after")
    @classmethod
    def _expand_paths(cls, value: list[str]) -> list[str]:
        """باز کردن ``~`` و تبدیل به مسیر مطلق."""
        return [str(Path(p).expanduser().resolve(strict=False)) for p in value if p]

    @field_validator("max_output_tokens", mode="after")
    @classmethod
    def _cap_tokens(cls, value: int) -> int:
        """جلوگیری از مصرف بی‌رویه‌ی توکن."""
        return min(value, 32000)

    @field_validator("server_host", mode="before")
    @classmethod
    def _normalize_server_host(cls, value: Any) -> str:
        """پذیرش «۰.۰.۰.۰»، ``http://0.0.0.0:8765`` یا خالی؛ خالی = loopback."""
        text = str(value or "").strip().lower()
        for prefix in ("http://", "https://", "ws://", "wss://"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
        text = text.strip("/").strip()
        if text.endswith(":*"):
            text = text[:-2]
        if text in {"", "*", "all", "any", "0"}:
            return "0.0.0.0"
        return text

    @field_validator("server_static_dir", "server_keystore", mode="before")
    @classmethod
    def _empty_path_to_none(cls, value: Any) -> Any:
        """رشته‌ی خالی در .env یعنی «استفاده از پیش‌فرض»."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ------------------------------------------------------------------
    # منبع‌های پیکربندی
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # رفتار
    # ------------------------------------------------------------------
    def model_post_init(self, __context: Any) -> None:
        """تنظیم مدل فعال و لاگ هشدار در صورت مدل ناشناخته."""
        self.active_model = self.model_name or DEFAULT_MODEL
        if self.model_name not in KNOWN_PUBLIC_MODELS and self.model_name != DEFAULT_MODEL:
            # مدل‌های سفارشی/محلی کاملاً مجازند؛ فقط اطلاع‌رسانی می‌شود.
            self.model_was_downgraded = False

    @property
    def is_api_key_set(self) -> bool:
        """آیا کلید API به‌شکل واقعی ارائه شده است؟"""
        return bool(self.openai_api_key) and not self.openai_api_key.startswith("test")

    @property
    def known_model(self) -> bool:
        """آیا مدل درخواست‌شده در فهرست مدل‌های عمومی شناخته‌شده است؟"""
        return self.model_name in KNOWN_PUBLIC_MODELS

    @property
    def safe_model_candidates(self) -> list[str]:
        """ترتیب تلاش روی مدل‌ها: مدل اصلی و سپس جایگزین‌های یکتا."""
        seen: list[str] = []
        for candidate in [self.model_name, *self.model_fallbacks]:
            name = (candidate or "").strip()
            if name and name not in seen:
                seen.append(name)
        return seen

    # ------------------------------------------------------------------
    # سرور اپ‌ها (REST + WebSocket)
    # ------------------------------------------------------------------
    @property
    def is_loopback_server_host(self) -> bool:
        """آیا سرور فقط روی همین ماشین گوش می‌دهد؟"""
        return self.server_host in {"127.0.0.1", "localhost", "::1", "loopback"}

    @property
    def server_is_open(self) -> bool:
        """``True`` یعنی سرور روی شبکه باز است؛ توکن در این حالت الزامی است."""
        return not self.is_loopback_server_host

    @property
    def server_url(self) -> str:
        """آدرس پایه‌ی سرور برای نمایش در CLI و واردکردن در اپ موبایل."""
        host = "127.0.0.1" if self.is_loopback_server_host else self.server_host
        return f"http://{host}:{self.server_port}"

    @property
    def keystore_path(self) -> Path:
        """فایل پروفایل‌های کلید API (بیرون از مخزن، پیش‌فرض در home)."""
        if self.server_keystore is not None:
            path = Path(self.server_keystore).expanduser()
            return path if path.is_absolute() else self.project_root / path
        return Path.home() / ".universal-agent-hub" / "keys.json"

    @property
    def memory_path(self) -> Path | None:
        """فایل JSONL حافظه (``None`` اگر حافظه خاموش باشد)."""
        if not self.memory_enabled:
            return None
        if self.memory_dir is not None:
            folder = Path(self.memory_dir).expanduser()
            folder = folder if folder.is_absolute() else self.project_root / folder
        else:
            folder = Path.home() / ".universal-agent-hub" / "memory"
        return folder / "memory.jsonl" if folder.suffix != ".jsonl" else folder

    @property
    def activity_path(self) -> Path:
        """مسیر مطلق فایل JSONL گزارش فعالیت (پیش‌فرض کنار فایل حافظه)."""
        if self.activity_log_file is not None:
            path = Path(self.activity_log_file).expanduser()
            return path if path.is_absolute() else self.project_root / path
        memory_file = self.memory_path
        if memory_file is not None and self.memory_dir is None:
            return memory_file.parent.parent / "activity.jsonl"
        return Path.home() / ".universal-agent-hub" / "activity.jsonl"

    @property
    def web_root(self) -> Path | None:
        """پوشه‌ی فایل‌های UI؛ ``None`` یعنی سرور فقط API سرو می‌کند.

        ترتیب جست‌وجو (نخستین پوشه‌ای که ``index.html`` داشته باشد برنده است):

        1. ``SERVER_STATIC_DIR`` (اگر کاربر تنظیم کرده باشد)
        2. ``src/server/web`` — همان مسیری که در wheel نصب‌شده هم وجود دارد
        3. ``<project_root>/web`` — چیدمان مخزن و ایمیج داکر
        """
        candidates: list[Path] = []
        if self.server_static_dir is not None:
            configured = Path(self.server_static_dir).expanduser()
            if str(configured).strip().lower() in UI_OFF_VALUES:
                return None  # کاربر UI را صریحاً خاموش کرده است
            candidates.append(configured if configured.is_absolute() else self.project_root / configured)
        here = Path(__file__).resolve().parent
        candidates.append(here / "server" / "web")
        candidates.append(self.project_root / "web")
        for candidate in candidates:
            if (candidate / "index.html").is_file():
                return candidate
        return None

    @property
    def event_log_path(self) -> Path | None:
        """مسیر مطلق فایل ذخیره‌ی رویدادها (JSONL) یا ``None``."""
        if not self.events_log_file:
            return None
        path = Path(self.events_log_file).expanduser()
        return path if path.is_absolute() else self.project_root / path

    @property
    def resolved_log_file(self) -> Path | None:
        """مسیر مطلق فایل لاگ."""
        if not self.log_file:
            return None
        path = Path(self.log_file).expanduser()
        return path if path.is_absolute() else self.project_root / path

    def to_safe_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل نمایش از تنظیمات (بدون افشای کلید API)."""
        data = self.model_dump(mode="json")
        if data.get("openai_api_key"):
            key = str(data["openai_api_key"])
            data["openai_api_key"] = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "***"
        if data.get("tavily_api_key"):
            data["tavily_api_key"] = "***"
        return data


@lru_cache(maxsize=1)
def get_config() -> Config:
    """بازگرداندن نمونه‌ی singleton از تنظیمات (با کش)."""
    return Config()


def reset_config() -> None:
    """پاک کردن کش تنظیمات (مفید برای تست‌ها)."""
    get_config.cache_clear()

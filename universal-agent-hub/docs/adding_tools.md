# ساخت ابزار تازه

هر توانایی ایجنت یک ابزار است: یک کلاس با `name`، `description`، اسکیمای JSON و یک
`execute` async. این سند مسیر کامل است — از کپی الگو تا تست سبز و بسته‌ی فریزشده.

---

## ۱) قرارداد در یک نگاه

| مورد | واقعیت کد |
|---|---|
| کلاس پایه | `src.core.base_tool.BaseTool` |
| ثبت | دکوریتور `@register_tool` از `src.core.tool_registry` (یا `ToolRegistry.register(cls)`) |
| اجرا | `await tool.run(arguments, *, context=None, call_id="", skip_confirmation=False)` → `ToolResult` |
| منطق شما | `async def execute(self, **kwargs) -> ToolResult` (override) |
| اسکیمای اجباری | `def get_schema(self) -> dict` — **abstract است**؛ بدون آن `@register_tool` با `TypeError: tool X does not implement: get_schema` می‌ایستد |
| پارامترها | ClassVarهای `required_parameters` / `optional_parameters` / `sensitive_parameters` (نه `parameters = {...}`) |
| دسته‌ها | `ToolCategory.SYSTEM · FILESYSTEM · NETWORK · BROWSER · DEVELOPER · CUSTOM` |
| ریسک | `RiskLevel.SAFE · LOW · MEDIUM · HIGH · CRITICAL` |
| config | به *نمونه‌ی ابزار* تزریق می‌شود: `ToolRegistry.get(name, config=cfg)` / `registry.instances(config=cfg)`؛ `ToolContext` به hook‌های ایمنی می‌رسد |
| خطاها | `ToolResult.fail(msg, tool=…, error_code=…)` — کدهای رایج: `invalid_input` `blocked` `declined` `confirmation_unavailable` `tool_error` `non_zero_exit` |

`BaseTool.run` همه‌ی نگرانی‌های عرضی را خودش انجام می‌دهد: اعتبارسنجی، `before_execute`،
`safety_check`، تأیید انسانی، رویدادهای `tool.requested/approved/denied/failed/completed`
روی `EventBus`، redaction، سقف حجم خروجی (`max_output_chars`) و `duration_ms`.

## ۲) بیست دقیقه، مرحله‌به‌مرحله

```bash
cp src/tools/template.py src/tools/weather.py
```

و همین چند نقطه را پر کنید (الگو با همین بخش‌ها commentگذاری شده است):

```python
"""ابزار هوا (نمونه‌ی آموزش)."""

from __future__ import annotations

from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.core.tool_registry import register_tool
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.validators import ValidationError, clean_text

__all__ = ["WeatherTool"]


@register_tool
class WeatherTool(BaseTool):
    """هوای یک شهر (نمونه؛ داده را از cache خودتان بگیرید)."""

    name: ClassVar[str] = "weather_now"
    description: ClassVar[str] = "Report the current temperature and wind for a city."
    category: ClassVar[ToolCategory] = ToolCategory.NETWORK
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    required_parameters: ClassVar[tuple[str, ...]] = ("city",)
    optional_parameters: ClassVar[tuple[str, ...]] = ("units",)
    sensitive_parameters: ClassVar[tuple[str, ...]] = ()

    async def execute(  # type: ignore[override]
        self,
        city: str = "",
        units: str = "c",
        *,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """برگشتن داده‌ی هوا در قالب ToolResult."""
        place = clean_text(city, max_length=80)
        if not place:
            return ToolResult.fail("city must not be empty", tool=self.name, error_code="invalid_input")
        if units not in {"c", "f"}:
            raise ValidationError("units must be 'c' or 'f'")
        payload = {"city": place, "units": units, "temperature": 21.5, "wind_kph": 7}
        return ToolResult.ok(payload, tool=self.name)

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling برای مدل."""
        return self.function_schema(
            name=self.name,
            description=self.description,
            properties={
                "city": {"type": "string", "description": "City name, e.g. 'Tehran'."},
                "units": {"type": "string", "enum": ["c", "f"], "description": "Temperature unit."},
            },
            required=("city",),
        )
```

نکات قراردادی که تست‌های موجود هم روی آن‌ها حساس‌اند:

* `function_schema(name, description, properties, required=None, *, strict=False)` یک
  **staticmethod** است و خروجی‌اش این پوسته است:
  `{"type":"function","function":{"name","description","parameters":{…,"additionalProperties":false}}}`.
* استثنای `ValidationError` مجاز است: `run` آن را به `ToolResult.fail(…, "invalid_input")`
  تبدیل می‌کند و رویداد `tool.failed` می‌فرستد.
* `ToolResult` را خودتان بسازید و برگردانید (`ok`/`fail`)؛ اگر مقدار خام برگردانید،
  `run` آن را داخل `ToolResult(success=True, data=…)` می‌پیچد.
* هیچ‌وقت `print` نکنید؛ خروجی ابزار به مدل می‌رود. برای دیباگ از
  `self.logger` (یا `get_logger("tools.weather")`) استفاده کنید.

## ۳) چه چیزی لازم است «تأیید» بخواهد؟

نگهبان بر اساس `risk_level` + قواعد خودش تصمیم می‌گیرد. شما سه اهرم دارید:

* `risk_level = RiskLevel.HIGH` + `requires_confirmation = True` → همیشه قبل از اجرا
  می‌پرسد (CLI/UI/اپ). برای حذف، نوشتن بیرون از `ALLOWED_DIRECTORIES`، یا هر عمل
  مالی/انتشاری همین است.
* override `async def safety_check(self, kwargs, context)` → وقتی تصمیم به ورودی وابسته
  است (مثل `terminal_run` که دستور را ارزیابی می‌کند). نمونه‌ی واقعی: `src/tools/filesystem.py`
  که `context`/`self.safety_guard` را با `assess_path(path, action="write")` درگیر می‌کند.
* `sensitive_parameters = ("token", "password")` → آن مقادیر در لاگ، رویدادها و پاسخ‌ها
  ماسک می‌شوند (`_redact`).

اگر کاری را نمی‌توان عقب برگرداند، **در ابزار** رد نشدنی طراحی کنید؛ `--yes` و
`ENABLE_CONFIRMATION=false` باید فقط تأییدِ «کارهای معمولی» را حذف کنند، نه اینکه
اجازه‌ی برداشت پول بدهند.

## ۴) تست

`tests/test_tools/test_weather.py`:

```python
"""تست ابزار هوا."""

from __future__ import annotations

import pytest

from src.config import Config
from src.core.tool_registry import ToolRegistry, discover_tools


@pytest.fixture(autouse=True)
def _registered(config: Config) -> None:
    ToolRegistry.set_default_config(config)
    discover_tools(force=True)


async def test_reports_temperature(config: Config) -> None:
    tool = ToolRegistry.get("weather_now", config=config)
    assert tool is not None
    result = await tool.run({"city": "Tehran"})
    assert result.success
    assert result.data["city"] == "Tehran"


async def test_empty_city_is_invalid(config: Config) -> None:
    tool = ToolRegistry.get("weather_now", config=config)
    result = await tool.run({"city": "   "})
    assert not result.success and result.error_code == "invalid_input"


async def test_units_are_validated(config: Config) -> None:
    tool = ToolRegistry.get("weather_now", config=config)
    result = await tool.run({"city": "Rasht", "units": "kelvin"})
    assert not result.success and "units" in (result.error or "")


def test_schema_shape(config: Config) -> None:
    schema = ToolRegistry.get("weather_now", config=config).get_schema()
    assert schema["function"]["parameters"]["required"] == ["city"]
    assert schema["function"]["parameters"]["additionalProperties"] is False
```

قراردادهای تست این پروژه: بدون شبکه، بدون مدل، بدون home کاربر (fixtures
`config`/`workspace` همه‌چیز را به `tmp_path` می‌برند). اگر ابزارتان HTTP می‌زند،
`aiohttp`/`requests` را monkeypatch کنید و یک تست هم برای «خطای شبکه باید به
`ToolResult.fail` تبدیل شود» بگذارید — همان کاری که `tests/test_tools/test_web_search.py`
با `DDGS` و `tests/test_tools/test_browser.py` با `aiohttp` می‌کنند.

```bash
make test && make lint            # coverage gate: ۸۰٪
```

## ۵) ثبت، کشف، و پلاگین بیرون از مخزن

سه راه برای اینکه ابزار در دسترس ایجنت باشد:

1. **داخل بسته:** فایل در `src/tools/` با نامی در `BUILTIN_TOOL_MODULES`
   (`terminal, filesystem, browser, web_search, system_info, memory`). این لیست
   مسیر fallback برای حالت فریزشده/zipapp است که `pkgutil` در آن کاری نمی‌کند؛
   اگر ابزار تازه‌ای *باید* در باینلی باشد، همین‌جا اضافه‌اش کنید
   (`src/core/tool_registry.py`).
2. **کشف خودکار:** `discover_tools()` همه‌ی ماژول‌های `src/tools/*.py` را import
   می‌کند؛ دکوریتور ثبت می‌کند. `template.py` عمداً بدون `@register_tool` است تا
   الگو بماند و ثبت نشود.
3. **پلاگین از پوشه:** `AGENT_HUB_TOOL_DIRS=/home/me/agent-tools` (یا
   `ToolRegistry.load_plugins([...])`). هر `.py` آن پوشه import می‌شود و
   subclassهای `BaseTool` با دکوریتور ثبت می‌گردند. اگر همان فایل دوباره import
   شود، از `sys.modules` بازاستفاده می‌شود و **دوباره ثبت** می‌گردد — پس یک باینلی
   فریزشده بدون rebuild ابزار تازه می‌گیرد. `packaging/hooks/rthook_agent_hub.py`
   پوشه‌ی `tools/` کنار executable را به همین متغیر اضافه می‌کند.

```bash
agent-hub --tools terminal_run,read_file,weather_now --prompt "…"   # allow-list
agent-hub /tools                                                    # داخل چت: فهرست
agent-hub --schema weather_now                                      # اسکیمای ابزار
```

## ۶) چک‌لیست پیش از commit

- [ ] `name` یکتا، small_snake_case، فعل/فعل‌گروه مشخص (`read_*`, `browser_*`, `memory_*`).
- [ ] `description` برای *مدل* نوشته شده (چه‌وقت استفاده کند، چه‌وقت نکند) — نه برای آدم.
- [ ] `get_schema` با `function_schema`؛ `required` فقط چیزهای واقعاً لازم.
- [ ] ورودی‌های ناممکن → `ToolResult.fail` یا `ValidationError` (نه استثنای خام، نه traceback).
- [ ] `risk_level` درست؛ اگر نوشتن/حذف/پول است: `requires_confirmation = True`.
- [ ] داده‌ی حساس در `sensitive_parameters`؛ خروجی خام‌ی بزرگ `truncate` می‌شود (خودکار).
- [ ] `timeout` از `context.timeout` (یا `self.config`) — هیچ I/O بدون سقف نه.
- [ ] تست واحد + تست «خطای بیرونی graceful» + `make lint`/`make test` سبز.
- [ ] اگر ابزار باید در باینلی باشد: `BUILTIN_TOOL_MODULES` و `packaging/agent-hub.spec` (hiddenimports).
- [ ] یک خط در `docs/server_api.md` (فهرست ابزارها) و اگر رفتار ایمنی تازه دارد: `docs/security.md`.

## ۷) ابزار ساخت‌شده توسط خود ایجنت

ایجنت می‌تواند با `write_file` + `terminal_run` فایل ابزار را در یک پوشه‌ی پلاگین
بسازد و با `AGENT_HUB_TOOL_DIRS` فعالش کند؛ قانون پروژه: **فقط در پوشه‌ی پلاگین،
با تست، و با تأیید انسان**. نوشتن در `src/tools/` بیرون از `ALLOWED_DIRECTORIES`
است و تأیید می‌خواهد — همین را تغییر ندهید. جزئیات و دلایل:
[`autonomy.md`](autonomy.md) بخش ۷.

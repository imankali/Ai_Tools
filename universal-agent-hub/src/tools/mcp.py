"""پل MCP ⇒ ابزار درجه‌یک (G03).

ابزارهای یک سرور راه دور MCP را به زیرکلاس‌های :class:`BaseTool` تبدیل می‌کند تا
مدل زبانی آن‌ها را دقیقاً مثل ابزارهای داخلی ببیند و صدا بزند. نام هر ابزار
``mcp__{server}__{tool}`` است (همان :attr:`MCPTool.qualified_name`).

چرا زیرکلاسِ ساخته‌شده در زمان اجرا، نه یک ابزار عمومی؟
    چون ``ToolRegistry`` کلاس‌محور است (``register(tool_class)`` و
    ``get(name) ⇒ tool_class(config)``). یک ابزار عمومی با نام ثابت یعنی فقط *یک*
    ابزار MCP قابل ثبت است؛ با ساخت یک زیرکلاس به‌ازای هر ابزار راه دور، کل
    registry موجود (schema، فیلتر، رویداد ``registry.changed``، تأیید) بدون هیچ
    تغییری کار می‌کند.

مرزهای امنیتی — این‌ها انتخابی نیستند:
    * توضیح و اسکیمای ابزار راه دور **ورودی نامطمئن** است: پیش از رسیدن به مدل
      از :func:`sanitize_untrusted` رد می‌شود، و اگر اسکنر تزریق در سطح
      ``high`` یا بالاتر چیزی ببیند، ابزار **ثبت نمی‌شود**.
    * آرگومان‌های ``tools/call`` در :meth:`MCPClient.call_tool` از
      ``SafetyGuard`` عبور می‌کنند؛ یعنی ``command``/``path``/``url`` دقیقاً مثل
      یک فراخوانی محلی ارزیابی می‌شود.
    * اگر سرور وصل نباشد، ابزار به‌جای استثنا، ``ToolResult`` خطا برمی‌گرداند.

مثال::

    clients = await hub.connect_mcp_and_clients()
    registered = register_mcp_tools(clients)
    # ['mcp__filesystem__read_file', ...]
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from src.core.base_tool import BaseTool, ToolContext
from src.core.mcp_client import MCPClient, MCPError, MCPTool
from src.core.tool_registry import ToolRegistry
from src.models.tool_models import RiskLevel, ToolCategory, ToolResult
from src.utils.injection import PromptInjectionScanner, sanitize_untrusted
from src.utils.logger import get_logger
from src.utils.validators import ValidationError

logger = get_logger("tools.mcp")

#: نام ابزار باید برای Function Calling امن باشد (حروف/رقم/``_``/``-``).
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")

#: سقف کاراکتر توضیح ابزار راه دور (ورودی نامطمئن نباید context را ببلعد).
_MAX_DESCRIPTION_CHARS = 1200


class MCPToolAdapter(BaseTool):
    """ابزار راه دور MCP، پوشانده در قالب یک :class:`BaseTool`.

    Attributes:
        mcp_server: نام سرور (برای پیدا کردن کلاینت از ``context.mcp``).
        mcp_tool_name: نام ابزار *بدون* پیشوند (همان چیزی که به سرور می‌رود).
    """

    #: عمداً *خالی*: ``ToolRegistry._register_tools_from`` کلاس‌هایی با ``name``
    #: خالی را رد می‌کند، و این کلاس یک پایه است نه یک ابزار. اگر نام داشت،
    #: ``discover_tools()`` آن را به‌عنوان ابزار شبحیِ ``mcp_tool`` به مدل نشان
    #: می‌داد. زیرکلاس‌های پویا همیشه ``name`` واقعی می‌گیرند.
    name: ClassVar[str] = ""
    description: ClassVar[str] = "A remote tool provided by an MCP server."
    #: ``CUSTOM`` چون ToolCategory دسته‌ی ``mcp`` ندارد و افزودن یک عضو جدید به
    #: آن enum، فیلترهای موجود را بی‌صدا تغییر می‌دهد.
    category: ClassVar[ToolCategory] = ToolCategory.CUSTOM
    #: ابزار راه دور یعنی کد شخص ثالث. ریسک ذاتی «متوسط» است: گارد آرگومان‌ها را
    # بررسی می‌کند، ولی ما بدنه‌ی آن را ندیده‌ایم.
    risk_level: ClassVar[RiskLevel] = RiskLevel.MEDIUM
    requires_confirmation: ClassVar[bool] = False

    mcp_server: ClassVar[str] = ""
    mcp_tool_name: ClassVar[str] = ""
    #: اسکیمای ورودی اعلام‌شده توسط سرور (JSON Schema)؛ برای get_schema نگه داشته می‌شود.
    mcp_input_schema: ClassVar[dict[str, Any]] = {}

    def _client(self, context: ToolContext | None) -> MCPClient | None:
        """کلاینتِ همان سرور را از context پیدا می‌کند.

        ``context.mcp`` فهرستی از :class:`MCPClient` است (از ``ServiceHub``).
        """
        clients = getattr(context, "mcp", None) if context is not None else None
        if not clients:
            return None
        for candidate in clients:
            if not isinstance(candidate, MCPClient):
                continue
            if candidate.config.name == self.mcp_server:
                return candidate
        return None

    async def execute(self, **kwargs: Any) -> ToolResult:
        """ابزار راه دور را صدا می‌کند.

        Args:
            **kwargs: آرگومان‌ها (همان‌طور که مدل داده) به‌علاوه‌ی ``context``.
        """
        context = kwargs.pop("context", None)
        client = self._client(context)
        if client is None:
            return ToolResult.fail(
                f"MCP server '{self.mcp_server}' is not connected — call this tool only after MCP is up.",
                tool=self.name,
            )
        try:
            payload = await client.call_tool(self.mcp_tool_name, kwargs)
        except MCPError as exc:
            # خطای پروتکل/گارد ⇒ ToolResult خطا، نه استثنا (قرارداد BaseTool).
            return ToolResult.fail(f"MCP call failed: {exc}", tool=self.name)
        except (OSError, TimeoutError, ValueError) as exc:
            return ToolResult.fail(f"MCP transport error: {exc}", tool=self.name)

        text = _content_to_text(payload)
        if payload.get("isError"):
            return ToolResult.fail(text or "remote tool reported an error", tool=self.name)
        return ToolResult.ok(text, tool=self.name)

    def get_schema(self) -> dict[str, Any]:
        """اسکیمای Function Calling، ساخته‌شده از JSON Schema سرور."""
        raw = self.mcp_input_schema or {}
        properties = raw.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = raw.get("required")
        required = [str(item) for item in required] if isinstance(required, list) else []
        return self.function_schema(
            name=self.name,
            description=self.description,
            properties={str(key): value for key, value in properties.items()},
            required=required,
        )

    def validate_input(self, **kwargs: Any) -> bool:
        """اعتبارسنجی بر پایه‌ی اسکیمای *اعلام‌شده‌ی سرور*.

        عمداً ``required_parameters``/``optional_parameters`` کلاس پایه را دور
        می‌زند: آن‌ها ClassVar ثابت‌اند و ابزار راه دور در زمان اجرا شناخته می‌شود.
        """
        kwargs.pop("context", None)
        raw = self.mcp_input_schema or {}
        properties = raw.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = raw.get("required")
        required = {str(item) for item in required} if isinstance(required, list) else set()

        missing = sorted(name for name in required if kwargs.get(name) in (None, ""))
        if missing:
            raise ValidationError(f"missing required parameter(s): {', '.join(missing)}")
        if properties:
            unknown = sorted(set(kwargs) - set(properties))
            if unknown:
                raise ValidationError(f"unknown parameter(s): {', '.join(unknown)}")
        return True


def _content_to_text(payload: dict[str, Any]) -> str:
    """بلوک‌های ``content`` پاسخ MCP را به یک متن تبدیل می‌کند.

    پروتکل MCP چند نوع بلوک دارد (``text``، ``image``، ``resource``). فقط متن
    برای مدل مفید است؛ بقیه به یک نشانگر کوتاه تبدیل می‌شوند تا مدل بداند
    چیزی آنجا بوده بدون این‌که بایت خام وارد context شود.
    """
    blocks = payload.get("content")
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return json.dumps(payload, ensure_ascii=False, default=str)[:4000]

    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = str(block.get("type", "text"))
            if kind == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(f"[{kind} content omitted]")
    return "\n".join(part for part in parts if part).strip()


def _safe_tool_name(server: str, tool: str) -> str:
    """نام یکتا و امن برای registry.

    ``MCPServerConfig._clean_name`` نام سرور را پیش‌تر پاک کرده، ولی نام ابزار
    مستقیماً از سرور راه دور می‌آید و هیچ تضمینی ندارد.
    """
    cleaned = _SAFE_NAME_RE.sub("_", tool)[:64].strip("_") or "tool"
    return f"mcp__{server}__{cleaned}"


def build_tool_class(tool: MCPTool, *, scanner: PromptInjectionScanner | None = None) -> type[MCPToolAdapter] | None:
    """برای یک :class:`MCPTool` یک زیرکلاس :class:`MCPToolAdapter` می‌سازد.

    Args:
        tool: ابزار راه دور.
        scanner: اسکنر تزریق؛ اگر ``None`` باشد یک نمونه‌ی پیش‌فرض ساخته می‌شود.

    Returns:
        کلاس ساخته‌شده، یا ``None`` اگر توضیح ابزار در سطح ``high``+ آلوده باشد.

    Note:
        بازگرداندن ``None`` به‌جای استثنا عمدی است: یک سرور بد می‌تواند ده ابزار
        داشته باشد که یکی‌شان آلوده است؛ بقیه نباید از دست بروند.
    """
    active = scanner or PromptInjectionScanner()
    description = str(tool.description or "").strip()
    report = active.scan(description)
    if report.at_least("high"):
        logger.warning(
            "MCP tool '%s' rejected: description looks like a prompt injection (%s)",
            tool.qualified_name,
            report.summary(),
        )
        return None

    safe_description = sanitize_untrusted(description, max_chars=_MAX_DESCRIPTION_CHARS)
    if not safe_description.strip():
        safe_description = f"Remote MCP tool '{tool.name}'."
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}

    class_name = "".join(part.capitalize() for part in _SAFE_NAME_RE.sub("_", tool.name).split("_")) or "Tool"
    return type(
        f"MCP{class_name}Tool",
        (MCPToolAdapter,),
        {
            "__doc__": f"Adapter for remote MCP tool ``{tool.qualified_name}``.",
            "name": _safe_tool_name(tool.server, tool.name),
            "description": safe_description,
            "mcp_server": tool.server,
            "mcp_tool_name": tool.name,
            "mcp_input_schema": dict(schema),
        },
    )


async def collect_mcp_tools(clients: list[MCPClient]) -> list[MCPTool]:
    """همه‌ی ابزارهای سرورهای *وصل* را جمع می‌کند.

    سرورِ قطع‌شده بی‌صدا رد می‌شود: نبودِ یک سرور اختیاری نباید راه‌اندازی
    ایجنت را بشکند.
    """
    tools: list[MCPTool] = []
    for client in clients:
        if not client.connected:
            continue
        tools.extend(client.tools.values())
    return tools


async def register_mcp_tools(
    clients: list[MCPClient],
    *,
    registry: type[ToolRegistry] = ToolRegistry,
    scanner: PromptInjectionScanner | None = None,
    replace: bool = True,
) -> list[str]:
    """ابزارهای راه دور را به‌صورت ابزار درجه‌یک در registry ثبت می‌کند.

    Args:
        clients: کلاینت‌های MCP (معمولاً ``ServiceHub.mcp_clients``).
        registry: registry هدف (برای تست قابل تزریق).
        scanner: اسکنر تزریق مشترک.
        replace: بازنویسی ابزار هم‌نام (پیش‌فرض True؛ چون reconnect یعنی
            reconnect همان سرور، نه یک سرور دیگر).

    Returns:
        نام ابزارهای ثبت‌شده.
    """
    registered: list[str] = []
    for tool in await collect_mcp_tools(clients):
        tool_class = build_tool_class(tool, scanner=scanner)
        if tool_class is None:
            continue
        try:
            registry.register(tool_class, replace=replace)
        except (TypeError, ValueError) as exc:
            logger.warning("could not register MCP tool '%s': %s", tool.qualified_name, exc)
            continue
        registered.append(tool_class.name)
    if registered:
        logger.info("registered %d MCP tool(s): %s", len(registered), ", ".join(registered))
    return registered


async def unregister_mcp_tools(
    clients: list[MCPClient],
    *,
    registry: type[ToolRegistry] = ToolRegistry,
) -> int:
    """ابزارهای MCP سرورهای داده‌شده را از registry برمی‌دارد.

    Returns:
        تعداد ابزارهای حذف‌شده.
    """
    removed = 0
    for tool in await collect_mcp_tools(clients):
        if registry.unregister(_safe_tool_name(tool.server, tool.name)):
            removed += 1
    return removed

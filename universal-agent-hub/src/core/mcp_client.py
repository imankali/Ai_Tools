"""کلاینت Model Context Protocol (MCP).

چرا این بزرگ‌ترین شکاف اکوسیستمی بود؟
-------------------------------------
هر سه مرجع آن را دارند (OpenClaw ``src/mcp``، Agent-Zero ``mcp_handler`` و
شش اندپوینت ``mcp_server_*``، IronClaw «MCP Protocol») و ما هیچ نداشتیم. MCP
یعنی هزاران ابزار آماده‌ی جامعه بدون نوشتن یک خط ``BaseTool``.

پشتیبانی
--------
* **stdio** — subprocess با JSON خط‌به‌خط (رایج‌ترین حالت MCP).
* **streamable HTTP** — POST با JSON-RPC؛ پاسخ JSON ساده یا ``text/event-stream``.
* متدها: ``initialize``، ``notifications/initialized``، ``tools/list``،
  ``tools/call``، ``prompts/list``، ``resources/list``، ``resources/read``، ``ping``.

ایمنی
-----
* هر ``tools/call`` از :class:`~src.utils.safety.SafetyGuard` عبور می‌کند.
* سرور MCP یک منبع *نامعتبر* است؛ پس توضیح ابزارهایش پیش از ورود به prompt
  اسکن injection می‌شود.
* timeout و سقف اندازه‌ی پاسخ دارد تا یک سرور بدخواه حافظه را نخورد.
* هیچ secret محیطی به subprocess پاس نمی‌شود مگر صریحاً در ``env`` آمده باشد.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.utils.injection import PromptInjectionScanner
from src.utils.logger import get_logger

__all__ = [
    "MCPClient",
    "MCPServerConfig",
    "MCPServerStatus",
    "MCPTool",
    "MCPTransport",
    "load_mcp_config",
]

logger = get_logger("core.mcp")

#: نسخه‌ی پروتکل که در handshake اعلام می‌کنیم.
PROTOCOL_VERSION = "2025-06-18"

CLIENT_INFO = {"name": "universal-agent-hub", "version": "1.1.0"}


class MCPTransport(str, Enum):
    """نوع اتصال."""

    STDIO = "stdio"
    HTTP = "http"


class MCPServerStatus(str, Enum):
    """وضعیت یک سرور."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class MCPServerConfig(BaseModel):
    """پیکربندی یک سرور MCP.

    Attributes:
        name: نام یکتا (بخشی از نام ابزار می‌شود).
        transport: ``stdio`` یا ``http``.
        command: برای stdio، دستور اجرایی.
        args: آرگومان‌ها.
        env: متغیرهای محیطی *اضافه* (کل محیط پاس نمی‌شود).
        cwd: پوشه‌ی کاری subprocess.
        url: برای http.
        headers: هدرهای HTTP (مثلاً Authorization).
        enabled: فعال؟
        timeout: سقف ثانیه‌ی هر فراخوانی.
    """

    name: str
    transport: MCPTransport = MCPTransport.STDIO
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    timeout: float = 30.0

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: Any) -> str:
        """نام باید برای ساختن نام ابزار امن باشد."""
        text = str(value or "").strip().lower()
        cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in text).strip("-")
        if not cleaned:
            raise ValueError("server name must not be empty")
        return cleaned[:48]

    @field_validator("timeout", mode="after")
    @classmethod
    def _bound_timeout(cls, value: float) -> float:
        """سقف زمانی منطقی."""
        return max(1.0, min(float(value), 600.0))

    def validate_transport(self) -> None:
        """سازگاری فیلدها با نوع اتصال.

        Raises:
            ValueError: اگر فیلد لازم خالی باشد.
        """
        if self.transport is MCPTransport.STDIO:
            if not self.command.strip():
                raise ValueError(f"server '{self.name}': stdio transport needs 'command'")
        else:
            if not self.url.strip():
                raise ValueError(f"server '{self.name}': http transport needs 'url'")
            if not self.url.lower().startswith(("http://", "https://")):
                raise ValueError(f"server '{self.name}': url must be http(s)")

    def safe_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل نمایش (مقادیر secret پوشانده می‌شوند)."""
        data = self.model_dump(mode="json")
        for key in list(data.get("env", {})):
            data["env"][key] = "***"
        for key in list(data.get("headers", {})):
            if any(hint in key.lower() for hint in ("auth", "token", "key", "cookie")):
                data["headers"][key] = "***"
        return data


@dataclass
class MCPTool:
    """یک ابزار که یک سرور MCP ارائه می‌دهد."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        """نام یکتای سراسری: ``mcp__{server}__{tool}``."""
        return f"mcp__{self.server}__{self.name}"

    def as_dict(self) -> dict[str, Any]:
        """نسخه‌ی قابل serialize."""
        return {
            "server": self.server,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class MCPError(RuntimeError):
    """خطای سطح پروتکل MCP."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        """Args:
        message: پیام.
        code: کد JSON-RPC.
        data: داده‌ی خطا.
        """
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class _Pending:
    """یک درخواست در انتظار پاسخ."""

    future: asyncio.Future[Any]
    method: str


class MCPClient:
    """کلاینت یک سرور MCP.

    نمونه::

        async with MCPClient(MCPServerConfig(name="fs", command="npx", args=["-y", "@mcp/fs"])) as client:
            tools = await client.list_tools()
            result = await client.call_tool("read_file", {"path": "/tmp/x"})
    """

    #: سقف بایت پاسخ؛ جلوی سروری که گیگابایت می‌فرستد را می‌گیرد.
    MAX_MESSAGE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        scanner: PromptInjectionScanner | None = None,
        safety_guard: Any = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Args:
        config: پیکربندی سرور.
        scanner: اسکنر injection برای توضیح ابزارها.
        safety_guard: یک ``SafetyGuard`` برای بررسی آرگومان‌ها.
        session_factory: سازنده‌ی ``aiohttp.ClientSession`` (برای تست).
        """
        self.config = config
        self.scanner = scanner or PromptInjectionScanner()
        self.safety_guard = safety_guard
        self._session_factory = session_factory
        self.status = MCPServerStatus.DISCONNECTED
        self.error: str = ""
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.tools: dict[str, MCPTool] = {}
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._http_session: Any = None
        self._http_endpoint: str = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    @property
    def connected(self) -> bool:
        """آیا اتصال برقرار است؟"""
        return self.status is MCPServerStatus.CONNECTED

    async def connect(self) -> dict[str, Any]:
        """اتصال + handshake + دریافت فهرست ابزارها.

        Returns:
            ``serverInfo`` سرور.

        Raises:
            MCPError: اگر handshake شکست بخورد.
        """
        self.config.validate_transport()
        self.status = MCPServerStatus.CONNECTING
        self.error = ""
        try:
            if self.config.transport is MCPTransport.STDIO:
                await self._connect_stdio()
            else:
                await self._connect_http()
            info = await self._initialize()
            await self.refresh_tools()
            self.status = MCPServerStatus.CONNECTED
            return info
        except Exception as exc:  # noqa: BLE001 - هر خطایی باید status را error کند
            self.status = MCPServerStatus.ERROR
            self.error = f"{type(exc).__name__}: {exc}"[:500]
            await self.close()
            raise MCPError(self.error) from exc

    async def _connect_stdio(self) -> None:
        """subprocess را بالا می‌آورد.

        نکته‌ی امنی: ``env`` از یک محیط *خالی* شروع می‌شود و فقط ``PATH``/``HOME``
        و مقادیر صریحِ ``config.env`` را می‌گیرد. این یعنی secret های ``.env``
        کاربر به‌صورت پیش‌فرض به سرور MCP نشت نمی‌کنند.
        """
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        base_env.update({str(k): str(v) for k, v in self.config.env.items()})
        cwd = self.config.cwd or None
        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *[str(a) for a in self.config.args],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=base_env,
            cwd=cwd,
        )
        self._reader_task = asyncio.create_task(self._read_stdio(), name=f"mcp-{self.config.name}-reader")

    async def _read_stdio(self) -> None:
        """حلقه‌ی خواندن stdout و پخش پاسخ‌ها."""
        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            try:
                line = await process.stdout.readline()
            except (asyncio.CancelledError, OSError):
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("mcp %s: reader error: %s", self.config.name, exc)
                break
            if not line:
                break
            if len(line) > self.MAX_MESSAGE_BYTES:
                logger.warning("mcp %s: dropping oversized message (%d bytes)", self.config.name, len(line))
                continue
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            self._dispatch(message)
        self._fail_all_pending("server closed the connection")

    def _dispatch(self, message: dict[str, Any]) -> None:
        """پاسخ JSON-RPC را به future مربوطه می‌دهد."""
        raw_id = message.get("id")
        if raw_id is None:
            return  # notification سمت سرور؛ فعلاً فقط نادیده گرفته می‌شود
        try:
            key = int(raw_id)
        except (TypeError, ValueError):
            return
        pending = self._pending.pop(key, None)
        if pending is None or pending.future.done():
            return
        if "error" in message:
            err = message.get("error") or {}
            pending.future.set_exception(
                MCPError(str(err.get("message") or "unknown error"), code=err.get("code"), data=err.get("data"))
            )
            return
        pending.future.set_result(message.get("result"))

    def _fail_all_pending(self, reason: str) -> None:
        """همه‌ی درخواست‌های باز را با خطا می‌بندد."""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(MCPError(reason))
        self._pending.clear()

    async def _connect_http(self) -> None:
        """session HTTP را می‌سازد."""
        if self._session_factory is not None:
            self._http_session = self._session_factory()
        else:  # pragma: no cover - در تست‌ها factory تزریق می‌شود
            import aiohttp

            self._http_session = aiohttp.ClientSession()
        self._http_endpoint = self.config.url

    # ------------------------------------------------------------------ JSON-RPC
    def _make_id(self) -> int:
        """شناسه‌ی یکتای درخواست."""
        self._next_id += 1
        return self._next_id

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        """یک درخواست JSON-RPC می‌فرستد و منتظر پاسخ می‌ماند.

        Args:
            method: نام متد.
            params: پارامترها.
            timeout: سقف انتظار (پیش‌فرض از config).

        Raises:
            MCPError: اگر اتصال نباشد یا سرور خطا بدهد.
        """
        if self.status not in (MCPServerStatus.CONNECTING, MCPServerStatus.CONNECTED):
            raise MCPError(f"server '{self.config.name}' is not connected ({self.status.value})")
        request_id = self._make_id()
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        limit = timeout if timeout is not None else self.config.timeout
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = _Pending(future=future, method=method)
        try:
            await self._send(payload)
            return await asyncio.wait_for(future, timeout=limit)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise MCPError(f"{method} timed out after {limit:.0f}s") from None
        except Exception:
            self._pending.pop(request_id, None)
            raise

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """یک notification (بدون انتظار پاسخ)."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._send(payload)
        except Exception as exc:  # noqa: BLE001 - notification نباید جریان را بشکند
            logger.debug("mcp %s: notify %s failed: %s", self.config.name, method, exc)

    async def _send(self, payload: dict[str, Any]) -> None:
        """payload را روی ترنسپورت فعلی می‌فرستد."""
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        if self.config.transport is MCPTransport.STDIO:
            process = self._process
            if process is None or process.stdin is None:
                raise MCPError("stdio process is not running")
            process.stdin.write(raw)
            await process.stdin.drain()
            return
        if self._http_session is None:
            raise MCPError("http session is not open")
        async with self._http_session.post(
            self._http_endpoint,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **self.config.headers,
            },
            timeout=self.config.timeout,
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise MCPError(f"HTTP {response.status}: {body[:300]}")
            text = await response.text()
        for message in _iter_sse_or_json(text):
            self._dispatch(message)

    async def _initialize(self) -> dict[str, Any]:
        """handshake پروتکل."""
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "clientInfo": CLIENT_INFO,
            },
        )
        data: dict[str, Any] = result if isinstance(result, dict) else {}
        # دو بار ``data.get(...)`` صدا زده نشود: mypy نوع بازگشتی را narrow
        # نمی‌کند وقتی شرط و انتساب هر دو ``get`` جداگانه باشند.
        info = data.get("serverInfo")
        caps = data.get("capabilities")
        self.server_info = info if isinstance(info, dict) else {}
        self.capabilities = caps if isinstance(caps, dict) else {}
        await self.notify("notifications/initialized", {})
        return self.server_info

    async def ping(self) -> bool:
        """سلامت اتصال."""
        try:
            await self.request("ping", {}, timeout=min(5.0, self.config.timeout))
        except MCPError:
            return False
        return True

    # ------------------------------------------------------------------ tools
    async def refresh_tools(self) -> list[MCPTool]:
        """فهرست ابزارها را می‌گیرد و اسکن injection می‌کند."""
        result = await self.request("tools/list", {})
        raw_tools = result.get("tools", []) if isinstance(result, dict) else []
        self.tools.clear()
        for item in raw_tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            description = str(item.get("description") or "")
            report = self.scanner.scan(description)
            if report.at_least("high"):
                logger.warning(
                    "mcp %s: tool %r rejected by injection scanner (%s)", self.config.name, name, report.summary()
                )
                continue
            schema = item.get("inputSchema")
            self.tools[name] = MCPTool(
                server=self.config.name,
                name=name,
                description=description[:1000],
                input_schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            )
        return list(self.tools.values())

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """یک ابزار راه دور را صدا می‌کند.

        Args:
            name: نام ابزار (بدون پیشوند).
            arguments: آرگومان‌ها.

        Returns:
            ``{"content": [...], "isError": bool}`` نرمال‌شده.

        Raises:
            MCPError: اگر ابزار ناشناخته باشد، گارد رد کند، یا سرور خطا دهد.
        """
        if name not in self.tools:
            raise MCPError(f"server '{self.config.name}' has no tool named '{name}'")
        args = dict(arguments or {})
        if self.safety_guard is not None:
            self._guard_arguments(args)
        result = await self.request("tools/call", {"name": name, "arguments": args})
        return _normalize_tool_result(result)

    def _guard_arguments(self, args: dict[str, Any]) -> None:
        """مقادیر مسیر/URL/دستور را با گارد موجود بررسی می‌کند.

        Raises:
            MCPError: اگر گارد عملیات را مسدود کند.
        """
        guard = self.safety_guard
        for key, value in list(args.items()):
            if not isinstance(value, str):
                continue
            low = key.lower()
            decision = None
            if any(hint in low for hint in ("command", "cmd", "shell", "script")):
                decision = guard.assess_command(value)
            elif any(hint in low for hint in ("path", "file", "dir", "folder")):
                decision = guard.assess_path(value, action="write")
            elif any(hint in low for hint in ("url", "uri", "endpoint", "host")):
                decision = guard.assess_network(value)
            if decision is not None and getattr(decision, "blocked", False):
                raise MCPError(f"safety guard blocked '{key}': {getattr(decision, 'reason_text', 'blocked')}")

    # ------------------------------------------------------------------ prompts / resources
    async def list_prompts(self) -> list[dict[str, Any]]:
        """فهرست promptهای سرور (اگر پشتیبانی کند)."""
        try:
            result = await self.request("prompts/list", {})
        except MCPError:
            return []
        items = result.get("prompts", []) if isinstance(result, dict) else []
        return [item for item in items if isinstance(item, dict)]

    async def list_resources(self) -> list[dict[str, Any]]:
        """فهرست منابع سرور."""
        try:
            result = await self.request("resources/list", {})
        except MCPError:
            return []
        items = result.get("resources", []) if isinstance(result, dict) else []
        return [item for item in items if isinstance(item, dict)]

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """خواندن یک منبع."""
        result = await self.request("resources/read", {"uri": str(uri)})
        return result if isinstance(result, dict) else {"contents": []}

    # ------------------------------------------------------------------ teardown
    async def close(self) -> None:
        """اتصال را می‌بندد و subprocess را تمام می‌کند."""
        self._fail_all_pending("client closing")
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001 - در حال لغو است
                await self._reader_task
            self._reader_task = None
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except (asyncio.TimeoutError, OSError, ProcessLookupError):
                with contextlib.suppress(OSError, ProcessLookupError):
                    process.kill()
        if self._http_session is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001 - بستن session نباید جریان را بشکند
                await self._http_session.close()
            self._http_session = None
        if self.status is not MCPServerStatus.ERROR:
            self.status = MCPServerStatus.DISCONNECTED

    async def __aenter__(self) -> MCPClient:
        """ورود به context manager."""
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """خروج از context manager."""
        await self.close()

    def describe(self) -> dict[str, Any]:
        """خلاصه برای ``/api/mcp``."""
        return {
            "name": self.config.name,
            "transport": self.config.transport.value,
            "status": self.status.value,
            "error": self.error,
            "server_info": self.server_info,
            "capabilities": sorted(self.capabilities),
            "tools": [tool.as_dict() for tool in self.tools.values()],
        }


def _iter_sse_or_json(text: str) -> list[dict[str, Any]]:
    """پاسخ HTTP را پارس می‌کند: JSON ساده یا ``text/event-stream``."""
    body = (text or "").strip()
    if not body:
        return []
    if body.startswith("{") or body.startswith("["):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []
        return [data] if isinstance(data, dict) else [item for item in data if isinstance(item, dict)]
    out: list[dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _normalize_tool_result(result: Any) -> dict[str, Any]:
    """پاسخ ``tools/call`` را به شکل یکنواخت درمی‌آورد."""
    if not isinstance(result, dict):
        return {"content": [{"type": "text", "text": str(result)}], "isError": False}
    content = result.get("content")
    if not isinstance(content, list):
        content = [{"type": "text", "text": str(content) if content is not None else ""}]
    cleaned = [item for item in content if isinstance(item, dict)]
    return {"content": cleaned, "isError": bool(result.get("isError", False))}


def load_mcp_config(path: str | Path) -> list[MCPServerConfig]:
    """فایل پیکربندی MCP را می‌خواند.

    قالب::

        {
          "mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
            "remote": {"transport": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "…"}}
          }
        }

    همان کلید ``mcpServers`` که در اکوسیستم رایج است، پس فایل‌های موجود
    مستقیماً کار می‌کنند.

    Args:
        path: مسیر فایل JSON.

    Returns:
        فهرست پیکربندی‌های معتبر (نامعتبرها با warning رد می‌شوند).
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return []
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("mcp: cannot read %s: %s", file_path, exc)
        return []
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        return []
    configs: list[MCPServerConfig] = []
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        payload = dict(raw)
        payload.setdefault("name", name)
        if "url" in payload and "transport" not in payload:
            payload["transport"] = "http"
        try:
            config = MCPServerConfig.model_validate(payload)
            config.validate_transport()
        except Exception as exc:  # noqa: BLE001 - یک سرور خراب نباید بقیه را بگیرد
            logger.warning("mcp: skipping server %r: %s", name, exc)
            continue
        configs.append(config)
    return configs

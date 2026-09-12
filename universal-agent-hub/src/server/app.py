"""اپلیکیشن HTTP/WebSocket سرور اپ‌ها (aiohttp).

چرا aiohttp؟ چون همین حالا یک وابستگی اصلی پروژه است؛ بنابراین «سرور + اپ
موبایل» بدون اضافه‌کردن فریمورک تازه (FastAPI/uvicorn) کار می‌کند و روی Termux
اندروید هم اجرا می‌شود.

طراحی:

* ``/healthz`` — بررسی زنده بودن، بدون احراز (برای healthcheck داکر و اپ).
* ``/api/*``   — REST؛ احراز با توکن + محدودساز نرخ + بررسی Host (ضد DNS rebinding).
* ``/ws``      — WebSocket: جریان رویداد زنده، تأییدها و اجرای پیاپی.
* ``/`` …      — UI وب (PWA) از پوشه‌ی ``web/``؛ روی موبایل با «Add to Home screen»
  مثل یک اپ نصب می‌شود.

نکته‌ی ایمنی: اگر ``SERVER_TOKEN`` خالی باشد و سرور روی آدرس غیر loopback باز
شود، :func:`create_app` با پیام راهنما خطا می‌دهد (fail closed).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import ValidationError as PydanticValidationError

from src import __version__
from src.config import Config, get_config
from src.core.services import ServiceHub
from src.core.tool_registry import ToolRegistry, discover_tools
from src.server.auth import RateLimiter, TokenAuthorizer, is_loopback_address, unauthorized
from src.server.keystore import KeyStore, redact_text
from src.server.protocol import (
    ApiKeyRequest,
    ApprovalDecision,
    MemoryNoteRequest,
    RunRequest,
    SessionUpdate,
    ToolInvokeRequest,
    WsMessage,
)
from src.server.sessions import AgentSession, SessionRegistry
from src.utils.helpers import truncate_text
from src.utils.logger import get_logger

logger = get_logger("server")


@dataclass
class ServerStats:
    """شمارنده‌های زنده.

    عمداً یک object قابل‌تغییر است که یک‌بار در app ثبت می‌شود: بعد از start شدن
    اپلیکیشن نمی‌توان کلید تازه‌ای در ``app`` گذاشت، وگرنه هر اتصال WebSocket با
    ``RuntimeError: Cannot mutate the Application`` می‌شکست.
    """

    sockets: int = 0
    runs: int = 0
    approved: int = 0
    denied: int = 0

    def as_dict(self) -> dict[str, int]:
        """نسخه‌ی JSON برای اپ."""
        return {"sockets": self.sockets, "runs": self.runs, "approved": self.approved, "denied": self.denied}


#: مسیرهای سنگین که با RateLimiter محافظت می‌شوند (run/invoke/کلیدها)
LIMITED_PATHS: frozenset[str] = frozenset({"/api/run", "/api/keys"})


def _is_rate_limited(method: str, path: str) -> bool:
    """آیا این درخواست ظرفیت‌بر است و باید نرخ‌بندی شود؟

    ``/api/run`` و ``POST /api/keys`` دقیقاً لیست‌اند؛ ``/invoke`` زیر مسیر
    نام ابزار است، پس با پسوند تطبیق داده می‌شود. ``/api/run/stop`` عمداً
    محدود نمی‌شود (لغو باید همیشه ممکن باشد).
    """
    if method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return False
    return path in LIMITED_PATHS or path.endswith("/invoke")


# کلیدهای typed برای app state (توصیه‌ی aiohttp ≥ 3.9؛ از NotAppKeyWarning جلوگیری می‌کند)
APP_CONFIG: web.AppKey[Config] = web.AppKey("agent_hub_config", Config)
APP_AUTH: web.AppKey[TokenAuthorizer] = web.AppKey("agent_hub_auth", TokenAuthorizer)
APP_LIMITER: web.AppKey[RateLimiter] = web.AppKey("agent_hub_limiter", RateLimiter)
APP_SESSIONS: web.AppKey[SessionRegistry] = web.AppKey("agent_hub_sessions", SessionRegistry)
APP_KEYSTORE: web.AppKey[KeyStore] = web.AppKey("agent_hub_keystore", KeyStore)
APP_STARTED_AT: web.AppKey[float] = web.AppKey("agent_hub_started_at", float)
#: شمارنده‌های زنده؛ **باید mutable باشد** چون بعد از start شدن app نتوانیم کلید تازه می‌گذاریم
APP_STATS: web.AppKey[ServerStats] = web.AppKey("agent_hub_stats", ServerStats)
#: ``ServiceHub`` مشترک (روتین‌ها/اعلان‌ها/skillها/MCP/بکاپ/ممیزی/heartbeat).
APP_SERVICES: web.AppKey[ServiceHub] = web.AppKey("agent_hub_services", ServiceHub)

#: مسیرهای مجاز بدون توکن (سلامت و فایل‌های UI)
PUBLIC_PATHS: frozenset[str] = frozenset({"/healthz", "/favicon.ico", "/manifest.webmanifest", "/sw.js"})
#: پیام مشترک «ابتدا کلید API را تنظیم کنید»
NO_API_KEY_HINT = "no model API key configured — add one in the Keys panel or in .env (OPENAI_API_KEY)"


def _json(payload: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    """پاسخ JSON یکدست (utf-8، بدون escape فارسی)."""
    body = json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)
    return web.json_response(text=body, status=status, headers=headers)


def _error(message: str, code: str, *, status: int = 400, **details: Any) -> web.Response:
    """پاسخ خطا با ساختار ثابت که همه‌ی کلاینت‌ها می‌شناسند."""
    payload: dict[str, Any] = {"error": redact_secrets_message(message), "error_code": code}
    if details:
        payload["details"] = details
    return _json(payload, status=status)


def redact_secrets_message(text: str) -> str:
    """ماسک‌کردن کلیدها در پیام‌های خطای HTTP."""
    return redact_text(str(text or ""))


def session_id_of(request: web.Request, payload: Any = None) -> str:
    """شناسه‌ی session از مسیر، هدر یا بدنه (به همین ترتیب اولویت)."""
    match = request.match_info.get("id", "") if hasattr(request, "match_info") else ""
    if match:
        return str(match).strip()[:64]
    header = request.headers.get("X-Agent-Session", "").strip()
    if header:
        return header[:64]
    if isinstance(payload, dict):
        return str(payload.get("session_id") or "").strip()[:64]
    return str(request.query.get("session") or "").strip()[:64]


def store(request: web.Request) -> SessionRegistry:
    """دسترسی به رجیستری session ها."""
    return registry_for(request.app)


def registry_for(app: web.Application) -> SessionRegistry:
    """رجیستری روی خودِ app (برای مسیرهای بستن/پاک‌سازی)."""
    return app[APP_SESSIONS]


def config_of(request: web.Request) -> Config:
    """تنظیمات سرور."""
    return request.app[APP_CONFIG]


def key_store(request: web.Request) -> KeyStore:
    """مخزن کلیدهای API."""
    return request.app[APP_KEYSTORE]


def authorizer_for(app: web.Application) -> TokenAuthorizer:
    """لایه‌ی احراز (برای WS و پیام‌های راهنما)."""
    return app[APP_AUTH]


# ---------------------------------------------------------------------------
# میان‌افزارها
# ---------------------------------------------------------------------------
@web.middleware
async def security_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """احراز توکن، بررسی Host و محدودساز نرخ برای ``/api/*``."""
    auth: TokenAuthorizer = request.app[APP_AUTH]
    path = request.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS:
        expected_host = str(request.app[APP_CONFIG].server_host)
        forwarded_host = request.headers.get("Host", "").split(":")[0].strip().lower()
        if forwarded_host not in {"", "localhost", "127.0.0.1", "::1", expected_host.lower(), "0.0.0.0", "[::1]"}:
            logger.warning("rejected request with unexpected Host header: %s", forwarded_host)
            return _error("host is not allowed (possible DNS rebinding)", "bad_host", status=403)
        if not auth.is_allowed(request):
            return unauthorized(
                "invalid token" if auth.enabled else "this server only accepts requests from the local machine"
            )
        if _is_rate_limited(request.method, path):
            limiter: RateLimiter = request.app[APP_LIMITER]
            key = request.remote or "unknown"
            if not limiter.allow(key):
                return _error(
                    f"too many requests ({limiter.per_minute}/min)",
                    "rate_limited",
                    status=429,
                    retry_after=limiter.retry_after(key),
                )
    return await handler(request)


@web.middleware
async def errors_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """تبدیل استثنای پیش‌بینی‌نشده به ۵۰۰ JSON (هرگز traceback ندهیم)."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except asyncio.CancelledError:  # pragma: no cover - بسته‌شدن سرور
        raise
    except Exception as exc:  # noqa: BLE001 - مرز نهایی API
        logger.exception("unhandled error on %s %s", request.method, request.path)
        return _error(f"{type(exc).__name__}: {exc}", "server_error", status=500)


# ---------------------------------------------------------------------------
# کمک‌توابع
# ---------------------------------------------------------------------------
async def read_json_dict(request: web.Request) -> dict[str, Any]:
    """خواندن بدنه‌ی JSON به‌صورت dict (خطای ۴۰۰ تمیز اگر نامعتبر باشد)."""
    raw: Any = {}
    if request.can_read_body:
        try:
            raw = await request.json(loads=json.loads)
        except (json.JSONDecodeError, TypeError) as exc:
            raise web.HTTPBadRequest(text='{"error":"invalid json body"}', content_type="application/json") from exc
    if not isinstance(raw, dict):
        raise web.HTTPBadRequest(text='{"error":"body must be a JSON object"}', content_type="application/json")
    return dict(raw)


async def read_model(request: web.Request, model: Any) -> Any:
    """خواندن بدنه و اعتبارسنجی با یک مدل pydantic."""
    return await _validate(model, await read_json_dict(request))


async def _validate(model: Any, raw: dict[str, Any]) -> Any:
    """اعتبارسنجی dict با مدل pydantic و تبدیل خطا به ۴۰۰ JSON."""
    try:
        return model(**raw)
    except PydanticValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()[:4]
        )
        raise web.HTTPBadRequest(
            text=json.dumps({"error": problems, "error_code": "invalid_request"}, ensure_ascii=False),
            content_type="application/json",
        ) from exc


def tools_payload(config: Config) -> list[dict[str, Any]]:
    """فهرست ابزارها با ریسک/تأیید/دسته (برای پنل ابزارها)."""
    discover_tools()
    ToolRegistry.set_default_config(config)
    return [tool.to_info().model_dump(mode="json") for tool in ToolRegistry.instances(config=config)]


def resolve_session(request: web.Request, *, create: bool = True, **kwargs: Any) -> AgentSession:
    """session درخواست‌کننده (ایجاد در صورت نبود، مگر ``create=False``)."""
    key = session_id_of(request)
    registry = store(request)
    if not create:
        session = registry.peek(key)
        if session is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": f"no such session: {key or '(empty)'}", "error_code": "no_session"}),
                content_type="application/json",
            )
        return session
    return registry.get(key, **kwargs)


def agent_readiness(request: web.Request) -> tuple[bool, str]:
    """آیا می‌شود همین حالا مدل را صدا زد؟ (کلید تنظیم شده؟)"""
    candidate = store(request).peek(session_id_of(request))
    effective = candidate.config if candidate is not None else config_with_keys(request)
    if str(getattr(effective, "openai_api_key", "") or ""):
        return True, ""
    return False, NO_API_KEY_HINT


def config_with_keys(request: web.Request) -> Config:
    """config پایه با پروفایل فعال مخزن کلید (برای بررسی آمادگی)."""
    keystore: KeyStore = key_store(request)
    updates = keystore.active_config_values()
    if not updates:
        return config_of(request)
    return config_of(request).model_copy(update=updates)


# ---------------------------------------------------------------------------
# مسیرها
# ---------------------------------------------------------------------------
async def handle_health(request: web.Request) -> web.Response:
    """``GET /healthz`` — اطلاعات بی‌خطر برای سلامت/نسخه."""
    config = config_of(request)
    discover_tools()
    return _json(
        {
            "status": "ok",
            "version": __version__,
            "model": config.active_model or config.model_name,
            "tools": len(ToolRegistry.names()),
            "auth_required": bool(config.server_token),
            "profiles": sorted(_profile_names()),
            "uptime_seconds": round(time.time() - float(request.app[APP_STARTED_AT]), 1),
        }
    )


def _profile_names() -> set[str]:
    """نام پروفایل‌های ثبت‌شده در کارخانه."""
    from src.core.agent_factory import AgentFactory

    return set(AgentFactory.profiles())


async def handle_status(request: web.Request) -> web.Response:
    """``GET /api/status`` — وضعیت کامل برای صفحه‌ی تنظیمات اپ."""
    config = config_of(request)
    session = store(request).peek(session_id_of(request))
    ready, hint = agent_readiness(request)
    web_root = config.web_root
    return _json(
        {
            "version": __version__,
            "ready": ready,
            "ready_hint": hint or None,
            "config": config.to_safe_dict(),
            "safety": session.agent.safety_summary() if session else None,
            "auth": request.app[APP_AUTH].describe(),
            "keystore": key_store(request).status(),
            "ui_available": web_root is not None,
            "tool_count": len(ToolRegistry.names()),
            "stats": request.app[APP_STATS].as_dict(),
            "session": session.describe() if session else None,
            "sessions": [item.describe() for item in store(request).all()],
            "profiles": sorted(_profile_names()),
            "capabilities": {
                "direct_tool_calls": bool(config.server_allow_direct_tools),
                "approvals": True,
                "websocket": True,
                "max_body_bytes": int(config.server_max_body_bytes),
            },
        }
    )


async def handle_tools(request: web.Request) -> web.Response:
    """``GET /api/tools`` — فهرست ابزارها (+ فیلتر دسته با ``?category=``)."""
    payload = tools_payload(config_of(request))
    category = request.query.get("category", "").strip().lower()
    if category:
        payload = [item for item in payload if str(item.get("category", "")).lower() == category]
    search = request.query.get("q", "").strip().lower()
    if search:
        payload = [
            item
            for item in payload
            if search in str(item.get("name", "")).lower() or search in str(item.get("description", "")).lower()
        ]
    return _json({"count": len(payload), "tools": payload})


async def handle_tool_schema(request: web.Request) -> web.Response:
    """``GET /api/tools/{name}`` — اسکیمای OpenAI یک ابزار."""
    name = request.match_info["name"]
    tool = ToolRegistry.get(name, config=config_of(request))
    if tool is None:
        return _error(f"no tool named '{name}'", "unknown_tool", status=404)
    return _json({"name": name, "schema": tool.get_schema(), "info": tool.to_info().model_dump(mode="json")})


async def handle_tool_invoke(request: web.Request) -> web.Response:
    """``POST /api/tools/{name}/invoke`` — اجرای مستقیم ابزار (با همان لایه‌ی ایمنی)."""
    config = config_of(request)
    if not config.server_allow_direct_tools:
        return _error("direct tool calls are disabled (SERVER_ALLOW_DIRECT_TOOLS=false)", "disabled", status=403)
    name = request.match_info["name"]
    tool = ToolRegistry.get(name, config=config)
    if tool is None:
        return _error(f"no tool named '{name}'", "unknown_tool", status=404)
    payload = await read_json_dict(request)
    payload.setdefault("tool", name)
    body = await _validate(ToolInvokeRequest, payload)
    session = store(request).get(body.session_id or session_id_of(request))
    context = session.context
    result = await tool.run(dict(body.arguments), context=context)
    return _json(result.model_dump(mode="json"))


async def handle_profiles(request: web.Request) -> web.Response:
    """``GET /api/profiles`` — پروفایل‌های آماده‌ی ایجنت."""
    from src.core.agent_factory import AgentFactory

    names = AgentFactory.profiles()
    return _json(
        {
            "profiles": [
                {
                    "name": name,
                    "description": getattr(profile, "description", ""),
                    "tools": AgentFactory.tools_for(profile),
                    "enabled_tools": sorted(getattr(profile, "enabled_tools", None) or []) or None,
                    "disabled_tools": sorted(getattr(profile, "disabled_tools", None) or []) or None,
                    "temperature": getattr(profile, "temperature", None),
                    "max_tool_iterations": getattr(profile, "max_tool_iterations", None),
                    "model": getattr(profile, "model", None) or None,
                    "auto_confirm_all": bool(getattr(profile, "auto_confirm_all", False)),
                }
                for name, profile in sorted(names.items())
            ]
        }
    )


async def handle_run(request: web.Request) -> web.Response:
    """``POST /api/run`` — اجرای یک درخواست روی session (بلوکه تا پایان)."""
    body = await read_model(request, RunRequest)
    registry = store(request)
    if body.new_session:
        session = registry.get("")
    else:
        kwargs: dict[str, Any] = {}
        if body.profile:
            kwargs["profile"] = body.profile
        if body.tools:
            kwargs["tools"] = list(body.tools)
        if body.system_note:
            kwargs["system_note"] = body.system_note
        # اپ‌ها شناسه را گاهی در هدر و گاهی در body می‌فرستند؛ هر دو پذیرفته می‌شود
        session = registry.get(body.session_id or session_id_of(request), **kwargs)
    ready, hint = agent_readiness(request)
    if not ready:
        return _error(hint, "missing_api_key", status=503)
    result = await session.run(body.prompt, timeout=float(body.timeout or 900))
    request.app[APP_STATS].runs += 1
    status = 200 if result.get("ok", True) else 422
    return _json(result, status=status)


async def handle_run_stop(request: web.Request) -> web.Response:
    """``POST /api/run/stop`` — لغو اجرای جاری یک session."""
    session = resolve_session(request)
    stopped = session.stop()
    return _json({"cancelled": stopped, "session_id": session.id})


async def handle_sessions(request: web.Request) -> web.Response:
    """``GET /api/sessions`` — فهرست session های فعال."""
    registry = store(request)
    registry.expire_stale()
    return _json({"count": len(registry), "sessions": [item.describe() for item in registry.all()]})


async def handle_create_session(request: web.Request) -> web.Response:
    """``POST /api/sessions`` — ساخت session تازه با تنظیمات دلخواه."""
    body = await read_model(request, SessionUpdate)
    session = store(request).get("")
    if body.updates():
        session.apply_updates(body.updates())
    return _json(session.describe(), status=201)


async def handle_get_session(request: web.Request) -> web.Response:
    """``GET /api/sessions/{id}`` — توضیح یک session."""
    session = resolve_session(request, create=False)
    return _json(session.describe())


async def handle_patch_session(request: web.Request) -> web.Response:
    """``PATCH /api/sessions/{id}`` — تغییر پروفایل/ابزارها/تنظیمات مجاز."""
    session = resolve_session(request, create=False)
    body = await read_model(request, SessionUpdate)
    try:
        changed = session.apply_updates(body.updates())
    except KeyError as exc:
        return _error(str(exc).strip("'"), "invalid_update", status=422)
    return _json({"changed": changed, "session": session.describe()})


async def handle_delete_session(request: web.Request) -> web.Response:
    """``DELETE /api/sessions/{id}`` — بستن session."""
    session_id = request.match_info["id"]
    removed = store(request).remove(session_id)
    if not removed:
        return _error(f"no such session: {session_id}", "no_session", status=404)
    return _json({"closed": session_id})


async def handle_reset_session(request: web.Request) -> web.Response:
    """``POST /api/sessions/{id}/reset`` — پاک کردن تاریخچه‌ی گفت‌وگو."""
    session = resolve_session(request, create=False)
    session.reset()
    return _json({"reset": session.id, "session": session.describe()})


async def handle_history(request: web.Request) -> web.Response:
    """``GET /api/sessions/{id}/history`` — پیام‌های گفت‌وگو."""
    session = resolve_session(request, create=False)
    limit = int(request.query.get("limit", "40") or 40)
    return _json({"session_id": session.id, "messages": session.history(limit=min(max(1, limit), 200))})


async def handle_events(request: web.Request) -> web.Response:
    """``GET /api/sessions/{id}/events`` — رویدادهای اخیر (برای polling)."""
    session = resolve_session(request, create=False)
    limit = int(request.query.get("limit", "40") or 40)
    since = float(request.query.get("since", "0") or 0)
    return _json(
        {"session_id": session.id, "events": session.recent_events(limit=min(max(1, limit), 200), since=since)}
    )


async def handle_list_approvals(request: web.Request) -> web.Response:
    """``GET /api/sessions/{id}/approvals`` — درخواست‌های تأیید باز."""
    session = resolve_session(request, create=False)
    session.approvals.touch()
    return _json({"pending": session.approvals.pending})


async def handle_resolve_approval(request: web.Request) -> web.Response:
    """``POST /api/sessions/{id}/approvals`` — پاسخ کاربر به یک درخواست تأیید."""
    session = resolve_session(request, create=False)
    body = await read_model(request, ApprovalDecision)
    resolved = session.approvals.resolve(body.request_id, body.approved, reason=body.reason)
    if resolved:
        stats = request.app[APP_STATS]
        stats.approved += int(body.approved)
        stats.denied += int(not body.approved)
    if not resolved:
        return _error("that approval is already answered or unknown", "stale_approval", status=409)
    return _json({"resolved": body.request_id, "approved": body.approved})


async def handle_list_keys(request: web.Request) -> web.Response:
    """``GET /api/keys`` — پروفایل‌های کلید API (بدون خودِ کلید)."""
    keystore = key_store(request)
    return _json({"status": keystore.status(), "profiles": keystore.all()})


async def handle_put_key(request: web.Request) -> web.Response:
    """``POST /api/keys`` — ساخت/به‌روزرسانی یک پروفایل کلید (و اختیاری فعال‌سازی)."""
    body = await read_model(request, ApiKeyRequest)
    keystore = key_store(request)
    try:
        record = keystore.put(
            body.name,
            api_key=body.api_key,
            base_url=body.base_url,
            model=body.model,
            fallbacks=body.fallbacks,
            profile=body.profile,
        )
    except ValueError as exc:
        return _error(str(exc), "invalid_key", status=422)
    if body.activate:
        keystore.activate(record.name)
        for session in store(request).all():
            session.invalidate_agent()
    return _json({"saved": record.to_dict(), "active": keystore.active_name, "status": keystore.status()}, status=201)


async def handle_delete_key(request: web.Request) -> web.Response:
    """``DELETE /api/keys/{name}`` — حذف یک پروفایل کلید."""
    name = request.match_info["name"]
    keystore = key_store(request)
    if not keystore.delete(name):
        return _error(f"no such key profile: {name}", "unknown_profile", status=404)
    return _json({"deleted": name, "active": keystore.active_name, "status": keystore.status()})


async def handle_activate_key(request: web.Request) -> web.Response:
    """``POST /api/keys/{name}/activate`` — انتخاب پروفایل فعال و اعمال آن روی session ها."""
    name = request.match_info["name"]
    keystore = key_store(request)
    if not keystore.activate(name):
        return _error(f"no such key profile: {name}", "unknown_profile", status=404)
    values = keystore.active_config_values()
    touched = 0
    for session in store(request).all():
        try:
            session.apply_updates({key: value for key, value in values.items() if key in SessionUpdate.model_fields})
        except KeyError:  # pragma: no cover - فیلدهای مجاز از قبل فیلتر شده‌اند
            continue
        # کلید/مدل تازه در config پایه نیست؛ ایجنت باید با تنظیمات جدید بازسازی شود
        session.invalidate_agent()
        touched += 1
    return _json(
        {
            "active": keystore.active_name,
            "applied_to_sessions": touched,
            "masked_key": (keystore.active.masked_key if keystore.active else None),
        }
    )


async def handle_import_env(request: web.Request) -> web.Response:
    """``POST /api/keys/import-env`` — کپی کلید فعلی ``.env`` به مخزن (برای شروع سریع)."""
    config = config_of(request)
    keystore = key_store(request)
    if not config.openai_api_key:
        return _error("OPENAI_API_KEY is not set in the server environment", "nothing_to_import", status=409)
    record = keystore.put(
        request.query.get("name", "from-env"),
        api_key=config.openai_api_key,
        base_url=config.openai_base_url or "",
        model=config.model_name,
        fallbacks=list(config.model_fallbacks),
    )
    keystore.activate(record.name)
    return _json({"imported": record.to_dict(), "active": keystore.active_name}, status=201)


async def handle_config(request: web.Request) -> web.Response:
    """``GET /api/config`` — تنظیمات مؤثر (ماسک‌شده) + راهنمای fieldها."""
    config = config_of(request)
    data = config.to_safe_dict()
    return _json(
        {
            "config": data,
            "editable": sorted(SessionUpdate.model_fields),
            "notes": {
                "model_name": "gpt-6-astra فقط وقتی کار می‌کند که OPENAI_BASE_URL آن را ارائه دهد؛ در غیر این صورت MODEL_FALLBACKS را تنظیم کنید.",
                "allowed_directories": "همه‌ی ابزارهای فایل فقط داخل این مسیرها کار می‌کنند.",
                "enable_confirmation": "اگر خاموش شود، عملیات حساس بدون پرسیدن اجرا می‌شود.",
            },
        }
    )


async def handle_safety(request: web.Request) -> web.Response:
    """``GET /api/safety`` — سیاست ایمنی فعال (برای پنل ایمنی)."""
    session = resolve_session(request)
    return _json(session.agent.safety_summary())


async def handle_reports(request: web.Request) -> web.Response:
    """``GET /api/reports`` — گزارش فعالیت: اجراها، ابزارها، تأییدها، برنامه‌های باز.

    فقط‌خواندنی است و از همان دو فایل محلی (حافظه + activity.jsonl) می‌خواند؛ برای
    تب «Reports» در UI و برای صفحه‌ی «چه کردی؟» در اپ ساخته شده است.
    """
    from src.core.reports import build_report, render_report_text

    config = config_of(request)
    raw_days = request.query.get("days", "7")
    try:
        days = float(raw_days)
    except (TypeError, ValueError):
        return _error("days must be a number (0 = all time)", "invalid_input", status=400)
    if not 0 <= days <= 3650:
        return _error("days must be between 0 and 3650", "invalid_input", status=400)
    session = resolve_session(request)
    report = build_report(config, days=days, memory=getattr(session.agent, "memory", None))
    title = f"Activity report · last {days:g} days" if days > 0 else "Activity report · all time"
    return _json({"report": report, "text": render_report_text(report, title=title)})


async def handle_memory_list(request: web.Request) -> web.Response:
    """``GET /api/memory`` — فهرست/جست‌وجوی رکوردهای حافظه (بدون افشای متن‌های بلند)."""
    from src.core.memory import MEMORY_KINDS, AgentMemory

    memory = getattr(resolve_session(request).agent, "memory", None) or AgentMemory.for_config(config_of(request))
    if memory is None:
        return _error(
            "long-term memory is disabled on this install (MEMORY_ENABLED=false)",
            "memory_disabled",
            status=409,
        )
    query = str(request.query.get("q", "")).strip()
    try:
        limit = max(1, min(50, int(request.query.get("limit", "20"))))
    except (TypeError, ValueError):
        return _error("limit must be an integer between 1 and 50", "invalid_input", status=400)
    kind = str(request.query.get("kind", "")).strip()
    kinds = (kind,) if kind in MEMORY_KINDS else None
    records = memory.search(query, kinds=kinds, limit=limit) if query else memory.recent(limit, kinds=kinds)
    memory.touch(records)
    return _json(
        {
            "enabled": True,
            "query": query,
            "stats": memory.stats(),
            "kinds": list(MEMORY_KINDS),
            "records": [item.as_dict() for item in records],
        }
    )


async def handle_memory_write(request: web.Request) -> web.Response:
    """``POST /api/memory`` — افزودن یک یادداشت (کاربر یا پنل، به‌صورت دستی)."""
    from src.core.memory import AgentMemory

    body = await read_model(request, MemoryNoteRequest)
    memory = getattr(resolve_session(request).agent, "memory", None) or AgentMemory.for_config(config_of(request))
    if memory is None:
        return _error(
            "long-term memory is disabled on this install (MEMORY_ENABLED=false)",
            "memory_disabled",
            status=409,
        )
    record = memory.add(
        body.content, kind=body.kind, tags=list(body.tags), source="api:memory", pin=body.pin, confidence=1.0
    )
    if record is None:
        return _error("memory refused the note (empty content?)", "invalid_input", status=422)
    return _json({"saved": record.as_dict(), "stats": memory.stats()}, status=201)


async def handle_memory_forget(request: web.Request) -> web.Response:
    """``DELETE /api/memory/{id}`` — حذف یک رکورد (پinned فقط با ``?force=1``)."""
    from src.core.memory import AgentMemory

    memory = getattr(resolve_session(request).agent, "memory", None) or AgentMemory.for_config(config_of(request))
    if memory is None:
        return _error(
            "long-term memory is disabled on this install (MEMORY_ENABLED=false)",
            "memory_disabled",
            status=409,
        )
    record_id = request.match_info.get("id", "").strip()
    record = memory.find(record_id)
    if record is None:
        return _error(f"no memory record with id '{record_id}'", "not_found", status=404)
    force = request.query.get("force", "").lower() in {"1", "true", "yes"}
    if record.pinned and not force:
        return _error("record is pinned; repeat with ?force=1 to remove it", "pinned", status=409, id=record.id)
    memory.forget(record.id)
    return _json({"deleted": record.id, "stats": memory.stats()})


async def handle_ui_root(request: web.Request) -> web.StreamResponse:
    """``GET /`` — صفحه‌ی اصلی UI (یا پیام متنی اگر فایل‌ها نصب نشده باشند)."""
    root = config_of(request).web_root
    if root is None:
        return _json(
            {
                "app": "universal-agent-hub",
                "version": __version__,
                "ui": "not installed — use the REST/WebSocket API or copy the web/ folder next to the server",
                "docs": "docs/apps.md",
            }
        )
    index = root / "index.html"
    if not index.is_file():  # pragma: no cover - نصب ناقص (web_root فقط با index.html معتبر است)
        return _error("web UI is installed but index.html is missing", "ui_broken", status=500)
    return web.FileResponse(index, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
async def handle_websocket(request: web.Request) -> web.StreamResponse:
    """``GET /ws`` — کانال زنده: رویدادها، تأییدها، اجرا و لغو.

    پیام‌های ورودی: ``run`` · ``approve`` · ``cancel`` · ``reset`` · ``update`` · ``ping``
    پیام‌های خروجی: ``hello`` · ``result`` · ``event`` · ``approval_request`` · ``ack`` · ``error`` · ``pong``
    """
    auth: TokenAuthorizer = request.app[APP_AUTH]
    if not auth.is_allowed(request):
        return unauthorized()
    session_id = session_id_of(request)
    try:
        limit = int(request.app[APP_CONFIG].server_max_body_bytes)
    except (TypeError, ValueError):  # pragma: no cover - config همیشه معتبر است
        limit = 2 * 1024 * 1024
    socket = web.WebSocketResponse(heartbeat=30.0, max_msg_size=limit, compress=False)
    await socket.prepare(request)
    session = store(request).get(session_id)
    queue = session.subscribe()
    stats = request.app[APP_STATS]
    stats.sockets += 1
    peer = request.remote or "?"
    logger.info("ws connected: session=%s peer=%s", session.id, peer)
    try:
        await socket.send_json(
            {
                "kind": "hello",
                "payload": {
                    "version": __version__,
                    "session": session.describe(),
                    "pending_approvals": session.approvals.pending,
                    "recent_events": session.recent_events(limit=20),
                    "ready": agent_readiness(request)[0],
                    "ready_hint": agent_readiness(request)[1] or None,
                },
            }
        )
        writer = asyncio.create_task(_ws_writer(socket, queue))
        async for raw in socket:
            if raw.type in {web.WSMsgType.ERROR, web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED}:
                break
            if raw.data is None:
                continue
            message = _parse_ws_message(raw.data)
            if message is None:
                await socket.send_json({"kind": "error", "payload": {"error": "expected a JSON object with 'kind'"}})
                continue
            await _handle_ws_message(request, socket, session, message)
        writer.cancel()
        with contextlib_suppress_cancelled():
            await writer
    finally:
        session.unsubscribe(queue)
        stats.sockets = max(0, stats.sockets - 1)
        # وقتی تنها کلاینت می‌رود، تأییدهای باز را ببند تا ایجنت معلق نماند
        if not session.subscribers:
            session.approvals.cancel_all(reason="no client connected")
        logger.info("ws closed: session=%s peer=%s", session.id, peer)
    return socket


def contextlib_suppress_cancelled() -> Any:
    """``contextlib.suppress(asyncio.CancelledError)`` با import محلی (خوانایی)."""
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)


def _parse_ws_message(raw: Any) -> dict[str, Any] | None:
    """تجزیه‌ی پیام متنی WS؛ هر چیز غیر از آبجکت JSON → None."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - ایمنی
            return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def _ws_writer(socket: web.WebSocketResponse, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """جریان یک‌طرفه‌ی رویدادها به سمت کلاینت."""
    while True:
        message = await queue.get()
        if socket.closed:
            return
        await socket.send_json(message)


async def _handle_ws_message(
    request: web.Request, socket: web.WebSocketResponse, session: AgentSession, message: dict[str, Any]
) -> None:
    """مدیریت یک پیام WS."""
    try:
        envelope = WsMessage(**message)
    except PydanticValidationError as exc:
        await socket.send_json(
            {"kind": "error", "payload": {"error": str(exc)[:400], "error_code": "invalid_message"}}
        )
        return
    kind, payload = envelope.kind.lower(), dict(envelope.payload or {})
    if kind == "ping":
        await socket.send_json({"kind": "pong", "payload": {"at": time.time()}})
        return
    if kind == "subscribe":
        await socket.send_json(
            {
                "kind": "ack",
                "payload": {"subscribed": True, "session": session.describe(), "pending": session.approvals.pending},
            }
        )
        return
    if kind == "cancel":
        await socket.send_json({"kind": "ack", "payload": {"cancelled": session.stop()}})
        return
    if kind == "reset":
        session.reset()
        await socket.send_json({"kind": "ack", "payload": {"reset": session.id}})
        return
    if kind == "update":
        try:
            changed = session.apply_updates(SessionUpdate(**payload).updates())
        except (PydanticValidationError, KeyError) as exc:
            await socket.send_json(
                {
                    "kind": "error",
                    "payload": {"error": redact_secrets_message(str(exc))[:400], "error_code": "invalid_update"},
                }
            )
            return
        await socket.send_json({"kind": "ack", "payload": {"changed": changed, "session": session.describe()}})
        return
    if kind == "approve":
        try:
            decision = ApprovalDecision(**payload)
        except PydanticValidationError as exc:
            await socket.send_json(
                {"kind": "error", "payload": {"error": str(exc)[:300], "error_code": "invalid_approval"}}
            )
            return
        resolved = session.approvals.resolve(decision.request_id, decision.approved, reason=decision.reason)
        if resolved:
            stats = request.app[APP_STATS]
            stats.approved += int(decision.approved)
            stats.denied += int(not decision.approved)
        await socket.send_json({"kind": "ack", "payload": {"resolved": decision.request_id, "ok": resolved}})
        return
    if kind in {"run", "ask", "prompt"}:
        ready, hint = agent_readiness(request)
        if not ready:
            await socket.send_json({"kind": "error", "payload": {"error": hint, "error_code": "missing_api_key"}})
            return
        try:
            body = RunRequest(**{**payload, "session_id": session.id})
        except PydanticValidationError as exc:
            await socket.send_json(
                {"kind": "error", "payload": {"error": str(exc)[:300], "error_code": "invalid_request"}}
            )
            return
        await socket.send_json(
            {"kind": "accepted", "payload": {"prompt": truncate_text(body.prompt, 200)[0], "session_id": session.id}}
        )
        try:
            result = await session.run(body.prompt, timeout=float(body.timeout or 900))
        except Exception as exc:  # noqa: BLE001 - خطا باید به اپ برسد، نه اینکه WS بیفتد
            await socket.send_json(
                {
                    "kind": "error",
                    "payload": {"error": redact_secrets_message(str(exc))[:900], "error_code": "run_failed"},
                }
            )
            return
        request.app[APP_STATS].runs += 1
        await socket.send_json({"kind": "result", "payload": result})
        return
    await socket.send_json(
        {"kind": "error", "payload": {"error": f"unknown kind '{kind}'", "error_code": "unknown_kind"}}
    )


# ---------------------------------------------------------------------------
# ساخت اپ
# ---------------------------------------------------------------------------
def create_app(config: Config | None = None, *, keystore: KeyStore | None = None) -> web.Application:
    """ساخت :class:`aiohttp.web.Application` آماده‌ی اجرا.

    Args:
        config: تنظیمات (پیش‌فرض :func:`src.config.get_config`).
        keystore: مخزن کلید (برای تست قابل تزریق است).

    Raises:
        RuntimeError: اگر سرور روی شبکه باز باشد و توکن تنظیم نشده باشد.
    """
    settings = config or get_config()
    auth = TokenAuthorizer.from_config(settings)
    if settings.server_is_open and not auth.enabled:
        raise RuntimeError(
            f"refusing to bind the agent server to {settings.server_host}:{settings.server_port} without SERVER_TOKEN. "
            "Set SERVER_TOKEN (a long random string) or use SERVER_HOST=127.0.0.1 for local-only access."
        )
    app = web.Application(
        middlewares=[errors_middleware, security_middleware],
        client_max_size=int(settings.server_max_body_bytes),
    )
    app[APP_CONFIG] = settings
    app[APP_AUTH] = auth
    app[APP_LIMITER] = RateLimiter.from_config(settings)
    app[APP_KEYSTORE] = (keystore or KeyStore(settings.keystore_path)).load()
    app[APP_SESSIONS] = SessionRegistry(settings, keystore=app[APP_KEYSTORE])
    app[APP_STARTED_AT] = time.time()
    app[APP_STATS] = ServerStats()
    # زیرسیستم‌های Agent-OS. ساختنشان سبک است (بدون شبکه و بدون حلقه)؛
    # اتصال MCP و شروع زمان‌بند در ``on_startup`` انجام می‌شود.
    from src.core.services import ServiceHub

    hub = ServiceHub(settings, bus=None)
    app[APP_SERVICES] = hub

    async def _start_hub(_app: web.Application) -> None:
        registry = _app[APP_SESSIONS]
        runner = getattr(registry, "run_prompt", None)
        summary = await hub.start(agent_runner=runner)
        logger.info(
            "agent-os services ready · scheduler=%s · skills=%d · mcp=%d",
            summary.get("scheduler"),
            summary.get("skills", 0),
            summary.get("mcp_tools", 0),
        )

    app.on_startup.append(_start_hub)
    app.on_cleanup.append(lambda _app: hub.stop())
    app.on_cleanup.append(lambda _app: registry_for(_app).aclose())

    discover_tools()
    ToolRegistry.set_default_config(settings)

    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/safety", handle_safety)
    app.router.add_get("/api/tools", handle_tools)
    app.router.add_get("/api/tools/{name}", handle_tool_schema)
    app.router.add_post("/api/tools/{name}/invoke", handle_tool_invoke)
    app.router.add_get("/api/profiles", handle_profiles)
    app.router.add_get("/api/reports", handle_reports)
    app.router.add_get("/api/memory", handle_memory_list)
    app.router.add_post("/api/memory", handle_memory_write)
    app.router.add_delete("/api/memory/{id}", handle_memory_forget)
    app.router.add_post("/api/run", handle_run)
    app.router.add_post("/api/run/stop", handle_run_stop)
    app.router.add_get("/api/sessions", handle_sessions)
    app.router.add_post("/api/sessions", handle_create_session)
    app.router.add_get("/api/sessions/{id}", handle_get_session)
    app.router.add_patch("/api/sessions/{id}", handle_patch_session)
    app.router.add_delete("/api/sessions/{id}", handle_delete_session)
    app.router.add_post("/api/sessions/{id}/reset", handle_reset_session)
    app.router.add_get("/api/sessions/{id}/history", handle_history)
    app.router.add_get("/api/sessions/{id}/events", handle_events)
    app.router.add_get("/api/sessions/{id}/approvals", handle_list_approvals)
    app.router.add_post("/api/sessions/{id}/approvals", handle_resolve_approval)
    app.router.add_get("/api/keys", handle_list_keys)
    app.router.add_post("/api/keys", handle_put_key)
    app.router.add_delete("/api/keys/{name}", handle_delete_key)
    app.router.add_post("/api/keys/{name}/activate", handle_activate_key)
    app.router.add_post("/api/keys/import-env", handle_import_env)
    app.router.add_get("/ws", handle_websocket)

    from src.server.agent_os import register_agent_os_routes

    register_agent_os_routes(app)

    app.router.add_get("/", handle_ui_root)
    root = settings.web_root
    if root is not None:
        for name in ("styles.css", "app.js", "manifest.webmanifest", "sw.js", "icon.svg", "offline.html"):
            if (root / name).is_file():
                app.router.add_get(f"/{name}", _static_handler(root / name))
        icons = root / "icons"
        if icons.is_dir():
            app.router.add_static("/icons", str(icons), show_index=False)
    app.router.add_route("*", "/{tail:.*}", handle_not_found)

    logger.info(
        "server app ready · %d tools · auth=%s · ui=%s",
        len(ToolRegistry.names()),
        "token" if auth.enabled else "loopback-only",
        "yes" if root is not None else "no",
    )
    return app


async def handle_not_found(request: web.Request) -> web.StreamResponse:
    """مسیرهای ناآشنا: ۴۰۴ JSON برای API، و صفحه‌ی UI برای بقیه (SPA fallback)."""
    if request.path.startswith("/api/"):
        return _error(f"no such endpoint: {request.method} {request.path}", "no_route", status=404)
    root = config_of(request).web_root
    if root is not None and (root / "index.html").is_file():
        return web.FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
    return _error("not found", "not_found", status=404)


def _static_handler(path: Path) -> Any:
    """ساخت هندلر برای یک فایل استاتیک مشخص (با content-type حدسی)."""
    suffix_types = {
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".webmanifest": "application/manifest+json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
    }

    async def _serve(_request: web.Request) -> web.StreamResponse:
        """ارسال فایل با no-cache (تا تغییرات UI فوری دیده شود)."""
        if not path.is_file():  # noqa: ASYNC240 - stat یک فایل کوچک، داخل event loop مجاز است
            return _error(f"missing asset {path.name}", "not_found", status=404)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": suffix_types.get(path.suffix, "application/octet-stream"),
                "Cache-Control": "no-cache",
            },
        )

    return _serve


def is_request_local(request: web.Request) -> bool:
    """آیا درخواست از همین ماشین آمده؟ (برای پیام‌های راهنما)."""
    return is_loopback_address(request.remote)


__all__ = [
    "APP_AUTH",
    "APP_CONFIG",
    "APP_KEYSTORE",
    "APP_LIMITER",
    "APP_SESSIONS",
    "APP_STATS",
    "ServerStats",
    "authorizer_for",
    "config_of",
    "create_app",
    "handle_health",
    "handle_run",
    "handle_status",
    "handle_tools",
    "handle_websocket",
    "is_request_local",
    "key_store",
    "security_middleware",
    "store",
]

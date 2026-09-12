"""اندپوینت‌های REST زیرسیستم‌های Agent-OS.

این ماژول عمداً از ``app.py`` جدا است: ``app.py`` لایه‌ی «ایجنت و session» است و
اینجا لایه‌ی «سیستم‌عامل ایجنت» (روتین‌ها، اعلان‌ها، skillها، MCP، بکاپ،
ممیزی، سلامت). هر دو از یک ``ServiceHub`` مشترک در state اپ می‌خوانند.

جدول اندپوینت‌ها
----------------
============================== ====== =========================================
مسیر                           متد    کار
============================== ====== =========================================
``/api/routines``              GET    فهرست روتین‌ها + آمار زمان‌بند
``/api/routines``              POST   ساخت روتین
``/api/routines/{id}``         PATCH  به‌روزرسانی / فعال-غیرفعال
``/api/routines/{id}``         DELETE حذف روتین
``/api/routines/{id}/history`` GET    تاریخچه‌ی اجراها
``/api/notifications``         GET    فید اعلان‌ها
``/api/notifications/read``    POST   خوانده‌شده کردن (یکی یا همه)
``/api/notifications``         DELETE پاک کردن (خوانده‌شده‌ها یا همه)
``/api/skills``                GET    فهرست skillها
``/api/skills/{name}``         GET    بدنه‌ی یک skill
``/api/mcp``                   GET    سرورهای MCP و ابزارهایشان
``/api/backup``                GET    فهرست بکاپ‌ها
``/api/backup``                POST   ساخت بکاپ
``/api/backup/{name}/restore`` POST   بازیابی (**نیازمند ``confirm: true``**)
``/api/backup/{name}``         DELETE حذف بکاپ
``/api/audit``                 GET    رکوردهای ممیزی + سلامت زنجیره
``/api/diagnostics``           GET    سلامت یکپارچه‌ی همه‌ی زیرسیستم‌ها
``/hooks/{token}``             POST   تریگر webhook یک روتین
============================== ====== =========================================

نکته‌ی امنی درباره‌ی ``/hooks/{token}``
---------------------------------------
این تنها مسیری است که *بیرون* از ``/api/`` است، بنابراین middleware احراز توکن
روی آن اعمال نمی‌شود. احراز هویتش همان توکن غیرقابل‌حدسِ خودِ روتین است
(``Routine.expression``) — چون قرار است سیستم بیرونی (مثل GitHub) بدون داشتن
توکن سرور بتواند ایجنت را بیدار کند. اگر این را نمی‌پسندید، روتین webhook
نسازید؛ هیچ مسیر دیگری باز نیست.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from src.core.services import ServiceHub
from src.server.app import APP_SERVICES, _error, _json
from src.utils.logger import get_logger

__all__ = ["register_agent_os_routes", "services_of"]

logger = get_logger("server.agent_os")


def services_of(request: web.Request) -> ServiceHub:
    """``ServiceHub`` مشترک اپ.

    Raises:
        RuntimeError: اگر hub در ``create_app`` ثبت نشده باشد (خطای برنامه‌نویسی).
    """
    hub = request.app.get(APP_SERVICES)
    if hub is None:  # pragma: no cover - create_app همیشه ثبت می‌کند
        raise RuntimeError("ServiceHub is not registered on this application")
    return hub


async def _body(request: web.Request) -> dict[str, Any]:
    """بدنه‌ی JSON یا دیکشنری خالی (بدنه‌ی خالی مجاز است)."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - بدنه‌ی غیر-JSON یا خالی
        return {}
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------- routines
async def handle_routines_list(request: web.Request) -> web.Response:
    """``GET /api/routines`` — روتین‌ها، وضعیت زمان‌بند و تاریخچه‌ی کوتاه."""
    return _json(services_of(request).scheduler.describe())


async def handle_routine_create(request: web.Request) -> web.Response:
    """``POST /api/routines`` — ساخت روتین.

    بدنه::

        {"name": "nightly", "trigger": "cron", "expression": "0 22 * * *",
         "prompt": "summarise today", "profile": "read_only"}
    """
    from src.core.routines import Routine

    payload = await _body(request)
    try:
        routine = Routine.model_validate(payload)
        services_of(request).scheduler.add(routine)
    except ValueError as exc:
        return _error(str(exc), "invalid_routine", status=400)
    return _json(routine.model_dump(mode="json"), status=201)


async def handle_routine_update(request: web.Request) -> web.Response:
    """``PATCH /api/routines/{id}`` — به‌روزرسانی فیلدهای مجاز."""
    routine_id = request.match_info.get("id", "")
    payload = await _body(request)
    try:
        routine = services_of(request).scheduler.update(routine_id, **payload)
    except ValueError as exc:
        return _error(str(exc), "invalid_routine", status=400)
    if routine is None:
        return _error(f"routine '{routine_id}' not found", "not_found", status=404)
    return _json(routine.model_dump(mode="json"))


async def handle_routine_delete(request: web.Request) -> web.Response:
    """``DELETE /api/routines/{id}``."""
    routine_id = request.match_info.get("id", "")
    if not services_of(request).scheduler.remove(routine_id):
        return _error(f"routine '{routine_id}' not found", "not_found", status=404)
    return _json({"deleted": routine_id})


async def handle_routine_history(request: web.Request) -> web.Response:
    """``GET /api/routines/{id}/history``."""
    hub = services_of(request)
    routine_id = request.match_info.get("id", "")
    if hub.scheduler.get(routine_id) is None:
        return _error(f"routine '{routine_id}' not found", "not_found", status=404)
    limit = min(200, max(1, int(request.query.get("limit", "20") or 20)))
    return _json({"runs": [run.as_dict() for run in hub.scheduler.history(routine_id, limit=limit)]})


# ---------------------------------------------------------------- notifications
async def handle_notifications_list(request: web.Request) -> web.Response:
    """``GET /api/notifications`` — فید، با فیلتر ``unread``/``kind``/``severity``."""
    center = services_of(request).notifications
    if center is None:
        return _json({"notifications": [], "stats": {"total": 0, "unread": 0}, "disabled": True})
    limit = min(200, max(1, int(request.query.get("limit", "50") or 50)))
    unread_only = request.query.get("unread", "").lower() in {"1", "true", "yes"}
    items = center.list(
        unread_only=unread_only,
        kind=request.query.get("kind") or None,
        severity=request.query.get("severity") or None,
        limit=limit,
    )
    return _json({"notifications": [note.as_dict() for note in items], "stats": center.stats()})


async def handle_notifications_read(request: web.Request) -> web.Response:
    """``POST /api/notifications/read`` — با ``{"id": …}`` یا ``{"all": true}``."""
    center = services_of(request).notifications
    if center is None:
        return _error("notifications are disabled", "disabled", status=409)
    payload = await _body(request)
    if payload.get("all"):
        return _json({"marked": center.mark_all_read()})
    note_id = str(payload.get("id") or "").strip()
    if not note_id:
        return _error("provide 'id' or {'all': true}", "invalid_input", status=400)
    if not center.mark_read(note_id):
        return _error(f"notification '{note_id}' not found or already read", "not_found", status=404)
    return _json({"marked": 1})


async def handle_notifications_clear(request: web.Request) -> web.Response:
    """``DELETE /api/notifications`` — با ``?read_only=1`` فقط خوانده‌شده‌ها."""
    center = services_of(request).notifications
    if center is None:
        return _error("notifications are disabled", "disabled", status=409)
    read_only = request.query.get("read_only", "").lower() in {"1", "true", "yes"}
    return _json({"cleared": center.clear(read_only=read_only)})


# ----------------------------------------------------------------------- skills
async def handle_skills_list(request: web.Request) -> web.Response:
    """``GET /api/skills``."""
    return _json(services_of(request).skills.describe())


async def handle_skill_detail(request: web.Request) -> web.Response:
    """``GET /api/skills/{name}`` — متادیتا + بدنه."""
    library = services_of(request).skills
    name = request.match_info.get("name", "")
    skill = library.get(name)
    if skill is None:
        return _error(f"skill '{name}' not found", "not_found", status=404)
    return _json({**skill.as_dict(), "instructions": skill.body})


# -------------------------------------------------------------------------- mcp
async def handle_mcp_list(request: web.Request) -> web.Response:
    """``GET /api/mcp`` — سرورهای متصل و ابزارهای راه دورشان."""
    hub = services_of(request)
    clients = [client.describe() for client in hub.mcp_clients]
    return _json({"servers": clients, "tools": hub.mcp_tools(), "count": len(hub.mcp_tools())})


# ----------------------------------------------------------------------- backup
async def handle_backup_list(request: web.Request) -> web.Response:
    """``GET /api/backup``."""
    return _json(services_of(request).backup.describe())


async def handle_backup_create(request: web.Request) -> web.Response:
    """``POST /api/backup`` — بدنه‌ی اختیاری ``{"note": …, "include_keys": false}``."""
    hub = services_of(request)
    payload = await _body(request)
    try:
        info = hub.backup.create(
            note=str(payload.get("note") or ""),
            include_keys=bool(payload.get("include_keys", False)),
        )
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), "backup_failed", status=400)
    if hub.audit is not None:
        hub.audit.record("user", "backup.create", info.name, "allowed", bytes=info.bytes)
    return _json(info.as_dict(), status=201)


async def handle_backup_restore(request: web.Request) -> web.Response:
    """``POST /api/backup/{name}/restore`` — **نیازمند ``{"confirm": true}``**.

    بدون ``confirm`` فقط پیش‌نمایش (dry-run) برمی‌گردد؛ این دقیقاً همان الگوی
    «اول ببین، بعد انجام بده» است که بقیه‌ی پروژه دارد.
    """
    hub = services_of(request)
    name = request.match_info.get("name", "")
    payload = await _body(request)
    only = payload.get("only")
    only_list = [str(item) for item in only] if isinstance(only, list) else None
    confirm = bool(payload.get("confirm", False))
    try:
        result = hub.backup.restore(name, confirm=confirm, only=only_list)
    except (ValueError, RuntimeError) as exc:
        return _error(str(exc), "restore_failed", status=400)
    if not result.get("dry_run", False) and hub.audit is not None:
        hub.audit.record("user", "backup.restore", name, "confirmed", files=len(result["restored"]))
    return _json(result)


async def handle_backup_delete(request: web.Request) -> web.Response:
    """``DELETE /api/backup/{name}``."""
    if not services_of(request).backup.remove(request.match_info.get("name", "")):
        return _error("backup not found", "not_found", status=404)
    return _json({"deleted": request.match_info.get("name", "")})


# ------------------------------------------------------------------------ audit
async def handle_audit_list(request: web.Request) -> web.Response:
    """``GET /api/audit`` — آخرین رکوردها + نتیجه‌ی ``verify_chain``."""
    hub = services_of(request)
    if hub.audit is None:
        return _json({"entries": [], "chain": {"ok": True, "entries": 0}, "disabled": True})
    limit = min(500, max(1, int(request.query.get("limit", "100") or 100)))
    entries = hub.audit.query(
        actor=request.query.get("actor") or None,
        action=request.query.get("action") or None,
        limit=limit,
    )
    return _json(
        {
            "entries": [entry.model_dump(mode="json") for entry in entries],
            "chain": hub.audit.verify_chain().as_dict(),
            "stats": hub.audit.stats(),
        }
    )


# ------------------------------------------------------------------ diagnostics
async def handle_diagnostics(request: web.Request) -> web.Response:
    """``GET /api/diagnostics`` — سلامت یکپارچه‌ی همه‌ی زیرسیستم‌ها."""
    return _json(services_of(request).diagnostics())


# ---------------------------------------------------------------------- webhook
async def handle_webhook(request: web.Request) -> web.Response:
    """``POST /hooks/{token}`` — بیدار کردن یک روتین webhook.

    احراز هویت = خودِ توکن (خارج از ``/api/`` است؛ به docstring ماژول نگاه کنید).
    """
    hub = services_of(request)
    token = request.match_info.get("token", "")
    if not token:
        return _error("missing webhook token", "invalid_input", status=400)
    payload = await _body(request)
    run = await hub.scheduler.fire_webhook(token, payload=payload or None)
    if run is None:
        return _error("unknown or disabled webhook token", "not_found", status=404)
    if hub.audit is not None:
        hub.audit.record(
            "webhook", "routine.webhook", token, "allowed" if run.status == "ok" else "failed", run_id=run.id
        )
    return _json(run.as_dict(), status=200 if run.status == "ok" else 500)


def register_agent_os_routes(app: web.Application) -> None:
    """همه‌ی مسیرهای Agent-OS را روی اپ ثبت می‌کند."""
    app.router.add_get("/api/routines", handle_routines_list)
    app.router.add_post("/api/routines", handle_routine_create)
    app.router.add_patch("/api/routines/{id}", handle_routine_update)
    app.router.add_delete("/api/routines/{id}", handle_routine_delete)
    app.router.add_get("/api/routines/{id}/history", handle_routine_history)
    app.router.add_get("/api/notifications", handle_notifications_list)
    app.router.add_post("/api/notifications/read", handle_notifications_read)
    app.router.add_delete("/api/notifications", handle_notifications_clear)
    app.router.add_get("/api/skills", handle_skills_list)
    app.router.add_get("/api/skills/{name}", handle_skill_detail)
    app.router.add_get("/api/mcp", handle_mcp_list)
    app.router.add_get("/api/backup", handle_backup_list)
    app.router.add_post("/api/backup", handle_backup_create)
    app.router.add_post("/api/backup/{name}/restore", handle_backup_restore)
    app.router.add_delete("/api/backup/{name}", handle_backup_delete)
    app.router.add_get("/api/audit", handle_audit_list)
    app.router.add_get("/api/diagnostics", handle_diagnostics)
    app.router.add_post("/hooks/{token}", handle_webhook)

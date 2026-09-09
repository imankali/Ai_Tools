"""لایه‌ی سرور Universal Agent Hub (REST + WebSocket).

این بسته «هسته‌ی ایجنت» را برای اپ‌ها قابل استفاده می‌کند:

* **اندروید / iOS (PWA و WebView)** — آدرس سرور + توکن را وارد می‌کنید و گوشی
  تبدیل به کنترل‌کننده‌ی ایجنت روی کامپیوتر می‌شود؛
* **دسکتاپ (ویندوز/macOS/لینوکس)** — همان UI داخل مرورگر یا پنجره‌ی ``desktop``؛
* **Termux (اندروید)** — سرور مستقیم روی گوشی اجرا می‌شود (``SERVER_HOST=127.0.0.1``).

مسئولیت‌ها:

* :mod:`src.server.app` — مسیرهای HTTP، WebSocket و استاتیک UI
* :mod:`src.server.auth` — احراز توکن و محدودساز نرخ
* :mod:`src.server.sessions` — session های ایجنت، صف اجرا و پل تأیید
* :mod:`src.server.keystore` — پروفایل‌های کلید API روی دستگاه کاربر
* :mod:`src.server.protocol` — مدل‌های ورودی/خروجی (همان قرارداد مستند در ``docs/server_api.md``)
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from src.config import Config, get_config
from src.server.app import create_app
from src.server.sessions import AgentSession, ApprovalBroker, SessionRegistry

__all__ = ["AgentSession", "ApprovalBroker", "SessionRegistry", "create_app", "run_server", "serve_forever"]


def build_app(config: Config | None = None) -> web.Application:
    """ساخت اپلیکیشن aiohttp (بدون اجرای سرور) — مناسب تست و ادغام."""
    return create_app(config or get_config())


def run_server(config: Config | None = None, *, host: str | None = None, port: int | None = None) -> None:
    """اجرای بلاک‌کننده‌ی سرور (میان‌بر برای اسکریپت و ``python -m src.server``).

    Args:
        config: تنظیمات؛ پیش‌فرض :func:`src.config.get_config`.
        host: بازنشانی آدرس bind (پیش‌فرض ``SERVER_HOST``).
        port: بازنشانی پورت (پیش‌فرض ``SERVER_PORT``).
    """
    settings = config or get_config()
    app = create_app(settings)
    web.run_app(
        app, host=host or settings.server_host, port=int(port or settings.server_port), print=None, access_log=None
    )


async def serve_forever(config: Config | None = None, *, host: str | None = None, port: int | None = None) -> None:
    """اجرای async سرور روی loop جاری (برای ادغام با اپلیکیشن‌های دیگر)."""
    settings = config or get_config()
    runner = web.AppRunner(create_app(settings))
    await runner.setup()
    site = web.TCPSite(runner, host or settings.server_host, int(port or settings.server_port))
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()

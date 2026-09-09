"""ابزارهای سیستم (System tools) — هر ابزار یک فایل مستقل است.

import کردن این ماژول باعث می‌شود همه‌ی ابزارها از طریق decorator
``@register_tool`` در :class:`src.core.tool_registry.ToolRegistry` ثبت شوند.
برای افزودن ابزار جدید کافی است یک فایل بسازید (رجیستری به‌صورت خودکار
ماژول‌های این پکیج را کشف می‌کند)؛ الگوی کامل در :mod:`src.tools.template`.

میزان ریسک هر ابزار:

===================  ======================  =======================
ابزار                دسته                    تأیید کاربر
===================  ======================  =======================
``terminal_run``     developer               فقط برای دستورهای پرخطر
``read_file``        filesystem              ندارد
``write_file``       filesystem              دارد (نوشتن روی دیسک)
``list_directory``   filesystem              ندارد
``delete_file``      filesystem              همیشه
``move_file``        filesystem              همیشه
``search_files``     filesystem              ندارد
``browser_browse``   browser                 برای فرم‌ها
``browser_screenshot``  browser              ندارد
``browser_extract_text``  browser            ندارد
``browser_click``    browser                 ندارد
``browser_fill_form``  browser               همیشه
``web_search``       network                 ندارد
``search_advanced``  network                 ندارد
``os_info`` …        system                  ندارد
===================  ======================  =======================
"""

from __future__ import annotations

from src.core.tool_registry import ToolRegistry

#: فهرست ماژول‌های استاندارد ابزارها (برای import صریح و discovery)
TOOL_MODULES: tuple[str, ...] = (
    "terminal",
    "filesystem",
    "browser",
    "web_search",
    "system_info",
)

__all__ = ["TOOL_MODULES", "installed_tools", "register_builtin_tools"]


def register_builtin_tools(*, force: bool = False) -> list[str]:
    """اطمینان از ثبت همه‌ی ابزارهای داخلی.

    Args:
        force: کشف دوباره حتی اگر قبلاً انجام شده باشد.

    Returns:
        فهرست نام ابزارهای ثبت‌شده.
    """
    return ToolRegistry.discover("src.tools", force=force)


def installed_tools() -> dict[str, str]:
    """نگاشت ``نام ابزار → نام ماژول`` (برای عیب‌یابی)."""
    register_builtin_tools()
    return {name: ToolRegistry.classes()[name].__module__ for name in ToolRegistry.names()}

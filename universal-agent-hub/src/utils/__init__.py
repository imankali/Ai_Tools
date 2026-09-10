"""لایه‌ی کمکی (Utilities): لاگ، ایمنی، اعتبارسنجی و توابع سبک.

این ماژول‌ها به هسته وابسته نیستند (به‌جز ``safety`` که مدل‌ها را import می‌کند)
تا بتوان آن‌ها را مستقل تست کرد و در ابزارهای سفارشی استفاده کرد.
"""

from __future__ import annotations

from src.utils.helpers import format_size, now_iso, run_in_thread, truncate_text
from src.utils.logger import get_logger, setup_logging
from src.utils.safety import SafetyDecision, SafetyGuard
from src.utils.validators import ValidationError, bounded_int, clean_text, is_valid_url

__all__ = [
    "SafetyDecision",
    "SafetyGuard",
    "ValidationError",
    "bounded_int",
    "clean_text",
    "format_size",
    "get_logger",
    "is_valid_url",
    "now_iso",
    "run_in_thread",
    "setup_logging",
    "truncate_text",
]

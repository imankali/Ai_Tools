"""Setup script (legacy entry point).

The authoritative metadata lives in :mod:`pyproject.toml`; this file exists so
that ``python setup.py --version``, ``pip install -e .`` on old toolchains and
some CI images keep working.

Do not add dependencies here — edit ``pyproject.toml`` and keep
``requirements*.txt`` in sync.
"""

from __future__ import annotations

from pathlib import Path

from setuptools import setup

HERE = Path(__file__).resolve().parent


def _read_requirements(filename: str) -> list[str]:
    """خواندن فهرست وابستگی‌ها از یک فایل requirements.

    Args:
        filename: نام فایل نسبت به ریشه‌ی پروژه.

    Returns:
        فهرست خط‌های غیرخالی و غیرکامنتی.
    """
    path = HERE / filename
    if not path.is_file():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        lines.append(line)
    return lines


def _long_description() -> str:
    """توضیح طولانی از README (در صورت نبود، رشته‌ی خالی)."""
    readme = HERE / "README.md"
    return readme.read_text(encoding="utf-8") if readme.is_file() else ""


setup(
    name="universal-agent-hub",
    version="1.0.0",
    description="A modular, safety-guarded AI agent with configurable full system access",
    long_description=_long_description(),
    long_description_content_type="text/markdown",
    license="MIT",
    packages=["src", "src.core", "src.tools", "src.models", "src.utils"],
    package_data={"src": ["py.typed"]},
    python_requires=">=3.10",
    # The dependency list is kept in pyproject.toml only (PEP 621). This shim
    # deliberately mirrors just the runtime extras so that `requirements.txt`
    # (which also carries dev tooling) is never installed as a hard dependency.
    install_requires=[
        "openai>=1.40",
        "pydantic>=2.5",
        "pydantic-settings>=2.1",
        "python-dotenv>=1.0",
        "rich>=13.0",
        "aiohttp>=3.9",
        "requests>=2.31",
    ],
    extras_require={
        "system": ["psutil>=5.9"],
        "search": ["ddgs>=9.0"],
        "browser": ["playwright>=1.40"],
        "all": ["psutil>=5.9", "ddgs>=9.0", "playwright>=1.40"],
    },
    entry_points={"console_scripts": ["agent-hub = src.cli:main"]},
    zip_safe=False,
)

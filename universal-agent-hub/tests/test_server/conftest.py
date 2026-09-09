"""fixtures مشترک تست سرور.

هیچ تستی در این پوشه نباید به شبکه بیرونی، کلید واقعی یا فایل‌های home کاربر
دست بزند: keystore و UI همگی به پوشه‌ی موقت تبدیل می‌شوند و client ایجنت
با stub جایگزین می‌شود.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.config import Config
from src.server import create_app
from src.server.keystore import KeyStore
from src.server.sessions import AgentSession, SessionRegistry

__all__ = ["read_json", "server_config", "start_client", "stub_invoke"]


def server_config(base: Config, **overrides: Any) -> Config:
    """تنظیمات تست سرور روی پایه‌ی fixture ``config``.

    ``static_dir`` به پوشه‌ی ``web`` پروژه اشاره می‌کند تا مسیرهای UI هم واقعی
    تست شوند؛ keystore داخل tmp_path می‌نشیند.
    """
    updates: dict[str, Any] = {
        "server_enabled": True,
        "server_host": "127.0.0.1",
        "server_port": 0,
        "server_token": "",
        "server_keystore": str(base.project_root / "keys.json"),
        "server_rate_limit_per_minute": 1000,
        "server_approval_timeout": 10,
        "server_session_ttl": 60,
        "server_allow_direct_tools": True,
    }
    updates.update(overrides)
    return base.model_copy(update=updates)


@pytest.fixture
def app_config(config: Config) -> Config:
    """تنظیمات پیش‌فرض سرور در تست‌ها (بدون توکن، loopback)."""
    return server_config(config)


@pytest.fixture
def app(app_config: Config) -> web.Application:
    """اپلیکیشن aiohttp آماده (بدون اجرا)."""
    return create_app(app_config)


@pytest.fixture
async def client(app: web.Application) -> AsyncIterator[TestClient]:
    """کلاینت تستی روی سرور واقعی (پورت تصادفی localhost)."""
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


@pytest.fixture
def registry(app_config: Config) -> SessionRegistry:
    """رجیستری مستقل برای تست واحد sessions."""
    return SessionRegistry(app_config, ttl=60)


@pytest.fixture
def keystore(app_config: Config) -> KeyStore:
    """keystore تستی روی فایل موقت."""
    return KeyStore(app_config.keystore_path).load()


@pytest.fixture
async def session_client(client: TestClient) -> Iterator[TestClient]:
    """همان client، ولی با یک session از پیش ساخته‌شده (برای تست‌های متداول)."""
    response = await client.post("/api/sessions", json={})
    assert response.status == 201
    data = await response.json()
    client.session.headers.update({"X-Agent-Session": data["session_id"]})
    yield client


@pytest.fixture
def sid(session_client: TestClient) -> str:
    """شناسه‌ی session ساخته‌شده توسط fixture ``session_client`` (برای مسیرها)."""
    return str(session_client.session.headers["X-Agent-Session"])


def stub_invoke(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> dict[str, Any]:
    """جایگزینی اجرای واقعی ایجنت با یک خروجی آماده."""
    captured: list[str] = []
    state: dict[str, Any] = {"payload": payload, "captured": captured}

    async def fake_invoke(self: AgentSession, text: str) -> dict[str, Any]:
        captured.append(text)
        return {**state["payload"], "session_id": self.id}

    monkeypatch.setattr(AgentSession, "_invoke", fake_invoke)
    return state  # {'payload':…, 'captured': [prompts]}


async def start_client(config: Config, **server_overrides: Any) -> TestClient:
    """راه‌اندازی سرور با config دلخواه (هر تست keystore مستقل خودش را دارد).

    فراخوان مسئول بستن است: ``await client.close()``.
    """
    merged = config.model_copy(update=dict(server_overrides))
    if "server_keystore" not in server_overrides:
        suffix = abs(hash(tuple(sorted(server_overrides.items())))) % 1_000_000
        merged = merged.model_copy(update={"server_keystore": str(config.project_root / f"keys-{suffix}.json")})
    client = TestClient(TestServer(create_app(merged)))
    await client.start_server()
    return client


async def read_json(response: web.Response) -> dict[str, Any]:
    """خواندن body پاسخ تستی به‌صورت dict."""
    text = await response.text()
    return json.loads(text) if text else {}

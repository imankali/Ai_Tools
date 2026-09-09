"""تست فلگ‌های ``--serve`` در CLI و هلپرهای ``python -m src.server``."""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from src import cli as cli_module
from src.cli import _local_addresses, _serve, build_parser  # noqa: PLC2701 - تست helper داخلی
from src.config import Config


class TestParser:
    """فلگ‌ها و مقادیر پیش‌فرض."""

    def test_serve_flags_exist(self) -> None:
        """فلگ‌های سرور ثبت شده‌اند."""
        args = build_parser().parse_args(["--serve", "--lan", "--port", "9000", "--token", "abc", "--no-ui"])
        assert args.serve is True and args.lan is True
        assert args.port == 9000 and args.token == "abc" and args.no_ui is True
        assert args.insecure is False

    def test_defaults_are_off(self) -> None:
        """هیچ‌کدام از فلگ‌های سرور پیش‌فرض فعال نیستند."""
        args = build_parser().parse_args([])
        assert args.serve is False and args.host is None and args.lan is False
        assert args.token is None and args.insecure is False and args.no_ui is False

    def test_local_addresses_returns_strs(self) -> None:
        """آدرس‌های محلی (ممکن است خالی باشد) ولی هرگز استثنا نمی‌دهند."""
        addresses = _local_addresses()
        assert isinstance(addresses, list)
        assert all(isinstance(item, str) and item for item in addresses)
        assert len(addresses) <= 4


class TestServe:
    """منطق آماده‌سازی سرور در ``_serve``."""

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # noqa: D401 - فقط ثبت ARG
        """جایگزینی ``run_server`` با یک ثبت‌کننده و خاموش‌کردن rich."""
        box: dict[str, Any] = {}
        import src.server as server_module

        def fake_run_server(settings: Config, *, host: str | None = None, port: int | None = None) -> None:
            box["settings"] = settings
            box["host"] = host or settings.server_host
            box["port"] = port or settings.server_port
            box["calls"] = int(box.get("calls", 0)) + 1

        monkeypatch.setattr(server_module, "run_server", fake_run_server)
        monkeypatch.setattr(cli_module, "Console", None)
        return box

    def test_loopback_needs_no_token(self, config: Config, captured: dict[str, Any]) -> None:
        """روی loopback توکن ساخته نمی‌شود (حالت توسعه)."""
        args = argparse.Namespace(
            serve=True, lan=False, host=None, port=None, token=None, insecure=False, no_ui=False
        )
        assert _serve(config, args) == 0
        settings = captured["settings"]
        assert settings.server_enabled is True
        assert settings.server_token == config.server_token
        assert settings.server_host == "127.0.0.1"
        assert captured["host"] == "127.0.0.1" and captured["calls"] == 1

    def test_lan_generates_token(self, config: Config, captured: dict[str, Any]) -> None:
        """``--lan`` بدون توکن → توکن تصادفی (نه باز بودن بی‌رمز)."""
        args = argparse.Namespace(serve=True, lan=True, host=None, port=None, token=None, insecure=False, no_ui=False)
        _serve(config, args)
        settings = captured["settings"]
        assert settings.server_host == "0.0.0.0"
        assert len(settings.server_token) >= 20
        assert settings.server_is_open is True

    def test_insecure_keeps_it_open_without_token(self, config: Config, captured: dict[str, Any]) -> None:
        """``--insecure`` مسئولیت را به کاربر می‌سپارد (توکن ساخته نمی‌شود)."""
        args = argparse.Namespace(serve=True, lan=True, host=None, port=None, token=None, insecure=True, no_ui=False)
        _serve(config, args)
        assert captured["settings"].server_token == ""

    def test_explicit_host_and_port(self, config: Config, captured: dict[str, Any]) -> None:
        """تغییر آدرس/پورت از CLI اعمال می‌شود."""
        args = argparse.Namespace(
            serve=True, lan=False, host="192.168.0.9", port=9137, token="t-123", insecure=False, no_ui=False
        )
        _serve(config, args)
        settings = captured["settings"]
        assert settings.server_host == "192.168.0.9"
        assert settings.server_port == 9137
        assert settings.server_token == "t-123"
        assert captured["port"] == 9137
        assert captured["settings"].server_url == "http://192.168.0.9:9137"

    def test_no_ui_clears_static_dir(self, config: Config, captured: dict[str, Any]) -> None:
        """``--no-ui`` فقط API می‌سازد (هر مسیر استاتیکی ثبت نمی‌شود)."""
        args = argparse.Namespace(serve=True, lan=False, host=None, port=None, token=None, insecure=False, no_ui=True)
        _serve(config, args)
        assert captured["settings"].server_static_dir == "off"
        assert captured["settings"].web_root is None

    def test_keyboard_interrupt_is_clean(self, config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ctrl+C روی سرور → کد خروج صفر (نه traceback)."""
        import src.server as server_module

        def interrupt(*_args: Any, **_kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(server_module, "run_server", interrupt)
        monkeypatch.setattr(cli_module, "Console", None)
        args = argparse.Namespace(
            serve=True, lan=False, host=None, port=None, token=None, insecure=False, no_ui=False
        )
        assert _serve(config, args) == 0


class TestModuleEntryPoint:
    """``python -m src.server``."""

    def test_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--version`` بدون ساخت سرور خارج می‌شود."""
        from src.server.__main__ import main

        assert main(["--version"]) == 0
        assert "universal-agent-hub server" in capsys.readouterr().out

    def test_open_bind_without_token_refused(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """bind باز + بدون توکن + بدون ``--insecure`` → کد خروج ۲ با پیام راهنما."""
        import os

        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("SERVER_TOKEN", "")
        monkeypatch.setenv("SERVER_HOST", "127.0.0.1")
        monkeypatch.chdir(tmp_path)
        from src.config import reset_config
        from src.server.__main__ import main

        reset_config()
        try:
            assert main(["--host", "0.0.0.0"]) == 2
            out = capsys.readouterr()
            assert "refusing to expose" in out.err
            assert "--print-token" in out.err
        finally:
            reset_config()
            for name in ("PROJECT_ROOT", "OPENAI_API_KEY", "SERVER_TOKEN", "SERVER_HOST"):
                os.environ.pop(name, None)

    def test_print_token_generates_one(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--print-token`` یک توکن تازه می‌سازد و به سرور می‌دهد."""
        import src.server as server_module
        from src.server.__main__ import main

        captured: dict[str, Any] = {}

        def fake_run_server(settings: Config, **_kwargs: Any) -> None:
            captured["token"] = settings.server_token
            captured["url"] = settings.server_url

        monkeypatch.setattr(server_module, "run_server", fake_run_server)
        code = main(["--print-token", "--port", "9411"])
        assert code == 0
        assert len(captured["token"]) >= 20
        assert captured["url"].endswith(":9411")
        assert captured["token"] in capsys.readouterr().out

    def test_parser_rejects_bad_port(self) -> None:
        """پورت غیرعددی خطای argparse می‌دهد."""
        from src.server.__main__ import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["--port", "http"])


class TestDesktopWindow:
    """پنجره‌ی دسکتاپ (``--desktop``): با pywebview و در غیر این صورت با مرورگر."""

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # noqa: D401 - ثبت آرگومان
        """`run_server` را با یک ثبت‌کننده‌ی بی‌خطر جایگزین می‌کند."""
        box: dict[str, Any] = {}
        import src.server as server_module

        def fake_run_server(settings: Config, *, host: str | None = None, port: int | None = None) -> None:
            box["settings"] = settings
            box["host"] = host or settings.server_host
            box["calls"] = int(box.get("calls", 0)) + 1

        monkeypatch.setattr(server_module, "run_server", fake_run_server)
        monkeypatch.setattr(cli_module, "Console", None)
        return box

    def test_desktop_flag_defaults_to_off(self) -> None:
        """فلگ --desktop پیش‌فرض خاموش است (تا اجرای معمولی سرور رفتار قدیم بماند)."""
        assert build_parser().parse_args([]).desktop is False
        assert build_parser().parse_args(["--serve", "--desktop"]).desktop is True

    def test_page_url_adds_token(self) -> None:
        """توکن به URL اضافه و URL-encode می‌شود (تا تایپ کاربر لازم نباشد)."""
        assert cli_module._desktop_page_url("http://127.0.0.1:8765", "") == "http://127.0.0.1:8765/"  # noqa: SLF001
        assert (
            cli_module._desktop_page_url("http://127.0.0.1:8765/", "a b/c")  # noqa: SLF001
            == "http://127.0.0.1:8765/?token=a%20b%2Fc"
        )

    def test_wait_for_server_honours_abort(self) -> None:
        """`abort` فعال نباید صبر کند (سرور که bind نشد، زود اعلام شکست می‌کند)."""
        calls = {"n": 0}

        def abort() -> bool:
            calls["n"] += 1
            return True

        assert cli_module._wait_for_server("http://127.0.0.1:1/", timeout=30.0, abort=abort) is False  # noqa: SLF001
        assert calls["n"] == 1

    def test_wait_for_server_gives_up_quickly(self) -> None:
        """پورت بسته → False، آن هم زودتر از سقف timeout (نه هندل‌کردن ابدی)."""
        start = time.monotonic()
        assert cli_module._wait_for_server("http://127.0.0.1:1/", timeout=0.3) is False  # noqa: SLF001
        assert time.monotonic() - start < 3.0

    def test_wait_for_server_finds_open_port(self, tmp_path: Path) -> None:
        """وقتی چیزی گوش می‌دهد، باید True برگرداند."""
        with contextlib.closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            probe.listen(1)
            port = probe.getsockname()[1]
            assert cli_module._wait_for_server(f"http://127.0.0.1:{port}", timeout=2.0) is True  # noqa: SLF001

    def test_serve_in_window_falls_back_to_browser(
        self, config: Config, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pywebview نصب نباشد → همان URL در مرورگر پیش‌فرض باز می‌شود."""
        import webbrowser

        opened: list[str] = []
        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.setitem(sys.modules, "webview", None)  # ImportError مصنوعی

        settings = config.model_copy(update={"server_token": "tok-123"})
        assert cli_module.serve_in_window(settings, "http://127.0.0.1:8765", None, startup_wait=0.3) == 0
        assert opened == ["http://127.0.0.1:8765/?token=tok-123"]
        assert captured["settings"].server_token == "tok-123"
        assert captured["calls"] == 1

    def test_serve_in_window_uses_pywebview(
        self, config: Config, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pywebview باشد → پنجره‌ی بومی ساخته و start می‌شود."""
        from types import ModuleType

        calls: dict[str, Any] = {}

        class FakeWebview(ModuleType):
            def create_window(self, title: str, url: str, **kwargs: Any) -> None:
                calls["window"] = (title, url, kwargs)

            def start(self) -> None:
                calls["started"] = True

        monkeypatch.setitem(sys.modules, "webview", FakeWebview("webview"))
        assert cli_module.serve_in_window(config, "http://127.0.0.1:8765", None, startup_wait=0.3) == 0
        assert calls["started"] is True
        title, url, kwargs = calls["window"]
        assert title == "Universal Agent Hub"
        assert url == "http://127.0.0.1:8765/"
        assert kwargs["width"] == 1200
        assert captured["calls"] == 1

    def test_serve_in_window_reports_server_error(self, config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """اگر bind شکست بخورد، خطا به فراخوان بالا منتقل می‌شود (بی‌صدا نمی‌میرد)."""
        import webbrowser

        import src.server as server_module

        def boom(settings: Config, **_kwargs: Any) -> None:
            raise OSError("address already in use")

        monkeypatch.setattr(server_module, "run_server", boom)
        monkeypatch.setattr(webbrowser, "open", lambda _url: True)
        monkeypatch.setitem(sys.modules, "webview", None)
        monkeypatch.setattr(cli_module, "Console", None)
        with pytest.raises(OSError, match="address already in use"):
            cli_module.serve_in_window(config, "http://127.0.0.1:8765", None, startup_wait=0.3)

    def test_serve_routes_desktop_to_window(
        self, config: Config, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--serve --desktop`` دیگر run_server را مستقیم صدا نمی‌زند."""
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda _url: True)
        monkeypatch.setitem(sys.modules, "webview", None)
        monkeypatch.setattr(cli_module, "_wait_for_server", lambda *a, **k: True)  # noqa: SLF001
        args = argparse.Namespace(
            serve=True, lan=False, host=None, port=None, token=None, insecure=False, no_ui=False, desktop=True
        )
        assert _serve(config, args) == 0
        assert captured["calls"] == 1

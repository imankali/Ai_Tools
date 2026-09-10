"""اجرای مستقیم سرور: ``python -m src.server``.

برای شروع سریع روی همان ماشین (تا اپ موبایل با QR/آدرس LAN وصل شود)::

    python -m src.server --host 127.0.0.1 --port 8765
    python -m src.server --lan --token "$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
"""

from __future__ import annotations

import argparse
import secrets
import sys

from src import __version__

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    """ساخت parser ساده‌ی ``python -m src.server``."""
    parser = argparse.ArgumentParser(
        prog="python -m src.server", description="Serve the Universal Agent Hub API + mobile UI."
    )
    parser.add_argument("--host", default=None, help="bind address (default: SERVER_HOST from .env)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: SERVER_PORT)")
    parser.add_argument("--lan", action="store_true", help="bind 0.0.0.0 so phones on the Wi-Fi can connect")
    parser.add_argument("--token", default=None, help="access token required from apps (recommended on --lan)")
    parser.add_argument("--print-token", action="store_true", help="generate and print a fresh token, then use it")
    parser.add_argument(
        "--insecure", action="store_true", help="allow LAN binding without a token (only on trusted networks)"
    )
    parser.add_argument(
        "--desktop", action="store_true", help="open a native window (pywebview) or the default browser once serving"
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    """نقطه‌ی ورود ``python -m src.server`` (کد خروج مناسب برای systemd/termux)."""
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"universal-agent-hub server {__version__}")
        return 0

    from src.config import get_config, reset_config
    from src.utils.logger import setup_logging

    reset_config()
    config = get_config()
    updates: dict[str, object] = {"server_enabled": True}
    token = args.token or config.server_token
    if args.print_token:
        token = secrets.token_urlsafe(24)
    if args.lan:
        updates["server_host"] = "0.0.0.0"
    if args.host:
        updates["server_host"] = args.host
    if args.port:
        updates["server_port"] = int(args.port)
    if token:
        updates["server_token"] = token
    try:
        config = config.model_copy(update=updates)
    except Exception as exc:  # noqa: BLE001 - ورودی نامعتبر از CLI
        print(f"invalid arguments: {exc}", file=sys.stderr)
        return 2
    if config.server_is_open and not config.server_token and not args.insecure:
        print(
            "refusing to expose the agent on the network without a token.\n"
            "  safe:   --print-token   (generates a strong token and prints it once)\n"
            "  unsafe: --insecure      (only on a network you fully trust)",
            file=sys.stderr,
        )
        return 2
    setup_logging(config.log_level, config.resolved_log_file, rich_console=False)

    from src.server import run_server

    if args.desktop:
        from src.cli import serve_in_window

        try:
            return serve_in_window(config, config.server_url, None)
        except KeyboardInterrupt:  # pragma: no cover - توقف دستی
            print("\nserver stopped")
            return 0

    print(f"Universal Agent Hub {__version__} → {config.server_url}")
    print(
        f"  open the UI in a browser (or the Android/iOS app) · auth: {'token' if config.server_token else 'loopback only'}"
    )
    if config.server_token and (config.server_is_open or args.print_token):
        print(f"  token: {config.server_token}")
        print("  keep it private: anyone with the URL + token controls this machine.")
    try:
        run_server(config, host=config.server_host, port=config.server_port)
    except KeyboardInterrupt:  # pragma: no cover - توقف دستی
        print("\nserver stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Universal Agent Hub — نصب‌کننده‌ی macOS و لینوکس
#
#   curl -fsSL https://raw.githubusercontent.com/imankali/Ai_Tools/main/universal-agent-hub/scripts/install.sh | bash
#
# یا بعد از clone:
#
#   ./scripts/install.sh --dev          # نصب از همین چک‌اوت (ویرایش‌پذیر)
#   ./scripts/install.sh --with all     # با psutil + ddgs + playwright
#
# کارها: ساخت venv در ~/.agent-hub/venv، نصب بسته، ساخت shim در
# ~/.local/bin/agent-hub، و نوشتن .env نمونه در ~/.agent-hub اگر وجود نداشته باشد.
# هیچ sudo لازم نیست و چیزی جز ~/.agent-hub و ~/.local/bin دست نمی‌زند.
# ---------------------------------------------------------------------------
set -euo pipefail

VERSION="1.0.0"
PREFIX="${HOME}/.agent-hub"
BIN_DIR="${HOME}/.local/bin"
FROM_SOURCE=0
EXTRAS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) FROM_SOURCE=1; shift ;;
    --with) EXTRAS="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

log() { printf '  \033[36m==\033[0m %s\n' "$*"; }
die() { printf '  \033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

find_python() {
  for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo "$candidate"; return 0
      fi
    fi
  done
  return 1
}

PY_BIN="$(find_python)" || die "python 3.10+ is required (brew install python@3.12 | apt install python3-venv)"
log "python: $("$PY_BIN" --version) at $(command -v "$PY_BIN")"

mkdir -p "$PREFIX" "$BIN_DIR"

if [[ "$FROM_SOURCE" -eq 1 ]]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [[ -f "$SRC/pyproject.toml" ]] || die "--dev needs the repo layout (pyproject.toml not found next to this script)"
  log "installing from checkout: $SRC"
  INSTALL_TARGET=".${EXTRAS:+[$EXTRAS]}"
else
  log "installing universal-agent-hub $VERSION from PyPI"
  INSTALL_TARGET="universal-agent-hub${EXTRAS:+[$EXTRAS]}"
fi

if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  log "creating virtualenv at $PREFIX/venv"
  "$PY_BIN" -m venv "$PREFIX/venv"
fi
VENV_PY="$PREFIX/venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip >/dev/null
( cd "${SRC:-$PREFIX}" && "$VENV_PY" -m pip install "$INSTALL_TARGET" )

cat > "$BIN_DIR/agent-hub" <<SHIM
#!/usr/bin/env bash
# ساخته‌شده توسط install.sh — اجرای Universal Agent Hub
exec "$PREFIX/venv/bin/agent-hub" "\$@"
SHIM
chmod +x "$BIN_DIR/agent-hub"

if [[ ! -f "$PREFIX/.env" ]]; then
  cat > "$PREFIX/.env" <<'ENV'
# Universal Agent Hub — تنظیمات
OPENAI_API_KEY=
# مدل و آدرس base URL (برای مدل‌های غیررسمی)
# MODEL_NAME=gpt-6-astra
# OPENAI_BASE_URL=https://…/v1
# سرور: برای اتصال گوشی این را روی 0.0.0.0 بگذارید (یا agent-hub --serve --lan)
SERVER_HOST=127.0.0.1
SERVER_PORT=8765
ENV
  log "wrote $PREFIX/.env (کلید API را همین‌جا وارد کنید)"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) log "note: add $BIN_DIR to your PATH" ;;
esac

log "done ✓"
cat <<DONE

  next steps
    agent-hub --version                 should print universal-agent-hub $VERSION
    agent-hub --serve --desktop         UI روی همین کامپیوتر
    agent-hub --serve --lan             اتصال گوشی (توکن را در اپ وارد کنید)

  config: $PREFIX/.env   ·   uninstall: rm -rf $PREFIX $BIN_DIR/agent-hub
DONE

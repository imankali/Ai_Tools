#!/data/data/com.termux/files/usr/bin/bash
# ---------------------------------------------------------------------------
# Universal Agent Hub — نصب روی Termux (اندروید)
#
# این حالت «سرور روی خود گوشی» است: ایجنت داخل Termux اجرا می‌شود و ابزار
# terminal/filesystem در sandbox ترموکس کار می‌کنند (بدون روت). برای کنترل
# کامپیوتر، لازم نیست چیزی روی گوشی نصب کنید — همان PWA/اپ اندروید را با
# آدرس LAN کامپیوتر استفاده کنید.
#
#   pkg install curl -y && curl -fsSL <raw-url>/install-termux.sh | bash
# ---------------------------------------------------------------------------
set -euo pipefail

log() { printf '  == %s\n' "$*"; }
die() { printf '  error: %s\n' "$*" >&2; exit 1; }

command -v pkg >/dev/null 2>&1 || die "این اسکریپت برای Termux است (F-Droid → Termux)"

log "به‌روزرسانی بسته‌های termux"
pkg update -y >/dev/null
pkg install -y python termux-api termux-services ripgrep >/dev/null 2>&1 || true

log "نصب universal-agent-hub (با psutil + ddgs)"
python -m pip install --upgrade pip >/dev/null
python -m pip install "universal-agent-hub[all]" 2>/dev/null \
  || python -m pip install "universal-agent-hub[system,search]" 2>/dev/null \
  || die "npm/pip install failed — after 'pip install universal-agent-hub' rerun"

mkdir -p "$HOME/.universal-agent-hub"
if [[ ! -f "$HOME/.universal-agent-hub/.env" ]]; then
  cat > "$HOME/.universal-agent-hub/.env" <<'ENV'
# Universal Agent Hub روی اندروید (Termux)
OPENAI_API_KEY=
# همیشه loopback؛ گوشی خودش سرور است
SERVER_HOST=127.0.0.1
SERVER_PORT=8765
ENV
  log "نمونه‌ی .env نوشته شد: ~/.universal-agent-hub/.env"
fi

# بیدار نگه‌داشتن CPU هنگام اجرای کارهای بلند
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true

log "آماده ✓"
cat <<DONE

  اجرا (از HOME، تا ابزار filesystem در محدوده‌ی مجاز Termux بماند):
    cd ~ && agent-hub --serve
    # بعد در Chrome گوشی باز کنید:  http://127.0.0.1:8765
    # و از منوی Chrome «Add to Home screen» → مثل اپ نصب می‌شود

  اجرای یک‌باره:
    agent-hub --prompt "what is the battery level?"

  سرویس پس‌زمینه (اختیاری):
    sv-ack termux-services  # یا: termux-services + runit، دستور بالا در README
DONE

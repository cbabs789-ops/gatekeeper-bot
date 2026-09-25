#!/usr/bin/env bash
# Gatekeeper installer for a fresh Ubuntu server.
#   curl -fsSL https://raw.githubusercontent.com/cbabs789-ops/gatekeeper-bot/main/install.sh | sudo bash
set -euo pipefail

REPO="https://github.com/cbabs789-ops/gatekeeper-bot.git"
APP=/opt/gatekeeper
ENVF=/etc/gatekeeper.env

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Run this as root (or with sudo)."; exit 1; fi

say "1/5  Installing system packages"
# Small servers ($4-6 plans) get a 1 GB swap file so installs and the bot don't run out of memory.
if [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 1600000 ] && [ "$(swapon --show | wc -l)" -eq 0 ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile \
    && grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git sqlite3 >/dev/null

say "2/5  Downloading the bot"
if [ -d "$APP/.git" ]; then git -C "$APP" pull -q; else git clone -q "$REPO" "$APP"; fi
id gatekeeper >/dev/null 2>&1 || useradd --system --home /var/lib/gatekeeper --shell /usr/sbin/nologin gatekeeper
mkdir -p /var/lib/gatekeeper && chown gatekeeper:gatekeeper /var/lib/gatekeeper

say "3/5  Setting up Python"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

say "4/5  Your keys (typed here, stored only on this server)"
systemctl stop gatekeeper 2>/dev/null || true

# Pasting into a web console can add invisible characters. Strip them.
clean() { printf '%s' "$1" | sed -e 's/\x1b\[20[01]~//g' -e 's/\[20[01]~//g' | tr -cd 'A-Za-z0-9:_-'; }
tg_ok() { curl -fsS -m 10 "https://api.telegram.org/bot$1/getMe" 2>/dev/null | grep -q '"ok":true'; }

TG=""; HK=""
if [ -f "$ENVF" ]; then
  TG="$(clean "$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ENVF" | cut -d= -f2- || true)")"
  HK="$(clean "$(grep -m1 '^HELIUS_API_KEY=' "$ENVF" | cut -d= -f2- || true)")"
  CHAT="$(grep -m1 '^TELEGRAM_CHAT_ID=' "$ENVF" | cut -d= -f2- || true)"
fi
while ! tg_ok "$TG"; do
  [ -n "$TG" ] && echo "Telegram doesn't recognize that token (starts '${TG:0:6}', ${#TG} characters). A real one looks like 8123456789:AAH... and is about 46 characters."
  printf 'Paste your Telegram bot token, then press Enter: '
  read -r RAW < /dev/tty
  TG="$(clean "$RAW")"
done
BOTNAME="$(curl -fsS -m 10 "https://api.telegram.org/bot$TG/getMe" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
echo "Token works. Connected to @$BOTNAME"
if [ -z "$HK" ]; then
  printf 'Paste your Helius API key (or just press Enter to skip for now): '
  read -r RAW < /dev/tty
  HK="$(clean "$RAW")"
fi
umask 077
cat > "$ENVF" <<EOF
# Gatekeeper settings. Edit with: gatekeeper config
TELEGRAM_BOT_TOKEN=$TG
TELEGRAM_CHAT_ID=${CHAT:-}
HELIUS_API_KEY=$HK
# Strategy overrides go here, for example:
# GK_POSITION_USD=100
# GK_STOP_LOSS_PCT=30
EOF
chown root:gatekeeper "$ENVF"; chmod 640 "$ENVF"

install -m 755 "$APP/gatekeeper.sh" /usr/local/bin/gatekeeper
cd "$APP"
if ! grep -q '^TELEGRAM_CHAT_ID=.\+' "$ENVF"; then
  until GATEKEEPER_ENV="$ENVF" "$APP/.venv/bin/python" -m gatekeeper setup-telegram; do
    printf 'In Telegram, open @%s, send it "hi", then press Enter here to try again (or type skip): ' "$BOTNAME"
    read -r ANS < /dev/tty
    [ "$ANS" = "skip" ] && break
  done
fi

say "5/5  Starting the bot"
install -m 644 "$APP/gatekeeper.service" /etc/systemd/system/gatekeeper.service
systemctl daemon-reload
systemctl enable -q --now gatekeeper
systemctl restart gatekeeper
sleep 3
systemctl --no-pager --lines=0 status gatekeeper | head -3 || true

say "Done. Gatekeeper is running and recording."
cat <<'EOF'
Useful commands:
  gatekeeper status      feed health and open paper trades
  gatekeeper report      paper results so far
  gatekeeper logs        watch what it's doing live (Ctrl+C to stop watching)
  gatekeeper backtest    replay recorded history through the rules
  gatekeeper update      pull the latest version and restart
In Telegram, send your bot /status, /today or /week.
EOF

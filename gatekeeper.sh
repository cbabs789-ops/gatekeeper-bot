#!/usr/bin/env bash
# The `gatekeeper` command on your server.
APP=/opt/gatekeeper
PY="$APP/.venv/bin/python"
run() { cd "$APP" && sudo -u gatekeeper env GATEKEEPER_ENV=/etc/gatekeeper.env "$PY" -m gatekeeper "$@"; }

case "${1:-help}" in
  status)   systemctl is-active --quiet gatekeeper && echo "Service: running" || echo "Service: STOPPED (try: gatekeeper restart)"; run status ;;
  report)   shift; run report "$@" ;;
  backtest) shift; run backtest "$@" ;;
  settings) run settings ;;
  fomo)     shift; run fomo "$@" ;;
  sweep)    shift; run sweep "$@" ;;
  followtest) shift; run followtest "$@" ;;
  logs)     journalctl -u gatekeeper -f -n 50 ;;
  restart)  sudo systemctl restart gatekeeper && echo "Restarted." ;;
  stop)     sudo systemctl stop gatekeeper && echo "Stopped. Start again with: gatekeeper restart" ;;
  update)   sudo git -C "$APP" pull -q && sudo "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" \
              && sudo install -m 755 "$APP/gatekeeper.sh" /usr/local/bin/gatekeeper \
              && sudo systemctl restart gatekeeper && echo "Updated and restarted." ;;
  config)   sudo nano /etc/gatekeeper.env && sudo systemctl restart gatekeeper && echo "Saved and restarted." ;;
  autoupdate)
    case "${2:-on}" in
      on)
        sudo tee /etc/systemd/system/gatekeeper-autoupdate.service >/dev/null <<'UNIT'
[Unit]
Description=Install new Gatekeeper updates automatically
[Service]
Type=oneshot
ExecStart=/usr/local/bin/gatekeeper auto-update-check
UNIT
        sudo tee /etc/systemd/system/gatekeeper-autoupdate.timer >/dev/null <<'UNIT'
[Unit]
Description=Check for Gatekeeper updates every 10 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
        sudo systemctl daemon-reload && sudo systemctl enable --now gatekeeper-autoupdate.timer >/dev/null \
          && echo "Auto-update is ON. New updates install within 10 minutes (never while a /test is running)." ;;
      off) sudo systemctl disable --now gatekeeper-autoupdate.timer >/dev/null 2>&1; echo "Auto-update is OFF. Use: gatekeeper update" ;;
      *) systemctl is-active --quiet gatekeeper-autoupdate.timer && echo "Auto-update: ON" || echo "Auto-update: OFF" ;;
    esac ;;
  auto-update-check)
    sudo git -C "$APP" fetch -q origin || exit 0
    [ "$(sudo git -C "$APP" rev-parse HEAD)" = "$(sudo git -C "$APP" rev-parse '@{u}')" ] && exit 0
    [ "$(run busy 2>/dev/null)" = "1" ] && { echo "A test is running; will update later."; exit 0; }
    sudo git -C "$APP" pull -q && sudo "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" \
      && sudo install -m 755 "$APP/gatekeeper.sh" /usr/local/bin/gatekeeper && sudo systemctl restart gatekeeper && echo "Auto-updated." ;;
  setup-telegram) cd "$APP" && sudo env GATEKEEPER_ENV=/etc/gatekeeper.env "$PY" -m gatekeeper setup-telegram && sudo systemctl restart gatekeeper ;;
  *) cat <<'EOF'
gatekeeper status | report [--days N] | backtest [--days N] [--split] [--set NAME=VALUE] [--trades]
           settings | logs | restart | stop | update | config | setup-telegram | autoupdate on|off|status
           fomo scan | fomo feed | fomo credits | fomo raw <handle> | sweep [--days N] | followtest [--days N]
EOF
  ;;
esac

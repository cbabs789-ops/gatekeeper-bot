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
  logs)     journalctl -u gatekeeper -f -n 50 ;;
  restart)  sudo systemctl restart gatekeeper && echo "Restarted." ;;
  stop)     sudo systemctl stop gatekeeper && echo "Stopped. Start again with: gatekeeper restart" ;;
  update)   sudo git -C "$APP" pull -q && sudo "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" \
              && sudo install -m 755 "$APP/gatekeeper.sh" /usr/local/bin/gatekeeper \
              && sudo systemctl restart gatekeeper && echo "Updated and restarted." ;;
  config)   sudo nano /etc/gatekeeper.env && sudo systemctl restart gatekeeper && echo "Saved and restarted." ;;
  setup-telegram) cd "$APP" && sudo env GATEKEEPER_ENV=/etc/gatekeeper.env "$PY" -m gatekeeper setup-telegram && sudo systemctl restart gatekeeper ;;
  *) cat <<'EOF'
gatekeeper status | report [--days N] | backtest [--days N] [--split] [--set NAME=VALUE] [--trades]
           settings | logs | restart | stop | update | config | setup-telegram
           fomo scan | fomo feed | fomo credits | fomo raw <handle>
EOF
  ;;
esac

#!/bin/bash
if [ "$0" != "/tmp/mm_rollback.sh" ]; then cp "$0" /tmp/mm_rollback.sh; exec bash /tmp/mm_rollback.sh "$@"; fi
set -e
APP="$HOME/machine-monitoring"; PREV="$HOME/machine-monitoring.previous.tgz"
[ -f "$PREV" ] || { echo "no previous version saved"; exit 1; }
tar -xzf "$PREV" -C "$APP"
systemctl --user restart machine-monitor.service
sleep 5
systemctl --user is-active --quiet machine-monitor.service && echo "OK - previous version running" || echo "!! still not running"
tail -n 10 "$APP/data/monitor.log"

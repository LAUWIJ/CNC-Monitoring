#!/bin/bash
# Runs on the Pi after deploy.bat uploaded a new version: keep the old one, unpack, restart.
# Re-run from /tmp because unpacking overwrites this very file.
if [ "$0" != "/tmp/mm_after_deploy.sh" ]; then cp "$0" /tmp/mm_after_deploy.sh; exec bash /tmp/mm_after_deploy.sh "$@"; fi
set -e
PKG="$1"; APP="$HOME/machine-monitoring"
cd "$APP"
tar --exclude=./data --exclude=./.venv -czf "$HOME/machine-monitoring.previous.tgz" .
tar -xzf "$PKG" -C "$APP"
rm -f "$PKG"
if ! cmp -s requirements.txt .venv/.installed-requirements; then
  echo "requirements changed - installing"
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt && cp requirements.txt .venv/.installed-requirements
fi
mkdir -p "$HOME/.config/systemd/user"
cp pi/machine-monitor.service pi/machine-monitor-backup.service pi/machine-monitor-backup.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user restart machine-monitor.service
sleep 5
if systemctl --user is-active --quiet machine-monitor.service; then
  echo "OK - monitor is running the new version"
else
  echo "!! monitor did NOT start - run rollback_pi.bat to go back"; fi
echo "--- last log lines"
tail -n 15 "$APP/data/monitor.log" 2>/dev/null || journalctl --user -u machine-monitor -n 15 --no-pager

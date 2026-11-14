#!/bin/bash
# Runs on the Pi after deploy.bat uploaded a new version: keep the old one, unpack, restart.
# Re-run from /tmp because unpacking overwrites this very file.
if [ "$0" != "/tmp/mm_after_deploy.sh" ]; then cp "$0" /tmp/mm_after_deploy.sh; exec bash /tmp/mm_after_deploy.sh "$@"; fi
set -e
PKG="$1"; APP="$HOME/machine-monitoring"
cd "$APP"
tar --exclude=./data --exclude=./.venv -czf "$HOME/machine-monitoring.previous.tgz" .
# config.toml can be changed on the Pi in the dashboard (Settings). data/config.deployed.toml is the version as
# it was last deployed (or as it was before the first change in the dashboard). If the Pi's config.toml differs
# from it, it was changed in the dashboard: keep it. deploy.bat then copies it back to the PC.
KEEP=""
if [ -f config.toml ] && [ -f data/config.deployed.toml ] && ! cmp -s config.toml data/config.deployed.toml; then
  KEEP=1; cp config.toml /tmp/mm_config_pi.toml
fi
tar -xzf "$PKG" -C "$APP"
rm -f "$PKG"
if [ -n "$KEEP" ]; then
  if ! cmp -s config.toml data/config.deployed.toml; then
    cp config.toml data/config.from_pc.toml
    echo "!! config.toml was changed on the PC AND in the dashboard. The dashboard version is kept;"
    echo "!! Your PC version is saved as config.toml.before_deploy on the PC - merge by hand if you need it."
  else
    echo "config.toml: keeping the changes made in the dashboard (copied back to the PC)"
  fi
  cp /tmp/mm_config_pi.toml config.toml; rm -f /tmp/mm_config_pi.toml
fi
mkdir -p data; cp config.toml data/config.deployed.toml
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

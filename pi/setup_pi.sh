#!/bin/bash
# One-time setup on the Raspberry Pi (safe to run again). Called by setup_pi.bat on the PC.
set -e
APP="$HOME/machine-monitoring"
cd "$APP"
echo "== 1/5 system packages (asks for your Pi password)"
sudo timedatectl set-timezone Europe/Amsterdam || true
# Python libraries that need compiling on ARM come ready-made from Debian
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-cryptography python3-openssl python3-dateutil \
     python3-tz python3-sortedcontainers python3-aiofiles python3-aiosqlite python3-typing-extensions

echo "== 2/5 Python environment"
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
cp requirements.txt .venv/.installed-requirements

echo "== 3/5 check: Python must be 32-bit for the FANUC library"
.venv/bin/python - <<'PY'
import sys, platform
bits = 64 if sys.maxsize > 2**32 else 32
print(f"   kernel {platform.machine()}, Python {bits}-bit")
if bits == 64 and platform.machine().startswith(("aarch64", "arm")):
    print("   WARNING: 64-bit OS - FANUC machines will not connect (Heidenhain/Siemens are fine)")
PY

echo "== 4/5 service (starts on boot, restarts after a crash) + nightly backup"
mkdir -p "$HOME/.config/systemd/user" "$HOME/backups" "$APP/data"
cp pi/machine-monitor.service pi/machine-monitor-backup.service pi/machine-monitor-backup.timer "$HOME/.config/systemd/user/"
sudo loginctl enable-linger "$USER"          # run without anybody logged in
systemctl --user daemon-reload
systemctl --user enable machine-monitor.service machine-monitor-backup.timer >/dev/null
systemctl --user restart machine-monitor.service
systemctl --user start machine-monitor-backup.timer

echo "== 5/5 status"
sleep 5
systemctl --user --no-pager --lines=0 status machine-monitor.service | head -4
IP=$(hostname -I | awk '{print $1}')
echo
echo "Dashboard: http://$IP:8080"

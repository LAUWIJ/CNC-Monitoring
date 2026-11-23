#!/bin/bash
# Mounts a Windows network share (e.g. the N: drive with the NC programs) READ-ONLY on the Raspberry Pi, so the
# monitor can list and send programs from it (Settings: "Program folder on the server").
# A drive letter in the settings maps to /mnt/<letter>: N:\Programma's DVF5000-BMO -> /mnt/n/Programma's DVF5000-BMO
# Run through mount_share_pi.bat. The password is asked here on the Pi, stored only in a root-only file
# (/etc/cnc-monitor/<letter>.cred), never in config.toml or Git.
set -e
echo "Network share for the monitor (read-only)."
echo "On the PC: Explorer > This PC shows the drive as e.g.  Data (\\\\FILESERVER\\Data) (N:)"
echo "or run  net use  in a command prompt."
echo
read -rp "Drive letter on the PCs (e.g. N): " L
L=$(echo "${L%%:*}" | tr 'A-Z' 'a-z')
[[ "$L" =~ ^[a-z]$ ]] || { echo "Not a drive letter."; exit 1; }
read -rp "Share path, as \\\\SERVER\\share (what the drive letter points to): " UNC
UNC=$(echo "$UNC" | tr '\\' '/')
[[ "$UNC" =~ ^//[^/]+/.+ ]] || { echo "Must look like \\\\SERVER\\share"; exit 1; }
UNC="${UNC%/}"
read -rp "Windows user name (read access is enough): " U
read -rp "Domain (empty if none): " D
read -rsp "Password: " P; echo

MP="/mnt/$L"
CRED="/etc/cnc-monitor/$L.cred"
if ! command -v mount.cifs >/dev/null; then
  echo "Installing cifs-utils ..."
  sudo apt-get install -y cifs-utils >/dev/null
fi
sudo mkdir -p /etc/cnc-monitor "$MP"
sudo chmod 700 /etc/cnc-monitor
printf 'username=%s\npassword=%s\n%s' "$U" "$P" "${D:+domain=$D
}" | sudo tee "$CRED" >/dev/null
sudo chmod 600 "$CRED"
UNC_FSTAB=$(echo "$UNC" | sed 's/ /\\040/g')
LINE="$UNC_FSTAB $MP cifs credentials=$CRED,ro,uid=$(id -u),gid=$(id -g),iocharset=utf8,file_mode=0444,dir_mode=0555,nofail,_netdev,x-systemd.automount,x-systemd.mount-timeout=15 0 0"
sudo sed -i "\# $MP cifs #d" /etc/fstab
echo "$LINE" | sudo tee -a /etc/fstab >/dev/null
sudo systemctl daemon-reload
sudo umount "$MP" 2>/dev/null || true
if sudo mount "$MP"; then
  echo
  echo "Mounted $UNC at $MP (read-only). First entries:"
  ls "$MP" | head -15
  echo
  echo "In Settings use e.g.  ${L^^}:\\<folder>  as the machine's program folder on the server."
else
  echo
  echo "Mounting failed - check the share path, user and password (run this again to change them)."
  echo "Older servers may need  vers=2.0  or  vers=1.0  added to the line for $MP in /etc/fstab."
  exit 1
fi

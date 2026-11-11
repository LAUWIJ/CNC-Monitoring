"""Nightly consistent copy of the database (works while the monitor is running). Keeps 14 days."""
import datetime, glob, os, sqlite3
home = os.path.expanduser("~")
src = os.path.join(home, "machine-monitoring", "data", "monitor.db")
dst_dir = os.path.join(home, "backups")
os.makedirs(dst_dir, exist_ok=True)
dst = os.path.join(dst_dir, f"monitor-{datetime.date.today():%Y-%m-%d}.db")
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
for old in sorted(glob.glob(os.path.join(dst_dir, "monitor-*.db")))[:-14]:
    os.remove(old)
print("backup written:", dst)

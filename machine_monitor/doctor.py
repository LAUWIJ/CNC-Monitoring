"""`python run.py --doctor` - a read-only check of an installation, for people and for AI agents.

Checks Python, the packages the configured machines need, config.toml, the FANUC library, the data folder,
the dashboard port, the admin password and whether each machine answers on its port. Nothing is changed and
no control is logged in to: the machine check only opens and closes a TCP connection (for a full read use
`--probe <id>`). Exit code 0 = no FAIL. `--json` prints the same as JSON.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

DEFAULT_PORTS = {"fanuc": 8193, "siemens": 4840, "heidenhain": 19000, "mtconnect": 5000}
NEEDS = {"siemens": ("asyncua", "asyncua"), "heidenhain": ("pyLSV2", "pyLSV2")}   # control -> (import, pip name)
CONTROLS = set(DEFAULT_PORTS) | {"simulator"}


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, status, detail="", fix=""):
        self.checks.append({"name": name, "status": status, "detail": detail, **({"fix": fix} if fix else {})})

    @property
    def ok(self):
        return not any(c["status"] == "FAIL" for c in self.checks)


def _tcp(host, port, timeout=3.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, ""
    except OSError as e:
        return False, str(e) or type(e).__name__


def _target(m):
    """(host, port) the machine is reached on."""
    host = str(m.get("host") or "")
    port = m.get("port")
    if "://" in host:                                  # MTConnect URL
        u = urlparse(host)
        return u.hostname or "", u.port or port or (443 if u.scheme == "https" else 80)
    return host, port or DEFAULT_PORTS.get(m.get("control"), 0)


def run(root: str, cfg_path: str, connect: bool = True) -> Report:
    r = Report()
    v = sys.version_info
    r.add("python", "OK" if v >= (3, 11) else "FAIL", f"{v.major}.{v.minor}.{v.micro} ({sys.executable}, "
          f"{64 if sys.maxsize > 2**32 else 32}-bit)", "" if v >= (3, 11) else "install Python 3.11 or newer")

    # ---- config
    cfg, machines = {}, []
    if not os.path.exists(cfg_path):
        r.add("config", "WARN", f"{cfg_path} not found",
              "start the monitor once (it creates an empty one), then add machines in the dashboard (Settings) "
              "or copy config.example.toml to config.toml")
    else:
        try:
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
            machines = cfg.get("machine", [])
            problems = []
            ids = [m.get("id") for m in machines]
            for m in machines:
                c = str(m.get("control", "")).strip().lower()
                if not m.get("id"):
                    problems.append("a machine without id")
                if c not in CONTROLS:
                    problems.append(f"{m.get('id')}: unknown control '{m.get('control')}'")
                if c != "simulator" and not m.get("host"):
                    problems.append(f"{m.get('id')}: no host")
            dup = sorted({i for i in ids if ids.count(i) > 1 and i})
            if dup:
                problems.append("duplicate id " + ", ".join(dup))
            on = [m for m in machines if m.get("enabled", True)]
            r.add("config", "FAIL" if problems else "OK",
                  "; ".join(problems) or f"{len(machines)} machine(s), {len(on)} enabled", "fix config.toml" if problems else "")
        except tomllib.TOMLDecodeError as e:
            r.add("config", "FAIL", f"{cfg_path}: {e}", "fix the TOML syntax at that line")
    active = [m for m in machines if m.get("enabled", True)]
    controls = {str(m.get("control", "")).strip().lower() for m in active}

    # ---- packages the configured controls need
    for c, (mod, pip) in NEEDS.items():
        try:
            __import__(mod)
            r.add(f"package {pip}", "OK", "installed")
        except ImportError:
            need = c in controls
            r.add(f"package {pip}", "FAIL" if need else "WARN",
                  f"not installed{' - needed for ' + c.upper() + ' machines' if need else ' (only needed for ' + c.upper() + ')'}",
                  "pip install -r requirements.txt")

    # ---- FANUC library (loaded, not connected)
    fanucs = [m for m in active if str(m.get("control", "")).lower() == "fanuc"]
    if fanucs:
        sys.path.insert(0, root)
        try:
            from machine_monitor.drivers.fanuc_focas import FanucFocasDriver
            d = FanucFocasDriver({**cfg.get("defaults", {}), **fanucs[0], "host": "0.0.0.0"})
            d._load()
            r.add("FANUC FOCAS library", "OK", "loads")
        except Exception as e:
            r.add("FANUC FOCAS library", "FAIL", str(e),
                  "Raspberry Pi: 32-bit Raspberry Pi OS (FOCAS/ is included). Windows: put Fwlib64.dll and all "
                  "fwlib*64.dll files from FANUC (A02B-0207-K737) in FOCAS\\ - see FOCAS/README.txt")

    # ---- data folder, database, admin password
    data = os.path.join(root, "data")
    try:
        os.makedirs(data, exist_ok=True)
        probe = os.path.join(data, ".doctor")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        db = cfg.get("server", {}).get("database", "data/monitor.db")
        db = db if os.path.isabs(db) else os.path.join(root, db)
        r.add("data folder", "OK", f"writable; database {os.path.getsize(db) / 1048576:.1f} MB" if os.path.exists(db)
              else "writable; no database yet (created on the first start)")
    except OSError as e:
        r.add("data folder", "FAIL", str(e), "the monitor must be able to write in data/")
    adm = os.path.exists(os.path.join(data, "admin.json"))
    r.add("admin password", "OK" if adm else "WARN", "set" if adm else "not set - the first visitor to Settings chooses it",
          "" if adm else "open the dashboard -> Settings and choose it now, or: python run.py --set-admin-password")

    # ---- dashboard port: free, or the monitor is already running there
    port = int(cfg.get("server", {}).get("port", 8080))
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/about", timeout=2) as resp:
            about = json.loads(resp.read())
        r.add("dashboard", "OK", f"monitor {about.get('version')} is running on http://localhost:{port}")
    except Exception:
        up, _ = _tcp("127.0.0.1", port, 1.0)
        r.add("dashboard", "FAIL" if up else "OK",
              f"port {port} is used by another program" if up else f"not running; port {port} is free",
              f"set [server] port in config.toml" if up else "")

    # ---- machines: TCP connect only (no login, nothing read)
    if connect:
        real = [m for m in active if str(m.get("control", "")).lower() != "simulator"]

        def chk(m):
            host, p = _target(m)
            if not host or not p:
                return m, False, "no host/port"
            ok, err = _tcp(host, p)
            return m, ok, f"{host}:{p} " + ("answers" if ok else f"no answer ({err})")
        with ThreadPoolExecutor(max_workers=16) as ex:
            for m, ok, detail in ex.map(chk, real):
                r.add(f"machine {m.get('id')}", "OK" if ok else "WARN",
                      f"{m.get('name', m.get('id'))} ({str(m.get('control', '')).upper()}): {detail}",
                      "" if ok else "machine off? check the IP address, port, cable and the control's network option")
    return r


def main(root: str, cfg_path: str, as_json: bool = False, connect: bool = True) -> int:
    r = run(root, cfg_path, connect)
    if as_json:
        print(json.dumps({"ok": r.ok, "checks": r.checks}, indent=2))
    else:
        for c in r.checks:
            print(f"  [{c['status']:<4}] {c['name']:<22} {c['detail']}")
            if c.get("fix"):
                print(f"         {'':<22} -> {c['fix']}")
        n = {s: sum(1 for c in r.checks if c["status"] == s) for s in ("OK", "WARN", "FAIL")}
        print(f"\n  {n['OK']} OK, {n['WARN']} warning(s), {n['FAIL']} problem(s)")
    return 0 if r.ok else 1

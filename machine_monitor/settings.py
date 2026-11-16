"""Machine settings for the dashboard's Settings page: what can be edited, validation, saving to config.toml
(through config_edit, comments kept) and a connection test with unsaved values.

Only the settings listed in FIELDS are edited here. Anything else a machine has in config.toml (OPC UA node
overrides, cycle signals, transfer roots, ...) is left alone and listed as "also set in config.toml".
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import tomllib
from typing import Callable, Optional

from . import config_edit

log = logging.getLogger("settings")

CONTROLS = [
    ("fanuc", "FANUC (FOCAS2)", 8193),
    ("siemens", "SIEMENS SINUMERIK (OPC UA)", 4840),
    ("heidenhain", "HEIDENHAIN TNC (LSV2)", 19000),
    ("mtconnect", "MTConnect (agent or adapter)", 5000),
]
TRANSFER_CONTROLS = ("fanuc", "siemens", "heidenhain")
REAL = [c[0] for c in CONTROLS]

# key, label, type, controls (None = all), default, help, extra
FIELDS = [
    {"key": "name", "label": "Name", "type": "text", "required": True},
    {"key": "host", "label": "IP address / host", "type": "text", "required": True, "controls": REAL,
     "help_for": {"mtconnect": "An IP address, or a full URL such as http://192.168.1.14:5000"}},
    {"key": "port", "label": "Port", "type": "int", "controls": REAL, "help": "Empty = the control's usual port"},
    {"key": "enabled", "label": "Monitor this machine", "type": "bool", "default": True},
    {"key": "username", "label": "OPC UA user", "type": "text", "controls": ["siemens"]},
    {"key": "password", "label": "OPC UA password", "type": "password", "controls": ["siemens"]},
    {"key": "security", "label": "Encryption", "type": "select", "controls": ["siemens"], "default": "",
     "options": [["", "None"], ["Basic256Sha256,SignAndEncrypt", "Basic256Sha256, sign + encrypt"]],
     "help": "With encryption, trust the monitor's certificate once on the control"},
    {"key": "alarm_source", "label": "Alarm source", "type": "select", "controls": ["siemens"], "default": "",
     "options": [["", "Server object (standard OPC UA)"], ["sinumerik", "Sinumerik object (SINUMERIK alarm events)"]],
     "help": "Check which one works with probe_alarms_pi.bat"},
    {"key": "part_count_macro", "label": "Part counter macro", "type": "int", "controls": ["fanuc"], "default": 3901,
     "help": "0 = don't read the part counter"},
    {"key": "memory_card", "label": "Memory card", "type": "select", "controls": ["fanuc"], "default": "",
     "options": [["", "Normal (or none)"], ["dnc", "DNC card shown as CF_TEXT (e.g. 0i-D)"]]},
    {"key": "read_m00_signals", "label": "Show M00/M01 as paused", "type": "bool", "controls": ["fanuc"], "default": True},
    {"key": "read_sequence_number", "label": "Show the N-number", "type": "bool", "controls": ["fanuc"], "default": False},
    {"key": "o8_digit_program_numbers", "label": "8-digit O-numbers", "type": "bool", "controls": ["fanuc"], "default": False},
    {"key": "fwlib_path", "label": "FOCAS library file", "type": "text", "controls": ["fanuc"],
     "help": "Empty = found automatically (Raspberry Pi: included; Windows: FOCAS\\ folder)"},
    {"key": "tool_in_spindle", "label": "Tool in spindle (PLC address)", "type": "text", "controls": ["heidenhain"],
     "help": "Empty = found automatically by \"Copy from control\". E.g. D17032"},
    {"key": "plc_messages", "label": "Read PLC messages", "type": "bool", "controls": ["heidenhain"], "default": True},
    {"key": "read_tool", "label": "Read the tool in the spindle", "type": "bool",
     "controls": ["fanuc", "heidenhain", "siemens"], "default": True},
    {"key": "device", "label": "Device name", "type": "text", "controls": ["mtconnect"],
     "help": "Only when the agent serves several devices"},
    {"key": "path", "label": "Path / turret", "type": "text", "controls": ["mtconnect"],
     "help": "Lathes with two paths: which one counts (empty = the first)"},
    {"key": "part_count_subtype", "label": "Part count subtype", "type": "text", "controls": ["mtconnect"]},
    {"key": "transfer", "label": "Program transfer", "type": "select", "controls": list(TRANSFER_CONTROLS),
     "options": [["off", "Off"], ["read", "Browse + fetch"], ["send", "Browse + fetch + send"]],
     "help": "FANUC is off unless switched on here"},
    {"key": "alarms", "label": "Read alarms & messages", "type": "bool", "default": True},
    {"key": "warmup_programs", "label": "Warm-up programs", "type": "programs",
     "help": "Comma-separated, e.g. 9000, WARMUP.H. While one runs in automatic mode the machine shows "
             "Warm-up: no part, no cycle, not counted as machining time."},
]
SECRET = {"password"}
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
_RX_CHARS = set("\\()[]{}|*+?^$")


def fields_for(control: str) -> list:
    return [f for f in FIELDS if not f.get("controls") or control in f["controls"]]


def default_of(f: dict, control: str):
    if f["key"] == "transfer":
        return "off" if control == "fanuc" else "read"
    return f.get("default")


def transfer_mode(v, control: str) -> str:
    if v is None:
        return "off" if control == "fanuc" else "read"
    if v in (False, "off", "no"):
        return "off"
    if v in ("send", "both", True, "on"):
        return "send"
    return "read"


# ---------------------------------------------------------------- wait rules <-> plain names
def name_to_pattern(text: str) -> str:
    """'9021' / 'O9021' -> ^O?9021$, 'LOAD_PRODUCT_0.H' -> ^LOAD_PRODUCT_0\\.H$, a regular expression as is."""
    t = text.strip()
    if not t:
        raise ValueError("a wait rule needs a program name")
    if any(c in _RX_CHARS for c in t):
        try:
            re.compile(t)
        except re.error as e:
            raise ValueError(f"'{t}' is not a valid pattern: {e}")
        return t
    m = re.fullmatch(r"[Oo]?(\d{1,8})", t)
    if m:
        return f"^O?{m.group(1)}$"
    return "^" + re.escape(t) + "$"


def pattern_to_name(p: str) -> str:
    m = re.fullmatch(r"\^O\?(\d{1,8})\$", p)
    if m:
        return m.group(1)
    if p.startswith("^") and p.endswith("$"):
        cand = re.sub(r"\\(.)", r"\1", p[1:-1])
        if "^" + re.escape(cand) + "$" == p:
            return cand
    return p


def wait_to_ui(rules) -> list:
    out = []
    for r in rules or []:
        on = "block" if r.get("block") else "program"
        val = r.get(on) or ""
        out.append({"on": on, "value": pattern_to_name(val) if on == "program" else val,
                    "reason": "robot" if str(r.get("reason", "operator")).lower() == "robot" else "operator",
                    "action": "end" if str(r.get("action", "pause")).lower() == "end" else "pause"})
    return out


def wait_from_ui(rules) -> list:
    out = []
    for r in rules or []:
        on = "block" if r.get("on") == "block" else "program"
        val = str(r.get("value", "")).strip()
        if not val:
            continue
        if on == "program":
            pat = name_to_pattern(val)
        else:
            try:
                re.compile(val)
            except re.error as e:
                raise ValueError(f"'{val}' is not a valid pattern: {e}")
            pat = val
        out.append({on: pat, "reason": "robot" if r.get("reason") == "robot" else "operator",
                    "action": "end" if r.get("action") == "end" else "pause"})
    return out


# ---------------------------------------------------------------- service
class SettingsService:
    def __init__(self, cfg_path: str, data_dir: str, admin, on_change: Optional[Callable[[str], None]] = None):
        self.cfg_path = cfg_path
        self.data_dir = data_dir
        self.admin = admin
        self.on_change = on_change           # called with the machine id after a save (reconnects it)
        self.lock = threading.Lock()
        self.test_lock = threading.Lock()

    # ------------------------------------------------------------ read
    def _text(self) -> str:
        with open(self.cfg_path, encoding="utf-8") as f:
            return f.read()

    def machines(self) -> dict:
        cfg = tomllib.loads(self._text())
        out = []
        for m in cfg.get("machine", []):
            c = str(m.get("control", "")).strip().lower()
            vals = {}
            for f in fields_for(c):
                k = f["key"]
                if k in SECRET:
                    vals[k] = {"set": bool(m.get(k))}
                elif k == "transfer":
                    vals[k] = transfer_mode(m.get(k), c)
                elif f["type"] == "programs":
                    v = m.get(k) or []
                    vals[k] = ", ".join(pattern_to_name(p) for p in ([v] if isinstance(v, str) else v))
                elif k in m:
                    vals[k] = m[k]
                else:
                    vals[k] = default_of(f, c)
            known = {f["key"] for f in fields_for(c)} | {"id", "control", "wait"}
            out.append({"id": m.get("id"), "control": c, "values": vals, "wait": wait_to_ui(m.get("wait")),
                        "other": sorted(k for k in m if k not in known)})
        return {"machines": out, "fields": FIELDS,
                "controls": [{"id": k, "label": l, "port": p} for k, l, p in CONTROLS],
                "config": os.path.basename(self.cfg_path)}

    # ------------------------------------------------------------ build / validate
    def _target(self, cur: dict, control: str, values: dict, clear: list) -> dict:
        """The machine dict as it should be in config.toml after this save."""
        new = {k: v for k, v in cur.items()}
        new["control"] = control
        keys_now = {f["key"] for f in fields_for(control)}
        for f in FIELDS:                               # settings of another control type go
            if f["key"] not in keys_now:
                new.pop(f["key"], None)
        for f in fields_for(control):
            k, t = f["key"], f["type"]
            if k not in values:
                continue
            v = values[k]
            if k in SECRET:
                if k in clear:
                    new.pop(k, None)
                elif isinstance(v, str) and v:
                    new[k] = v
                continue
            if t == "programs":
                names = [x.strip() for x in str(v or "").split(",") if x.strip()]
                pats = [name_to_pattern(x) for x in names]
                old = cur.get(k)
                old = [old] if isinstance(old, str) else list(old or [])
                if pats == old:
                    continue
                if pats:
                    new[k] = pats
                else:
                    new.pop(k, None)
                continue
            if t == "bool":
                v = bool(v)
            elif t == "int":
                if v in ("", None):
                    v = None
                else:
                    try:
                        v = int(str(v).strip())
                    except ValueError:
                        raise ValueError(f"{f['label']}: a whole number is needed")
            else:
                v = "" if v is None else str(v).strip()
                if v == "" and t == "text":
                    v = None
            if f.get("required") and v in (None, ""):
                raise ValueError(f"{f['label']} is required")
            if k == "port" and v is not None and not 1 <= v <= 65535:
                raise ValueError("Port: 1 - 65535")
            if k == "transfer":
                if v == transfer_mode(cur.get(k), control) and k in cur:
                    continue                           # unchanged: keep how it is written (e.g. true)
            if k == "tool_in_spindle" and isinstance(v, str) and v.lower() == "false":
                v = False
            if v is None or (t == "select" and v == "" and k != "transfer"):
                new.pop(k, None)
            elif k not in cur and v == default_of(f, control):
                new.pop(k, None)                       # a default that wasn't written stays unwritten
            else:
                new[k] = v
        return new

    def save(self, payload: dict) -> dict:
        mid = str(payload.get("id", "")).strip()
        is_new = bool(payload.get("new"))
        control = str(payload.get("control", "")).strip().lower()
        if control not in {c[0] for c in CONTROLS} | {"simulator"}:
            raise ValueError("choose a control type")
        values = payload.get("values") or {}
        wait = wait_from_ui(payload.get("wait"))
        with self.lock:
            text = self._text()
            cfg = tomllib.loads(text)
            ms = cfg.get("machine", [])
            idx = next((i for i, m in enumerate(ms) if m.get("id") == mid), None)
            if is_new:
                if not ID_RE.match(mid):
                    raise ValueError("ID: letters, digits, - and _ only (e.g. m14)")
                if idx is not None:
                    raise ValueError(f"there is already a machine with ID {mid}")
                target = self._target({"id": mid}, control, values, [])
                if wait:
                    target["wait"] = wait
                order = ["id", "name", "control", "host", "port"]
                vals = {k: target[k] for k in order if k in target}
                vals.update({k: v for k, v in target.items() if k not in vals and k != "wait"})
                new_text = config_edit.add_machine(text, vals, wait)
                expect = dict(vals)
                if wait:
                    expect["wait"] = wait
            else:
                if idx is None:
                    raise ValueError(f"machine {mid} is not in {os.path.basename(self.cfg_path)} (anymore)")
                cur = ms[idx]
                target = self._target(cur, control, values, list(payload.get("clear") or []))
                if wait:
                    target["wait"] = wait
                else:
                    target.pop("wait", None)
                if target == cur:
                    return {"ok": True, "changed": False}
                setv = {k: v for k, v in target.items() if k != "wait" and cur.get(k, object()) != v}
                remove = {k for k in cur if k not in target and k != "wait"}
                for k in setv:                          # sub-tables (nodes, ...) are never edited here
                    if isinstance(setv[k], dict):
                        raise ValueError(f"{k} can only be changed in config.toml")
                new_text = config_edit.update_machine(
                    text, idx, setv, remove, wait if (cur.get("wait") or []) != wait else None)
                expect = target
            config_edit.checked(text, new_text, mid, expect)
            self._remember_deployed(text)
            config_edit.write(self.cfg_path, new_text, os.path.join(self.data_dir, "config_backup"))
        log.info("settings: machine %s %s", mid, "added" if is_new else "changed")
        if self.on_change:
            self.on_change(mid)
        return {"ok": True, "changed": True}

    def delete(self, mid: str) -> dict:
        with self.lock:
            text = self._text()
            ms = tomllib.loads(text).get("machine", [])
            idx = next((i for i, m in enumerate(ms) if m.get("id") == mid), None)
            if idx is None:
                raise ValueError(f"machine {mid} not found")
            new_text = config_edit.remove_machine(text, idx)
            config_edit.checked(text, new_text, mid, None)
            self._remember_deployed(text)
            config_edit.write(self.cfg_path, new_text, os.path.join(self.data_dir, "config_backup"))
        log.info("settings: machine %s removed", mid)
        if self.on_change:
            self.on_change(mid)
        return {"ok": True}

    def _remember_deployed(self, text: str):
        """deploy.bat keeps the Pi's config.toml when it differs from data/config.deployed.toml. Installs from
        before that file existed: record the version as it was before the first change made here."""
        p = os.path.join(self.data_dir, "config.deployed.toml")
        if not os.path.exists(p):
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            except OSError:
                pass

    # ------------------------------------------------------------ connection test
    def test(self, payload: dict, defaults: Optional[dict] = None, timeout: float = 25.0) -> dict:
        """Connect with the values in the form (not saved yet), read once, disconnect."""
        from .drivers import make_driver
        mid = str(payload.get("id", "")).strip() or "test"
        control = str(payload.get("control", "")).strip().lower()
        cur = {}
        if not payload.get("new"):
            for m in tomllib.loads(self._text()).get("machine", []):
                if m.get("id") == mid:
                    cur = m
        m = self._target(dict(cur) or {"id": mid}, control, payload.get("values") or {}, [])
        m = {**(defaults or {}), **m, "id": mid, "control": control}
        if not m.get("host") and control != "simulator":
            raise ValueError("fill in the IP address first")
        if not self.test_lock.acquire(blocking=False):
            raise ValueError("a connection test is already running")
        res = {"ok": False, "lines": []}

        def run():
            lines = res["lines"]
            d = None
            try:
                t0 = time.time()
                d = make_driver(m)
                d.connect()
                lines.append(f"Connected in {time.time() - t0:.1f} s")
                s = d.read()
                st = getattr(s.exec_state, "value", s.exec_state)
                md = getattr(s.mode, "value", s.mode)
                lines.append(f"Online: {'yes' if s.online else 'no'} · state {st} · mode {md}")
                if s.main_program or s.current_program:
                    lines.append(f"Program: {s.main_program or '-'}"
                                 + (f" (now in {s.current_program})" if s.current_program and s.current_program != s.main_program else ""))
                if s.part_count is not None:
                    lines.append(f"Part counter: {s.part_count}")
                if getattr(s, "tool", None):
                    lines.append(f"Tool: {s.tool}")
                res["ok"] = bool(s.online)
            except Exception as e:
                lines.append(f"Failed: {type(e).__name__}: {e}")
            finally:
                try:
                    if d is not None:
                        d.close()
                except Exception:
                    pass

        try:
            th = threading.Thread(target=run, daemon=True, name=f"settings-test-{mid}")
            th.start()
            th.join(timeout)
            if th.is_alive():
                res["lines"].append(f"No answer within {timeout:.0f} s - check the IP address, port and network")
                res["ok"] = False
        finally:
            self.test_lock.release()
        return {"ok": res["ok"], "lines": list(res["lines"])}

"""MTConnect driver (read-only, plain HTTP + XML, standard library only).

An MTConnect *agent* (on the machine's HMI PC, e.g. DMG MORI CELOS / MDC, Okuma, Mazak, or a separate adapter +
agent) answers `GET http://host:port/current` with the latest value of every data item. The monitor polls that
once per second - nothing is ever written to the machine.

Mapping to the normalized Sample (MTConnect standard names, any version 1.x/2.x):
  Availability   UNAVAILABLE -> offline
  Execution      ACTIVE -> RUNNING; INTERRUPTED / FEED_HOLD / STOPPED / OPTIONAL_STOP / PROGRAM_STOPPED -> PAUSED;
                 WAIT -> WAITING; READY -> IDLE; PROGRAM_COMPLETED -> IDLE + program_finished
  ControllerMode AUTOMATIC / SEMI_AUTOMATIC -> AUTO; MANUAL_DATA_INPUT -> MDI; MANUAL / EDIT -> MANUAL
  Program        subType MAIN (or no subType) -> main program; subType ACTIVE / ActiveProgram -> current program
  PartCount      part counter (subType ALL/GOOD preferred over the others)
  ToolNumber / ToolAssetId -> tool in the spindle; Block -> current block
  EmergencyStop  TRIGGERED -> alarm; Condition Fault / Warning entries -> read_alarms (display only)

Machines with several paths/controllers (lathes with two turrets): `path = "path1"` picks the component whose
name or id contains it; default = the first component that reports an Execution.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from ..model import ExecState, Mode, Sample
from .base import Driver, DriverError

EXEC = {"ACTIVE": ExecState.RUNNING, "INTERRUPTED": ExecState.PAUSED, "FEED_HOLD": ExecState.PAUSED,
        "STOPPED": ExecState.PAUSED, "OPTIONAL_STOP": ExecState.PAUSED, "PROGRAM_STOPPED": ExecState.PAUSED,
        "WAIT": ExecState.WAITING, "READY": ExecState.IDLE, "PROGRAM_COMPLETED": ExecState.IDLE}
MODE = {"AUTOMATIC": Mode.AUTO, "SEMI_AUTOMATIC": Mode.AUTO, "MANUAL_DATA_INPUT": Mode.MDI,
        "MANUAL": Mode.MANUAL, "EDIT": Mode.MANUAL}
COMMON_PORTS = (5000, 7878, 1391, 5001, 8082, 80)   # tried by --probe when the configured port doesn't answer


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_current(xml: bytes | str, device: str | None = None) -> list[dict]:
    """Flatten an MTConnectStreams document into rows
    {device, component, component_id, category, type, name, sub_type, id, value, attrs}."""
    root = ET.fromstring(xml)
    rows = []
    for ds in root.iter():
        if _local(ds.tag) != "DeviceStream":
            continue
        dname = ds.get("name") or ds.get("uuid") or ""
        if device and device not in (ds.get("name"), ds.get("uuid")):
            continue
        for cs in ds:
            if _local(cs.tag) != "ComponentStream":
                continue
            for cat in cs:
                c = _local(cat.tag)
                if c not in ("Samples", "Events", "Condition"):
                    continue
                for el in cat:
                    t = _local(el.tag)
                    if c == "Condition":
                        level, typ = t, el.get("type", "")
                    else:
                        level, typ = None, t
                    rows.append({"device": dname, "component": cs.get("name") or cs.get("component") or "",
                                 "component_id": cs.get("componentId", ""), "category": c, "type": typ,
                                 "name": el.get("name") or "", "sub_type": el.get("subType") or "",
                                 "id": el.get("dataItemId") or "", "value": (el.text or "").strip(),
                                 "level": level, "attrs": dict(el.attrib)})
    return rows


def _pick_path(rows, path):
    """Component ids of the controller path to follow (None = all)."""
    if path:
        p = str(path).lower()
        ids = {r["component_id"] for r in rows if p in r["component"].lower() or p in r["component_id"].lower()}
        return ids or None
    for r in rows:
        if r["type"] == "Execution":
            return {r["component_id"]}
    return None


def sample_from_rows(rows, path=None, part_sub=None) -> Sample:
    ids = _pick_path(rows, path)
    mine = [r for r in rows if ids is None or r["component_id"] in ids]

    def val(typ, rows_=mine, subs=None):
        for r in rows_:
            if r["type"] == typ and (subs is None or r["sub_type"].upper() in subs):
                v = r["value"]
                return None if v.upper() == "UNAVAILABLE" or v == "" else v
        return None

    avail = val("Availability", rows)
    if avail is None and any(r["type"] == "Availability" for r in rows):
        return Sample(ts=time.time(), online=False, exec_state=ExecState.OFFLINE)
    ex = (val("Execution") or "").upper()
    md = (val("ControllerMode") or "").upper()
    exec_state = EXEC.get(ex, ExecState.IDLE)
    estop = (val("EmergencyStop", rows) or "").upper() == "TRIGGERED"
    faults = [r for r in mine if r["category"] == "Condition" and r["level"] == "Fault"]
    if estop or (faults and exec_state != ExecState.RUNNING):
        exec_state = ExecState.ALARM if exec_state != ExecState.RUNNING else exec_state
    main = val("Program", subs={"MAIN", ""}) or val("Program")
    current = val("Program", subs={"ACTIVE"}) or val("ActiveProgram") or main
    pc = None
    for subs in ([[part_sub.upper()]] if part_sub else []) + [["ALL"], ["GOOD"], None]:
        v = val("PartCount", subs=set(subs) if subs else None)
        if v is not None:
            try:
                pc = int(float(v))
                break
            except ValueError:
                pass
    tool = val("ToolNumber") or val("ToolAssetId")
    return Sample(ts=time.time(), online=True, exec_state=exec_state, mode=MODE.get(md, Mode.UNKNOWN),
                  alarm=estop or bool(faults), main_program=main, current_program=current, part_count=pc,
                  program_finished=(ex == "PROGRAM_COMPLETED") or None, current_block=val("Block"),
                  tool=str(tool) if tool not in (None, "0") else None,
                  extra={"execution": ex or None, "mode": md or None, "availability": avail})


def _raw_peek(host, port, out, wait=4.0):
    """--probe only: is something listening that is NOT an HTTP agent? An MTConnect *adapter* speaks SHDR on a
    plain socket ('2026-11-13T08:00:00Z|execution|ACTIVE|...', answers '* PING' with '* PONG'). Read-only."""
    import socket
    try:
        sock = socket.create_connection((host, port), timeout=3)
    except OSError as e:
        out.append(f"   raw TCP {host}:{port}: {type(e).__name__}: {e}")
        return
    out.append(f"   raw TCP {host}:{port}: port is open - listening for {wait:.0f} s")
    data = b""
    try:
        sock.settimeout(wait)
        try:
            sock.sendall(b"* PING\n")
        except OSError:
            pass
        end = time.time() + wait
        while time.time() < end and len(data) < 4000:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    if not data:
        out.append("   nothing received (not an SHDR adapter, or it only talks after a request)")
        return
    lines = data.decode("latin-1", "replace").splitlines()
    kind = ("SHDR adapter (MTConnect adapter, not an agent)" if any("|" in l or l.startswith("* ") for l in lines)
            else "unknown protocol")
    out.append(f"   received {len(data)} bytes - looks like: {kind}")
    for l in lines[:25]:
        out.append("   | " + l[:160])


class MTConnectDriver(Driver):
    control = "mtconnect"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.base = None

    def _url(self, port=None, what="current"):
        host = self.cfg["host"]
        if str(host).startswith("http"):
            base = str(host).rstrip("/")
        else:
            base = f"http://{host}:{port or self.cfg.get('port', 5000)}"
        dev = self.cfg.get("device")
        return f"{base}/{dev}/{what}" if dev else f"{base}/{what}"

    def _get(self, url, timeout=None):
        req = urllib.request.Request(url, headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=timeout or self.cfg.get("timeout_s", 5)) as r:
            return r.read()

    def connect(self) -> None:
        try:
            self._rows = parse_current(self._get(self._url()), None)
        except (urllib.error.URLError, OSError, ET.ParseError) as e:
            raise DriverError(f"MTConnect agent not reachable at {self._url()}: {e}") from e
        self.base = self._url()

    def read(self) -> Sample:
        try:
            self._rows = parse_current(self._get(self._url()), None)
        except (urllib.error.URLError, OSError, ET.ParseError) as e:
            raise DriverError(f"MTConnect read failed: {e}") from e
        return sample_from_rows(self._rows, self.cfg.get("path"), self.cfg.get("part_count_subtype"))

    def read_alarms(self):
        rows = getattr(self, "_rows", None)
        if rows is None:
            return None
        out = []
        for r in rows:
            if r["category"] == "Condition" and r["level"] in ("Fault", "Warning"):
                code = r["attrs"].get("nativeCode") or r["type"]
                text = r["value"] or r["type"]
                out.append({"code": str(code), "text": f"{'' if r['level'] == 'Fault' else '(warning) '}{text}"})
        return out

    def probe(self, out) -> None:
        """--probe: find the agent, list the devices/data items it offers and what the monitor makes of them."""
        ports = [self.cfg.get("port", 5000)] + [p for p in COMMON_PORTS if p != self.cfg.get("port", 5000)]
        if str(self.cfg["host"]).startswith("http"):
            ports = [None]
        found = None
        for p in ports:
            url = self._url(p)
            try:
                data = self._get(url, timeout=3)
                out.append(f"agent answers at {url} ({len(data)} bytes)")
                found = (p, data)
                break
            except Exception as e:
                out.append(f"no agent at {url}: {type(e).__name__}: {e}")
                if p:
                    _raw_peek(self.cfg["host"], p, out)
        if not found:
            out.append("FAILED: no MTConnect agent found - check host/port, and that MTConnect is enabled on the machine")
            return
        p, data = found
        if p and p != self.cfg.get("port", 5000):
            out.append(f"-> set port = {p} for this machine in config.toml")
        try:
            hdr = ET.fromstring(data)
            h = next((e for e in hdr.iter() if _local(e.tag) == "Header"), None)
            if h is not None:
                out.append("header: " + ", ".join(f"{k}={h.get(k)}" for k in ("sender", "version", "instanceId")
                                                   if h.get(k)))
        except ET.ParseError as e:
            out.append(f"FAILED: answer is not XML: {e}: {data[:200]!r}")
            return
        rows = parse_current(data, None)
        devs = sorted({r["device"] for r in rows})
        out.append(f"devices: {devs}")
        out.append("data items (category type[subType] name @component = value):")
        for r in rows:
            st = f"[{r['sub_type']}]" if r["sub_type"] else ""
            lvl = f" {r['level']}" if r["level"] else ""
            out.append(f"  {r['category'][:4]} {r['type']}{st}{lvl} {r['name']} @{r['component']} = {r['value'][:80]!r}")
        s = sample_from_rows(rows, self.cfg.get("path"), self.cfg.get("part_count_subtype"))
        out.append("monitor reads: " + repr(s))
        self.cfg = {**self.cfg, "port": p} if p else self.cfg
        out.append("samples for 20 s (start/stop the machine or open the door to see changes):")
        last = None
        for _ in range(10):
            try:
                s = self.read()
                key = (s.exec_state, s.mode, s.main_program, s.current_program, s.part_count, s.tool, s.alarm)
                if key != last:
                    out.append(f"  {time.strftime('%H:%M:%S')} {s.exec_state.value} {s.mode.value} "
                               f"main={s.main_program} now={s.current_program} parts={s.part_count} "
                               f"tool={s.tool} alarm={s.alarm} alarms={self.read_alarms()}")
                    last = key
            except Exception as e:
                out.append(f"  read failed: {e}")
            time.sleep(2)

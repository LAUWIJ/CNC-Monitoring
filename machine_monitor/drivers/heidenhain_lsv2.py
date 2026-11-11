"""HEIDENHAIN TNC 530 / 620 / 640 / TNC7 via LSV2 (the protocol behind the DNC option).

Requires:  pip install pyLSV2      (open source, works on Windows + Linux)
Control:   LSV2 reachable on TCP 19000. Reading run status/program stack
           needs the DNC option (#18) on most TNC 640/620 versions.
           Uses the DNC login (needed for status/program stack). The driver only
           reads; it never sends programs or changes anything on the control.

Heidenhain is the easy one for program names: the control reports the
program stack separately:
   main    = program selected in "Program Run" (what the operator started)
   current = program executing right now (CALL PGM / PGM CALL sub-program)
and an explicit program status, including FINISHED (regular end, M2/M30)
and CANCELLED (internal stop / aborted).
"""
from __future__ import annotations

import time

from ..model import ExecState, Mode, Sample
from .base import Driver, DriverError

EXEC_NAMES = ["MANUAL", "MDI", "PASS_REFERENCES", "SINGLE_STEP", "AUTOMATIC", "UNDEFINED"]
PGM_NAMES = ["STARTED", "STOPPED", "FINISHED", "CANCELLED", "INTERRUPTED", "ERROR",
             "ERROR_CLEARED", "IDLE", "UNDEFINED"]


def _enum_name(val, names) -> str:
    if val is None:
        return "UNDEFINED"
    n = getattr(val, "name", None)
    if n:
        return str(n).upper()
    try:
        return names[int(val)]
    except (ValueError, IndexError, TypeError):
        return str(val).upper()


def _pick(obj, *names):
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


PROJECT_DIR = __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))


MARKER_BLOCK = 128   # markers per LSV2 request - stays inside ONE telegram (pyLSV2 splits bigger requests
                     # into groups, and on the DVF BMO's TNC 640 every group after the first came back with
                     # the wrong markers: PLC 279 "Machine guard is open" was missed, unused markers looked set)


def _read_markers(con, first: int, count: int):
    """Marker values M<first>..M<first+count-1>, read in blocks of MARKER_BLOCK."""
    out = []
    for lo in range(first, first + count, MARKER_BLOCK):
        n = min(MARKER_BLOCK, first + count - lo)
        v = _read_markers_once(con, lo, n)
        if v is None or len(v) != n:
            raise IOError(f"read M{lo}..M{lo + n - 1}: got {None if v is None else len(v)} of {n} values")
        out.extend(v)
    return out


def _read_markers_once(con, first: int, count: int):
    """Marker values M<first>..M<first+count-1> via LSV2 (pyLSV2 read_plc_memory, 1.x and 0.x)."""
    try:
        from pyLSV2.const import MemoryType
        mt = MemoryType.MARKER
    except Exception:
        try:
            import pyLSV2
            mt = getattr(pyLSV2, "PLC_MEM_TYPE_MARKER", 1)
        except ImportError:
            mt = 1
    return con.read_plc_memory(first, mt, count)


def _is_itnc(con):
    """True/False when pyLSV2 knows the control type, None when it can't tell."""
    for holder in (getattr(con, "versions", None), con):
        f = getattr(holder, "is_itnc", None) if holder is not None else None
        if f is None:
            continue
        try:
            return bool(f() if callable(f) else f)
        except Exception:
            return None
    return None


def _lsv2_status(err):
    """(code, name) of pyLSV2's last_error, e.g. (51, 'T_ER_NO_PRIV')."""
    if err is None:
        return None, ""
    code = getattr(err, "e_code", None)
    if code is None and isinstance(err, dict):
        code = err.get("e_code")
    try:
        code = int(code)
    except (TypeError, ValueError):
        return code, ""
    name = ""
    try:
        from pyLSV2.const import LSV2StatusCode
        name = LSV2StatusCode(code).name
    except Exception:
        pass
    return code, name


def _dispose(con) -> None:
    try:
        con.disconnect()
    except Exception:
        pass
    # pyLSV2 creates its TCP socket before connecting - close it explicitly, whatever state it is in
    for holder in (getattr(con, "_llcom", None), con):
        sock = getattr(holder, "_tcpsock", None) if holder is not None else None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class HeidenhainLsv2Driver(Driver):
    control = "heidenhain"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.con = None
        self.v1 = True
        self.last_alarm_raw = None
        self.no_error_list = False
        self._plc_msgs = None              # PLC message list (data/plc_<id>/messages.json)
        self._plc_mtime = None
        self._plc_checked = 0.0
        self._plc_built = False
        self._plc_fail = 0
        self.last_plc_raw = None     # not an iTNC 530, or the control answered T_ER_UNKNOWN_TELE

    def connect(self) -> None:
        try:
            import pyLSV2
        except ImportError as e:
            raise DriverError("pyLSV2 not installed: pip install pyLSV2") from e
        # pyLSV2's safe_mode blocks the 'DNC' login, which is required to read run status and
        # the program stack. This driver only ever calls read functions, so it is off by default.
        con = pyLSV2.LSV2(self.cfg["host"], port=self.cfg.get("port", 19000),
                          timeout=self.cfg.get("timeout_s", 5),
                          safe_mode=bool(self.cfg.get("lsv2_safe_mode", False)))
        self.close()
        try:
            con.connect()
        except Exception:
            _dispose(con)          # machine off: don't leave the socket of this attempt open
            raise
        self.con = con
        self.v1 = hasattr(con, "execution_state")   # pyLSV2 >= 1.0

    def read(self) -> Sample:
        con = self.con
        if self.v1:
            ex, pg, stack = con.execution_state(), con.program_status(), con.program_stack()
        else:                                           # pyLSV2 0.x
            ex, pg, stack = con.get_execution_mode(), con.get_program_status(), con.get_program_stack()
        if ex is None and pg is None:
            raise DriverError("no data from control (DNC option missing or connection lost?)")

        ex_n = _enum_name(ex, EXEC_NAMES)
        pg_n = _enum_name(pg, PGM_NAMES)

        mode = {"AUTOMATIC": Mode.AUTO, "SINGLE_STEP": Mode.AUTO, "MDI": Mode.MDI,
                "MANUAL": Mode.MANUAL, "PASS_REFERENCES": Mode.MANUAL}.get(ex_n, Mode.UNKNOWN)
        exec_state = {
            "STARTED": ExecState.RUNNING,
            "STOPPED": ExecState.PAUSED,        # NC-stop, M0, single-block wait
            "INTERRUPTED": ExecState.PAUSED,
            "ERROR": ExecState.ALARM,
            "ERROR_CLEARED": ExecState.PAUSED,
            "FINISHED": ExecState.IDLE,
            "CANCELLED": ExecState.IDLE,
            "IDLE": ExecState.IDLE,
        }.get(pg_n, ExecState.IDLE)

        main = cur = line = None
        if stack is not None:
            main = _pick(stack, "main", "main_pgm", "Main")
            cur = _pick(stack, "current", "current_pgm", "Current")
            line = _pick(stack, "line_no", "line", "Line")

        # A real abort (internal stop) always shows as CANCELLED. FINISHED can be so short (robot cells select
        # the next program right away) that a 1 s poll never sees it: STARTED -> IDLE is a normal end too.
        prev, self._prev_pg = getattr(self, "_prev_pg", None), pg_n
        finished = pg_n == "FINISHED" or (pg_n == "IDLE" and prev == "STARTED")

        return Sample(
            ts=time.time(), online=True, exec_state=exec_state, mode=mode,
            alarm=(pg_n == "ERROR"),
            main_program=main or None, current_program=cur or None,
            program_finished=finished,
            program_aborted=(pg_n == "CANCELLED"),
            extra={"exec_mode": ex_n, "pgm_status": pg_n, "line": line},
        )

    def read_alarms(self):
        """Active messages for the detail view: the NC error list (iTNC 530 only) + PLC messages from the
        machine builder's message table (markers), when that list was built from the copied PLC files."""
        if self.con is None:
            return None
        nc = self._read_nc_errors()
        plc = self._read_plc_messages()
        if nc is None and plc is None:
            return None
        return (nc or []) + (plc or [])

    # ------------------------------------------------------------- PLC messages (TNC 640 & co)
    def _plc_list(self):
        """messages.json from data/plc_<id>/ - reloaded when a new copy was made."""
        import os
        from .. import plc_messages
        now = time.time()
        if now - self._plc_checked < 30:
            return self._plc_msgs
        self._plc_checked = now
        dest = os.path.join(PROJECT_DIR, "data", f"plc_{self.cfg.get('id')}")
        path = os.path.join(dest, "messages.json")
        try:
            mt = os.path.getmtime(path)
        except OSError:
            # files copied, but no list yet (e.g. copied by an older version): build it once
            if os.path.exists(os.path.join(dest, "filelist.txt")) and not self._plc_built:
                self._plc_built = True
                try:
                    plc_messages.build(dest)
                    mt = os.path.getmtime(path)
                except Exception:
                    return None
            else:
                self._plc_msgs = None
                return None
        if mt != self._plc_mtime:
            self._plc_mtime = mt
            self._plc_msgs = plc_messages.load(dest)
            self._plc_fail = 0
        return self._plc_msgs

    def _read_plc_messages(self, force: bool = False):
        # on as soon as a message list exists (made by "Copy from control"); plc_messages = false switches off
        if not (force or self.cfg.get("plc_messages", True)):
            return None
        msgs = self._plc_list()
        if not msgs or self._plc_fail >= 3:
            return None
        lo = min(m["address"] for m in msgs)
        hi = max(m["address"] for m in msgs)
        try:
            vals = _read_markers(self.con, lo, hi - lo + 1)
        except Exception as e:
            self._plc_fail += 1
            self.last_plc_raw = f"read markers M{lo}..M{hi} failed: {type(e).__name__}: {e}"
            if self._plc_fail == 3:
                import logging
                logging.getLogger("heidenhain").info("[%s] PLC messages not readable: %s", self.cfg.get("id"), e)
            return None
        if vals is None or len(vals) < hi - lo + 1:
            self._plc_fail += 1
            self.last_plc_raw = f"read markers M{lo}..M{hi}: got {None if vals is None else len(vals)} values"
            return None
        self._plc_fail = 0
        out = [{"code": f"PLC {m['nr']}", "text": m["text"]} for m in msgs if vals[m["address"] - lo]]
        self.last_plc_raw = f"markers M{lo}..M{hi} read, {len(out)} active"
        return out

    def _read_nc_errors(self):
        """Active error messages from the control's error window (LSV2 R_RI / NC error list).
        Display only. Returns None when this pyLSV2 version / control can't provide them."""
        con = self.con
        if con is None or self.no_error_list:
            return None
        # The LSV2 error-list request only exists on the iTNC 530. TNC 640/620/320/TNC7 answer
        # T_ER_UNKNOWN_TELE, so don't even ask them (error texts there need OPC UA or the RemoTools SDK).
        itnc = _is_itnc(con)
        if itnc is False:
            self.no_error_list = True
            self.last_alarm_raw = "not an iTNC 530: the error list can't be read over LSV2 - skipped"
            return None
        fn = getattr(con, "get_error_messages", None)
        if fn is None:
            return None
        msgs = fn()
        # pyLSV2 returns an empty list both for "no errors" and for "the control refused the request":
        # tell them apart via the LSV2 status of the last request.
        code, name = _lsv2_status(getattr(con, "last_error", None))
        self.last_alarm_raw = f"{msgs!r} | LSV2 status: {code} {name}"
        if not msgs and code not in (None, 0) and "NO_FIRST_ERROR" not in name and "NO_NEXT_ERROR" not in name:
            if "UNKNOWN_TELE" in name:     # this control doesn't know the request at all: stop asking
                self.no_error_list = True
            return None                    # refused / not supported -> "can't read", not "no alarms"
        if msgs is None or msgs is False:
            return None
        if isinstance(msgs, dict) or not isinstance(msgs, (list, tuple)):
            msgs = [msgs]
        out = []
        for m in msgs:
            num = _pick(m, "e_number", "Number", "number")
            text = _pick(m, "e_text", "Text", "text")
            cls = _pick(m, "e_class", "Class", "class_")
            if num in (None, "", 0) and not text:
                continue
            code = str(num) if num not in (None, "") else ""
            cname = _enum_name(cls, []) if cls is not None else ""
            if cname and not cname.isdigit() and cname not in ("NONE", "UNDEFINED"):
                code = f"{code} {cname.title()}".strip()
            out.append({"code": code, "text": str(text or "").strip()})
        return out

    def probe_extra(self, out) -> None:
        """--probe only: check that PLC markers are read at the right addresses.
        Reads well-known NC->PLC markers (ApiMarker.def: program running, feed hold, ...) whose state is
        obvious on the machine, plus the PLC message markers, and prints the LSV2 memory layout."""
        import os
        from .. import plc_messages
        con = self.con
        dest = os.path.join(PROJECT_DIR, "data", f"plc_{self.cfg.get('id')}")
        prog = os.path.join(dest, "plc", "program")
        if not os.path.isdir(prog):
            out.append("PLC check: no PLC files copied yet (detail view -> Copy from control)")
            return
        try:
            _read_markers(con, 0, 1)                    # makes pyLSV2 load the memory layout
        except Exception as e:
            out.append(f"PLC check: reading markers failed: {type(e).__name__}: {e}")
            return
        sp = getattr(con, "_sys_par", None)
        if sp is not None:
            out.append("LSV2 memory layout: " + ", ".join(
                f"{k}={getattr(sp, k, '?')}" for k in ("lsv2_version", "markers_start_address", "number_of_markers",
                                                     "inputs_start_address", "number_of_inputs",
                                                     "outputs_start_address", "words_start_address",
                                                     "number_of_words")))
        api = {}
        for f in ("ApiMarker.def", "Api4Marker.def"):
            api.update(plc_messages.parse_def_files([os.path.join(prog, f)]))
        if api:
            addrs = sorted(set(api.values()))
            vals = {}
            i = 0
            while i < len(addrs):                       # read in blocks of up to 200 markers
                lo = addrs[i]
                j = i
                while j + 1 < len(addrs) and addrs[j + 1] - lo < 200:
                    j += 1
                hi = addrs[j]
                try:
                    v = _read_markers(con, lo, hi - lo + 1) or []
                    for k, x in enumerate(v):
                        vals[lo + k] = x
                except Exception as e:
                    out.append(f"  read M{lo}..M{hi} failed: {e}")
                i = j + 1
            on = sorted((a, n) for n, a in api.items() if vals.get(a))
            out.append(f"NC/PLC interface markers ({len(api)} known): {len(on)} set right now:")
            for a, n in on[:120]:
                out.append(f"  M{a:<6} {n}")
        # snapshot of ALL markers + difference with the previous probe run (e.g. door closed -> door open)
        try:
            import json
            n = int(getattr(sp, "number_of_markers", 0) or 0) if sp is not None else 0
            n = n if 0 < n <= 100000 else 20000
            allv = list(_read_markers(con, 0, n))
            on = [i for i, v in enumerate(allv) if v]
            snap = os.path.join(dest, "marker_snapshot_v2.json")
            prev = None
            try:
                with open(snap) as f:
                    prev = json.load(f)
            except (OSError, ValueError):
                pass
            with open(snap, "w") as f:
                json.dump({"ts": time.time(), "on": on}, f)
            out.append(f"all markers M0..M{n - 1}: {len(on)} set (snapshot saved)")
            if prev:
                a, b = set(prev.get("on", [])), set(on)
                age = time.time() - prev.get("ts", 0)
                out.append(f"changed since the previous probe ({age:.0f} s ago):")
                out.append(f"  went ON : {' '.join(f'M{x}' for x in sorted(b - a)[:80]) or '-'}")
                out.append(f"  went OFF: {' '.join(f'M{x}' for x in sorted(a - b)[:80]) or '-'}")
        except Exception as e:
            out.append(f"marker snapshot failed: {type(e).__name__}: {e}")
        msgs = self._plc_list()
        if msgs:
            act = self._read_plc_messages(force=True) or []
            out.append(f"PLC message markers: {self.last_plc_raw}")
            for m in act:
                out.append(f"  {m['code']}: {m['text']}")
        try:
            out.append(f"machine right now: {self.read().extra}")
        except Exception:
            pass

    def close(self) -> None:
        if self.con is not None:
            _dispose(self.con)
            self.con = None

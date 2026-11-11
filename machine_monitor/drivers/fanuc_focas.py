"""FANUC 0i-D/F, 16i/18i/21i, 30i/31i/32i via FOCAS2 over Ethernet.

Requires:  the FOCAS2 library DLLs from FANUC (Fwlib32.dll + the fwlib*.dll
           companions, or Fwlib64.dll for 64-bit Python). They are not on pip:
           they come with the FANUC FOCAS2 library / machine builder. Point
           `fwlib_path` at the DLL; the companion DLLs must sit next to it.
           32-bit DLL  -> 32-bit Python.   Fwlib64.dll -> 64-bit Python.
           (Linux: libfwlib32.so works too.)
Control:   Embedded Ethernet, FOCAS2 port (parameter 14880 area, default 8193).

Why FANUC needs care: cnc_exeprgname() returns the program executing *now*.
During an M-code macro (e.g. M6 -> O9001), a G65 macro or an M98 sub it
returns that program. So we read the MAIN program separately:
   - cnc_pdf_rdmain()  -> main program path (30i/31i/0i-F with program folders)
   - cnc_rdprgnum()    -> 'mdata' = main program number (all series)
and use the part counter (#3901, counted at M30/M2 or the M-code set in
parameter 6710) as the "part finished" signal.
"""
from __future__ import annotations

import ctypes as C
import os
import sys
import time

from ..model import ExecState, Mode, Sample
from .base import Driver, DriverError

EW_OK = 0
EW_SOCKET = -16
EW_HANDLE = -8


class ODBST(C.Structure):
    _fields_ = [("hdck", C.c_short), ("tmmode", C.c_short), ("aut", C.c_short), ("run", C.c_short),
                ("motion", C.c_short), ("mstb", C.c_short), ("emergency", C.c_short),
                ("alarm", C.c_short), ("edit", C.c_short)]


class ODBPRO_S(C.Structure):          # standard 4-digit O-numbers
    _fields_ = [("dummy", C.c_short * 2), ("data", C.c_short), ("mdata", C.c_short)]


class ODBPRO_L(C.Structure):          # 8-digit O-number option (ONO8D)
    _fields_ = [("dummy", C.c_short * 2), ("data", C.c_int32), ("mdata", C.c_int32)]


class ODBEXEPRG(C.Structure):
    _fields_ = [("name", C.c_char * 36), ("o_num", C.c_int32)]


class ODBM(C.Structure):
    _fields_ = [("datano", C.c_short), ("dummy", C.c_short), ("mcr_val", C.c_int32), ("dec_val", C.c_short)]


class ODBSEQ(C.Structure):         # cnc_rdseqnum: sequence number (N) being executed
    _fields_ = [("dummy", C.c_short * 2), ("data", C.c_int32)]


class ODBALMMSG2(C.Structure):     # cnc_rdalmmsg2 (0i-D/F, 30i series): 64-char message
    _fields_ = [("alm_no", C.c_int32), ("type", C.c_short), ("axis", C.c_short), ("dummy", C.c_short),
                ("msg_len", C.c_short), ("alm_msg", C.c_char * 64)]


class ODBALMMSG(C.Structure):      # cnc_rdalmmsg (older 16i/18i/21i): 32-char message
    _fields_ = [("alm_no", C.c_int32), ("type", C.c_short), ("axis", C.c_short), ("dummy", C.c_short),
                ("msg_len", C.c_short), ("alm_msg", C.c_char * 32)]


class OPMSG(C.Structure):          # cnc_rdopmsg: operator message (#3006, PMC external messages)
    _fields_ = [("datano", C.c_short), ("type", C.c_short), ("char_num", C.c_short), ("data", C.c_char * 256)]


# alarm type -> prefix as shown on the control's alarm screen (30i / 0i-F numbering)
ALARM_PREFIX = {0: "SW", 1: "PW", 2: "IO", 3: "PS", 4: "OT", 5: "OH", 6: "SV", 7: "SR", 8: "MC", 9: "SP",
                10: "DS", 11: "IE", 12: "BG", 13: "SN", 15: "EX", 19: "PC"}
MAX_ALARMS = 10


def _ftext(raw: bytes, n: int = None) -> str:
    if n is not None and 0 < n <= len(raw):
        raw = raw[:n]
    return raw.split(b"\0", 1)[0].decode("latin-1", errors="replace").strip()


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LINUX_LIBS: dict = {}


def resolve_library(path):
    """One config.toml for Windows AND the Raspberry Pi: on Windows fwlib_path (the DLL) is used;
    on Linux a Windows path is ignored and the right libfwlib32-linux-<arch>.so* is taken from the
    FOCAS folder in the project (or set fwlib_path to a .so file explicitly)."""
    if sys.platform == "win32":
        if path:
            return path
        return "Fwlib64.dll" if sys.maxsize > 2**32 else "Fwlib32.dll"
    if path and not path.lower().endswith(".dll") and ":\\" not in path and os.path.exists(path):
        return path
    import glob
    import platform
    # NB: platform.machine() reports the KERNEL. A Pi 4 runs a 64-bit kernel (aarch64) even under
    # 32-bit Raspberry Pi OS, so whether Python itself is 32- or 64-bit decides.
    m = platform.machine().lower()
    py64 = sys.maxsize > 2**32
    if m.startswith(("aarch64", "arm")):
        if py64:
            raise DriverError("this is a 64-bit ARM system: the FANUC library only exists for 32-bit ARM - "
                              "install Raspberry Pi OS (32-bit)")
        arch = "armv7"
    elif m in ("x86_64", "amd64", "i686", "i386"):
        arch = "x64" if py64 else "x86"
    else:
        arch = m
    found = sorted(glob.glob(os.path.join(PROJECT_DIR, "FOCAS", f"libfwlib32-linux-{arch}.so*")))
    if not found:
        raise DriverError(f"no FANUC library for {m} found in {os.path.join(PROJECT_DIR, 'FOCAS')} "
                          f"(expected libfwlib32-linux-{arch}.so.*)")
    return found[-1]      # highest version


def _linux_lib(path):
    """Load once per process; cnc_startupprocess must be called only once."""
    if path not in _LINUX_LIBS:
        lib = C.CDLL(path)
        log = os.path.join(PROJECT_DIR, "data", "focas.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        lib.cnc_startupprocess.argtypes = [C.c_long, C.c_char_p]
        lib.cnc_startupprocess(0, log.encode())
        _LINUX_LIBS[path] = lib
    return _LINUX_LIBS[path]


class FanucFocasDriver(Driver):
    control = "fanuc"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.lib = None
        self.h = C.c_ushort(0)
        self.connected = False
        self.supports_pdf = True
        self.supports_exeprgname = True
        self.long_onum = bool(cfg.get("o8_digit_program_numbers", False))
        self.part_macro = int(cfg.get("part_count_macro", 3901))   # 0 = don't read
        self.read_opmsg = bool(cfg.get("read_operator_messages", True))
        # N-number being executed, shown on the enlarged card. One extra small call per poll -> off by default.
        self.supports_seq = bool(cfg.get("read_sequence_number", False))

    def _load(self):
        if self.lib is not None:
            return
        path = resolve_library(self.cfg.get("fwlib_path"))
        try:
            if sys.platform == "win32":
                d = os.path.dirname(os.path.abspath(path))
                if os.path.isdir(d):
                    # Fwlib64.dll loads the CNC-series DLLs (fwlib30i64.dll, fwlib0iD64.dll,
                    # fwlibe64.dll, ...) by name only when connecting. Windows does not look in
                    # Fwlib64's own folder for those, so: add the folder to the search path AND
                    # pre-load them; a DLL that is already loaded is found by name.
                    if hasattr(os, "add_dll_directory"):
                        os.add_dll_directory(d)
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                    is64 = sys.maxsize > 2**32
                    main = os.path.basename(path).lower()
                    for f in sorted(os.listdir(d)):
                        fl = f.lower()
                        if not (fl.startswith("fwlib") and fl.endswith(".dll")) or fl == main:
                            continue
                        if fl.endswith("64.dll") != is64 or fl in ("fwlib32.dll",):
                            continue
                        try:
                            C.WinDLL(os.path.join(d, f))
                        except OSError:
                            pass
                self.lib = C.WinDLL(path)
            else:
                self.lib = _linux_lib(path)
        except OSError as e:
            raise DriverError(f"cannot load FOCAS library '{path}': {e}. "
                              "Check fwlib_path and 32/64-bit match with Python.") from e
        L = self.lib
        L.cnc_allclibhndl3.argtypes = [C.c_char_p, C.c_ushort, C.c_long, C.POINTER(C.c_ushort)]
        L.cnc_allclibhndl3.restype = C.c_short
        L.cnc_freelibhndl.argtypes = [C.c_ushort]
        L.cnc_statinfo.argtypes = [C.c_ushort, C.POINTER(ODBST)]
        L.cnc_statinfo.restype = C.c_short
        L.cnc_rdprgnum.argtypes = [C.c_ushort, C.c_void_p]
        L.cnc_rdprgnum.restype = C.c_short
        L.cnc_rdmacro.argtypes = [C.c_ushort, C.c_short, C.c_short, C.POINTER(ODBM)]
        L.cnc_rdmacro.restype = C.c_short
        for fn, argt in (("cnc_rdalmmsg2", [C.c_ushort, C.c_short, C.POINTER(C.c_short), C.c_void_p]),
                         ("cnc_rdalmmsg", [C.c_ushort, C.c_short, C.POINTER(C.c_short), C.c_void_p]),
                         ("cnc_rdopmsg", [C.c_ushort, C.c_short, C.c_short, C.c_void_p])):
            if hasattr(L, fn):
                getattr(L, fn).argtypes = argt
                getattr(L, fn).restype = C.c_short
        if hasattr(L, "cnc_rdseqnum"):
            L.cnc_rdseqnum.argtypes = [C.c_ushort, C.POINTER(ODBSEQ)]
            L.cnc_rdseqnum.restype = C.c_short
        else:
            self.supports_seq = False
        for fn in ("cnc_exeprgname", "cnc_pdf_rdmain"):
            if not hasattr(L, fn):
                setattr(self, "supports_" + ("exeprgname" if fn == "cnc_exeprgname" else "pdf"), False)

    def connect(self) -> None:
        self._load()
        self.close()
        ret = self.lib.cnc_allclibhndl3(self.cfg["host"].encode(), int(self.cfg.get("port", 8193)),
                                        int(self.cfg.get("timeout_s", 5)), C.byref(self.h))
        if ret == -15:   # EW_NODLL
            raise DriverError("cnc_allclibhndl3 failed (-15 EW_NODLL): the DLL for this CNC series could not "
                              "be loaded - all fwlib*64.dll files must be in the same folder as Fwlib64.dll")
        if ret != EW_OK:
            why = {-16: "no answer from the CNC - check IP, port 8193, cable/switch, FOCAS/Ethernet enabled on the control",
                   -8: "invalid handle", -7: "version mismatch between CNC and library",
                   -6: "unexpected error", -5: "system error", -2: "CNC is busy (reset)",
                   -1: "CNC busy / other FOCAS client connected", -3: "library handle error"}.get(ret, "")
            raise DriverError(f"cnc_allclibhndl3 failed ({ret}) {why}".strip())
        self.connected = True

    def _check(self, ret: int, fn: str):
        if ret in (EW_SOCKET, EW_HANDLE):
            self.connected = False
            raise DriverError(f"{fn}: connection lost ({ret})")
        return ret == EW_OK

    def read(self) -> Sample:
        L, h = self.lib, self.h.value
        st = ODBST()
        if not self._check(L.cnc_statinfo(h, C.byref(st)), "cnc_statinfo"):
            raise DriverError("cnc_statinfo failed")

        # run: 0 reset, 1 STOP (M0/M1/single block), 2 HOLD (feed hold), 3 STRT, 4 MSTR
        exec_state = {0: ExecState.IDLE, 1: ExecState.PAUSED, 2: ExecState.PAUSED,
                      3: ExecState.RUNNING, 4: ExecState.RUNNING}.get(st.run, ExecState.IDLE)
        # aut: 0 MDI, 1 MEM, 3 EDIT, 4 HND, 5 JOG, 6 TJOG, 7 THND, 8 INC, 9 REF, 10 RMT
        mode = Mode.AUTO if st.aut in (1, 10) else (Mode.MDI if st.aut == 0 else Mode.MANUAL)
        alarm = bool(st.alarm) or bool(st.emergency)
        self._st_alarm, self._st_emg = bool(st.alarm), bool(st.emergency)   # used by read_alarms

        # program numbers: running + main
        pro = ODBPRO_L() if self.long_onum else ODBPRO_S()
        run_no = main_no = None
        if self._check(L.cnc_rdprgnum(h, C.byref(pro)), "cnc_rdprgnum"):
            run_no, main_no = int(pro.data), int(pro.mdata)

        current = f"O{run_no:04d}" if run_no else None
        if self.supports_exeprgname:
            ep = ODBEXEPRG()
            ret = L.cnc_exeprgname(h, C.byref(ep))
            if self._check(ret, "cnc_exeprgname"):
                name = ep.name.decode(errors="ignore").strip("\x00 ").strip()
                current = name or current
            elif ret in (1, 6):           # EW_FUNC / EW_NOOPT
                self.supports_exeprgname = False

        main = f"O{main_no:04d}" if main_no else None
        if self.supports_pdf:
            buf = C.create_string_buffer(256)
            ret = L.cnc_pdf_rdmain(h, buf)
            if self._check(ret, "cnc_pdf_rdmain"):
                p = buf.value.decode(errors="ignore").strip()
                main = p or main
            elif ret in (1, 6, -1):
                self.supports_pdf = False

        # DNC (aut 10 = RMT): the program runs from the memory card / PC, but rdprgnum.mdata and pdf_rdmain
        # still report the program SELECTED in memory mode (seen on a Puma 2600LY: kept showing the memory-mode program while running
        # a DNC file). Then the main program is unknown here - the engine latches it from the executing
        # program at cycle start (sub-programs / macros excluded as usual).
        dnc = st.aut == 10
        if dnc:
            main = None
            current = current or "DNC"

        seq = None
        if self.supports_seq:
            sq = ODBSEQ()
            ret = L.cnc_rdseqnum(h, C.byref(sq))
            if ret == 0:
                seq = int(sq.data) or None
            elif ret in (1, 6):
                self.supports_seq = False

        parts = None
        if self.part_macro:
            m = ODBM()
            if self._check(L.cnc_rdmacro(h, self.part_macro, 10, C.byref(m)), "cnc_rdmacro"):
                parts = int(round(m.mcr_val / (10 ** m.dec_val))) if m.dec_val >= 0 else int(m.mcr_val)

        return Sample(
            ts=time.time(), online=True, exec_state=exec_state, mode=mode, alarm=alarm,
            main_program=main, current_program=current, part_count=parts,
            extra={"run": st.run, "aut": st.aut, "motion": st.motion, "alarm": st.alarm,
                   "emergency": st.emergency, "running_no": run_no, "main_no": main_no, "seq_no": seq,
                   "dnc": dnc},
        )

    # ------------------------------------------------------------ alarms
    def _alarm_msgs(self, fn: str, struct, typ: int):
        """One cnc_rdalmmsg(2) call -> (ret, [{code, text}])."""
        L, h = self.lib, self.h.value
        buf = (struct * MAX_ALARMS)()
        num = C.c_short(MAX_ALARMS)
        ret = getattr(L, "cnc_" + fn)(h, typ, C.byref(num), buf)
        out = []
        if self._check(ret, "cnc_" + fn):
            for a in buf[:max(0, min(num.value, MAX_ALARMS))]:
                code = f"{ALARM_PREFIX.get(a.type, f'T{a.type}')}{a.alm_no:04d}"
                text = _ftext(bytes(a.alm_msg), a.msg_len)
                if a.axis > 0 and text and f"({a.axis})" not in text:
                    text += f" (axis {a.axis})"
                out.append({"code": code, "text": text})
        return ret, out

    def read_alarms(self):
        """Active CNC alarms (cnc_rdalmmsg2 / cnc_rdalmmsg) + operator messages (cnc_rdopmsg) + emergency stop.
        Display only. Returns None if nothing could be read. Return codes go to last_alarm_raw (--probe)."""
        if self.lib is None or not self.connected:
            return None
        L, h = self.lib, self.h.value
        out, got, raw = [], False, []
        flag = getattr(self, "_st_alarm", False)       # cnc_statinfo says an alarm is active
        for fn, struct in (("rdalmmsg2", ODBALMMSG2), ("rdalmmsg", ODBALMMSG)):
            if not hasattr(L, "cnc_" + fn):
                continue
            ret, msgs = self._alarm_msgs(fn, struct, -1)          # -1 = all alarm types
            raw.append(f"{fn}(all)={ret}/{len(msgs)}")
            if ret == EW_OK and (msgs or not flag):
                got = True
                out += msgs
                break
            if flag:
                # some series don't accept "all types" (or return nothing for it): ask per alarm type
                per, ok_any = [], False
                for t in sorted(ALARM_PREFIX):
                    r, m = self._alarm_msgs(fn, struct, t)
                    ok_any = ok_any or r == EW_OK
                    per += m
                raw.append(f"{fn}(per type)={'ok' if ok_any else 'error'}/{len(per)}")
                if ok_any:
                    got = True
                    out += per
                    if per:
                        break
        if self.read_opmsg and hasattr(L, "cnc_rdopmsg"):
            msgs = (OPMSG * 5)()
            ret = L.cnc_rdopmsg(h, -1, C.sizeof(msgs), msgs)
            raw.append(f"rdopmsg={ret}")
            if self._check(ret, "cnc_rdopmsg"):
                got = True
                for m in msgs:
                    text = _ftext(bytes(m.data), m.char_num)
                    if m.datano < 0 or not text:
                        continue
                    out.append({"code": f"MSG {m.datano}" if m.datano else "MSG", "text": text})
            elif ret in (1, 6):              # EW_FUNC / EW_NOOPT: not supported here
                self.read_opmsg = False
        if getattr(self, "_st_emg", False) and not any("EMERG" in (a["text"] or "").upper() for a in out):
            out.insert(0, {"code": "EMG", "text": "Emergency stop"})
            got = True
        self.last_alarm_raw = f"statinfo alarm={int(flag)} emergency={int(getattr(self, '_st_emg', False))} | " + \
            ", ".join(raw)
        return out if got else None       # None = nothing could be read this time

    def close(self) -> None:
        if self.lib is not None and self.connected:
            try:
                self.lib.cnc_freelibhndl(self.h.value)
            except Exception:
                pass
        self.connected = False

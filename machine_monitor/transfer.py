"""Program transfer between the dashboard and the controls (send to / fetch from the CNC).

Every operation opens its OWN connection to the control (never the monitoring connection), does one thing
and closes it again, so a transfer can never disturb or stall the status reading.

Per control:
  FANUC      FOCAS program-folder functions: cnc_rdpdf_drive / cnc_rdpdf_alldir (list), cnc_upstart4 /
             cnc_upload4 (fetch), cnc_dwnstart4 / cnc_download4 (send), cnc_pdf_del (replace).
             Drives: CNC memory (//CNC_MEM/USER/PATH1/) and, when the control has one, the memory card
             (//CF_TEXT/ on the Puma 2600LY - every drive the control reports is offered) - the card is where
             programs for DNC operation from the card live.
             DNC memory card (memory_card = "dnc", Puma 2600LY / 0i-D): NOT through the folder functions (opening
             //CF_TEXT/ that way crashed the control). Listed with cnc_rdmcdfinfo, fetched / sent with the exact
             messages FANUC's Program Transfer Tool uses (machine_monitor/fanuc_card.py). Shown as "card:/".
  HEIDENHAIN pyLSV2: directory listing, recive_file / send_file. NC files (.h .i ...) in text mode, so an
             iTNC 530 converts them to its internal format; everything else binary.
  SIEMENS    SINUMERIK OPC UA file system: browse Sinumerik/FileSystem, methods CopyFileFromServer and
             CopyFileToServer. The OPC UA user needs file-system read/write rights on the control.
  simulator  a folder on the monitor (data/demo_files/<id>/) - for the demo and the tests.

Safety (always, whatever the browser sends):
  * sending is off unless the machine has  transfer = "send"  in config.toml (default: browse + fetch only)
  * the program that is selected / running on the control is never replaced
  * protected programs (default FANUC O8000-O9999 macros) are never replaced
  * a replaced program is first copied to data/transfer_backup/<id>/ on the monitor - limited: the last 3
    versions per program, max 30 days, max 200 MB in total ([transfer] backup_keep / backup_days / backup_max_mb)
  * optional PIN for sending ([transfer] pin = "1234")
  * every send / fetch is written to the transfer history
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

log = logging.getLogger("transfer")

NC_TEXT_EXT = {".h", ".i", ".d", ".t", ".tab", ".tch", ".p", ".pnt", ".cma", ".a", ".hu", ".hc", ".nc", ".txt"}
NAME_RE = re.compile(r"^[A-Za-z0-9_\-. ]{1,64}$")
DEFAULT_PROTECT = {"fanuc": [r"^O?[89]\d{3}$"], "heidenhain": [], "siemens": [], "simulator": [r"^O?9\d{3}$"]}


class TransferError(Exception):
    pass


def base_name(p) -> str:
    """'//CNC_MEM/USER/PATH1/O0123' / 'TNC:\\nc_prog\\a.h' / 'Part Program/X.MPF' -> comparable name."""
    if not p:
        return ""
    n = re.split(r"[\\/]", str(p).strip())[-1].strip().upper()
    m = re.fullmatch(r"O0*(\d+)", n)
    return f"O{int(m.group(1))}" if m else n


def join(folder: str, name: str) -> str:
    if folder.endswith(("/", "\\")):
        return folder + name
    return folder + ("\\" if "\\" in folder else "/") + name


# =====================================================================================================
# File sessions - one per operation, each with its own connection
# =====================================================================================================
class FileSession:
    """roots() -> [{"path","label","note"}]; list(folder) -> [{"name","dir","size","mtime","comment"}];
    fetch(path, local_file, progress); send(local_file, folder, name, progress); delete(path)."""

    def __init__(self, mcfg: dict):
        self.cfg = mcfg

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        try:
            self.close()
        except Exception:
            pass

    def open(self):
        pass

    def close(self):
        pass

    def roots(self) -> list:
        raise NotImplementedError

    def list(self, folder: str) -> list:
        raise NotImplementedError

    def fetch(self, path: str, local: str, progress=None) -> int:
        raise NotImplementedError

    def send(self, local: str, folder: str, name: str, progress=None) -> None:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        raise NotImplementedError


# ------------------------------------------------------------------------------------------- demo
class DemoFiles(FileSession):
    """A folder on the monitor that behaves like the control's file system (demo + tests)."""

    ROOTS = {
        "fanuc": [("//CNC_MEM/USER/PATH1/", "CNC memory", ""),
                  ("//CF_TEXT/", "Memory card", "DNC operation from the card")],
        "heidenhain": [("TNC:\\nc_prog\\", "NC programs", "")],
        "siemens": [("Sinumerik/FileSystem/Part Program/", "Part programs", ""),
                    ("Sinumerik/FileSystem/Sub Program/", "Subprograms", ""),
                    ("Sinumerik/FileSystem/Work Pieces/", "Workpieces", "")],
    }
    SAMPLE = {"fanuc": {"//CNC_MEM/USER/PATH1/": ["O1001", "O1234", "O9022"], "//CF_TEXT/": ["BIGPART.NC"]},
              "heidenhain": {"TNC:\\nc_prog\\": ["CLEAN_VISE.H", "FLANGE_OP10.H"]},
              "siemens": {"Sinumerik/FileSystem/Part Program/": ["HOUSING_A.MPF"],
                          "Sinumerik/FileSystem/Sub Program/": ["L100.SPF"]}}

    def __init__(self, mcfg: dict, data_dir: str):
        super().__init__(mcfg)
        self.flavor = mcfg.get("flavor", "fanuc") if mcfg.get("flavor") in self.ROOTS else "fanuc"
        self.base = os.path.join(data_dir, "demo_files", mcfg["id"])
        if not os.path.isdir(self.base):
            for folder, names in self.SAMPLE[self.flavor].items():
                for n in names:
                    lp = self._local(join(folder, n))
                    os.makedirs(os.path.dirname(lp), exist_ok=True)
                    with open(lp, "w", newline="\n") as f:
                        f.write(f"%\n{n} (DEMO PROGRAM)\nG90 G54\nM30\n%\n")
        for r, _, _ in self.ROOTS[self.flavor]:
            os.makedirs(self._local(r), exist_ok=True)

    def _local(self, path: str) -> str:
        parts = [p for p in re.split(r"[\\/:]+", path) if p and p not in (".", "..")]
        return os.path.join(self.base, *parts)

    def roots(self):
        return [{"path": p, "label": l, "note": n} for p, l, n in self.ROOTS[self.flavor]]

    def list(self, folder):
        d = self._local(folder)
        if not os.path.isdir(d):
            raise TransferError(f"folder not found: {folder}")
        out = []
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            st = os.stat(p)
            out.append({"name": n, "dir": os.path.isdir(p), "size": None if os.path.isdir(p) else st.st_size,
                        "mtime": st.st_mtime})
        return out

    def fetch(self, path, local, progress=None):
        src = self._local(path)
        if not os.path.isfile(src):
            raise TransferError(f"file not found: {path}")
        shutil.copyfile(src, local)
        return os.path.getsize(local)

    def send(self, local, folder, name, progress=None):
        d = self._local(folder)
        if not os.path.isdir(d):
            raise TransferError(f"folder not found: {folder}")
        time.sleep(min(2.0, os.path.getsize(local) / 2e6))     # feel of a real transfer
        shutil.copyfile(local, os.path.join(d, name))

    def delete(self, path):
        os.remove(self._local(path))


# ------------------------------------------------------------------------------------------- FANUC
class FanucFiles(FileSession):
    """FOCAS program folders. Needs a 30i/31i/0i-D/0i-F type control (program folder functions)."""
    EW_BUFFER, EW_RESET = 10, -2
    CHUNK = 1280

    CARD = "card:/"                               # DNC memory card (memory_card = "dnc")
    CARD_DRIVES = {"CF_TEXT"}                     # never opened with the folder functions on such a machine

    def open(self):
        import ctypes as C
        self.C, self.drv = C, None
        self._lib()

    def _lib(self):
        """FOCAS library handle, opened on first use (closed again before a memory card transfer, which runs
        on its own connection - so the control never sees more connections than FANUC's own tool uses)."""
        if self.drv is None:
            from .drivers import fanuc_focas as F
            self.F = F
            self.drv = F.FanucFocasDriver(dict(self.cfg))
            self.drv.connect()                   # its own FOCAS handle, separate from the monitor's
            self.L, self.h = self.drv.lib, self.drv.h.value
            self._setup()
        return self.L

    def close(self):
        try:
            if self.drv is not None:
                self.drv.close()
        except Exception:
            pass
        self.drv = None

    # ---- DNC memory card
    def has_card(self) -> bool:
        return str(self.cfg.get("memory_card", "")).lower() == "dnc"

    def is_card(self, path: str) -> bool:
        return self.has_card() and str(path).lower().startswith(self.CARD)

    def _guard(self, path: str):
        """Folder functions on the card drive of a DNC-card machine are what crashed the 2600LY - refuse."""
        if self.has_card():
            drive = str(path).strip("/").split("/")[0].upper()
            if drive in self.CARD_DRIVES:
                raise TransferError("the memory card of this machine is only reachable through 'Memory card'")

    @staticmethod
    def _card_name(path: str) -> str:
        n = str(path)[len(FanucFiles.CARD):].strip("/")
        if not n or "/" in n or not NAME_RE.match(n):
            raise TransferError("not a memory card file name")
        return n

    def overwrites(self, folder: str) -> bool:
        """The card replaces a file in one step (send with '10000:'), no delete - like FANUC's own tool."""
        return self.is_card(folder)

    def card_list(self) -> list:
        """cnc_rdmcdfinfo: entry 1, then -1 = next, until an empty entry (as FANUC's own tool does).
        Verified read-only on the Puma 2600LY 2026-11-11."""
        C = self.C
        L = self._lib()
        for fn in ("cnc_rdmcdfinfo", "cnc_canmcdfinfo"):
            if not hasattr(L, fn):
                raise TransferError(f"{fn} not in this FOCAS library - memory card not supported")
        L.cnc_rdmcdfinfo.argtypes = [C.c_ushort, C.c_int32, C.POINTER(self.ODBFILESTATUS)]
        L.cnc_rdmcdfinfo.restype = C.c_short
        L.cnc_canmcdfinfo.argtypes = [C.c_ushort]
        L.cnc_canmcdfinfo.restype = C.c_short
        out = []
        L.cnc_canmcdfinfo(self.h)                # FANUC's tool also starts with this (answer 5 = nothing open)
        try:
            for i in range(500):
                st = self.ODBFILESTATUS()
                ret = L.cnc_rdmcdfinfo(self.h, 1 if i == 0 else -1, C.byref(st))
                if ret != 0:
                    if i == 0 and ret != 5:
                        raise self._err("cnc_rdmcdfinfo", ret)
                    break
                name = st.filename.split(b"\0")[0].decode(errors="replace").strip()
                if not name:
                    break
                mt = None
                if st.year > 1900:
                    try:
                        mt = time.mktime((st.year, st.month, st.day, st.hour, st.min, 0, 0, 0, -1))
                    except (OverflowError, ValueError):
                        pass
                out.append({"name": name, "dir": False, "size": int(st.size), "mtime": mt, "comment": ""})
        finally:
            L.cnc_canmcdfinfo(self.h)
        return out

    def _card_stream(self):
        from .fanuc_card import CardStream
        self.close()                             # one connection at a time for the card transfer
        return CardStream(self.cfg["host"], int(self.cfg.get("port", 8193)))

    def card_fetch(self, path, local, progress=None) -> int:
        from .fanuc_card import CardError
        name = self._card_name(path)
        try:
            with self._card_stream() as cs:
                data = cs.fetch(name, progress)
        except (CardError, OSError) as e:
            raise TransferError(f"memory card: {e}")
        with open(local, "wb") as f:
            f.write(data)
        return len(data)

    def card_send(self, local, name, progress=None, overwrite=False):
        from .fanuc_card import CardError
        self._card_name(self.CARD + name)
        if len(name) > 19:
            raise TransferError("memory card file names are at most 19 characters")
        with open(local, "rb") as f:
            data = f.read()
        try:
            with self._card_stream() as cs:
                cs.send(name, data, progress, overwrite)
        except (CardError, OSError) as e:
            raise TransferError(f"memory card: {e}")

    def _setup(self):
        C, L = self.C, self.L

        class IDBPDFADIR(C.Structure):
            _fields_ = [("path", C.c_char * 212), ("req_num", C.c_short), ("size_kind", C.c_short),
                        ("type", C.c_short), ("dummy", C.c_short)]

        class ODBPDFADIR(C.Structure):
            _fields_ = [("data_kind", C.c_short), ("year", C.c_short), ("mon", C.c_short), ("day", C.c_short),
                        ("hour", C.c_short), ("min", C.c_short), ("sec", C.c_short), ("dummy", C.c_short),
                        ("dummy2", C.c_int32), ("size", C.c_int32), ("attr", C.c_int32),
                        ("d_f", C.c_char * 36), ("comment", C.c_char * 52), ("o_time", C.c_char * 12)]

        class ODBPDFDRV(C.Structure):
            _fields_ = [("max_num", C.c_short), ("dummy", C.c_short), ("drive", (C.c_char * 12) * 16)]

        class ODBFILESTATUS(C.Structure):          # memory card file list (cnc_rdmcdfinfo)
            _fields_ = [("size", C.c_uint32), ("min", C.c_ubyte), ("hour", C.c_ubyte), ("day", C.c_ubyte),
                        ("month", C.c_ubyte), ("year", C.c_ushort), ("reserve", C.c_ubyte * 2),
                        ("filename", C.c_char * 20)]

        self.IDBPDFADIR, self.ODBPDFADIR, self.ODBPDFDRV = IDBPDFADIR, ODBPDFADIR, ODBPDFDRV
        self.ODBFILESTATUS = ODBFILESTATUS
        sigs = {
            "cnc_rdpdf_drive": [C.c_ushort, C.POINTER(ODBPDFDRV)],
            "cnc_rdpdf_alldir": [C.c_ushort, C.POINTER(C.c_short), C.POINTER(IDBPDFADIR), C.POINTER(ODBPDFADIR)],
            "cnc_upstart4": [C.c_ushort, C.c_short, C.c_char_p],
            "cnc_upload4": [C.c_ushort, C.POINTER(C.c_int32), C.c_char_p],
            "cnc_upend4": [C.c_ushort],
            "cnc_dwnstart4": [C.c_ushort, C.c_short, C.c_char_p],
            "cnc_download4": [C.c_ushort, C.POINTER(C.c_int32), C.c_char_p],
            "cnc_dwnend4": [C.c_ushort],
            "cnc_pdf_del": [C.c_ushort, C.c_char_p],
        }
        for fn, argt in sigs.items():
            if hasattr(L, fn):
                getattr(L, fn).argtypes = argt
                getattr(L, fn).restype = C.c_short

    def _need(self, fn):
        self._lib()
        if not hasattr(self.L, fn):
            raise TransferError(f"{fn} not in this FOCAS library - program folders not supported")
        return getattr(self.L, fn)

    def _err(self, fn, ret):
        msg = {1: "function not supported by this control", 3: "wrong path / name", 5: "data error",
               7: "write-protected (parameter / key)", 8: "memory full", 10: "busy, try again",
               12: "control is busy with another transfer or in the wrong mode", 13: "program in use / locked",
               -1: "control busy", -2: "control was reset during the transfer", -8: "handle lost",
               -16: "connection lost"}.get(ret, "")
        return TransferError(f"{fn} returned {ret}{' - ' + msg if msg else ''}")

    # Drive names differ per control/version: the Puma 2600LY reports CNC_MEM, CF_TEXT (memory card, used
    # for DNC operation from the card) and DATA. Every drive the control reports is offered.
    DRIVE_LABELS = {"CNC_MEM": ("CNC memory", ""), "MEMCARD": ("Memory card", "DNC operation from the card"),
                    "CF_TEXT": ("Memory card", "DNC operation from the card"), "CF": ("Memory card", ""),
                    "USB_MEM": ("USB stick", ""), "USB": ("USB stick", ""), "DATA_SV": ("Data server", ""),
                    "DATA": ("Data server", ""), "HDD": ("Hard disk", ""), "SSD": ("SSD", "")}

    def drives(self) -> list:
        """Drive names as the control reports them (cnc_rdpdf_drive), e.g. ['CNC_MEM', 'CF_TEXT', 'DATA']."""
        d = self.ODBPDFDRV()
        ret = self._need("cnc_rdpdf_drive")(self.h, self.C.byref(d))
        self.last_drive_raw = f"cnc_rdpdf_drive ret={ret} max_num={d.max_num} raw=" + ", ".join(
            repr(bytes(d.drive[i]).split(b"\0")[0]) for i in range(16) if bytes(d.drive[i]).strip(b"\0"))
        if ret != 0:
            raise self._err("cnc_rdpdf_drive", ret)
        out = []
        for i in range(max(0, min(16, d.max_num))):
            n = bytes(d.drive[i]).split(b"\0")[0].decode(errors="ignore").strip().strip("[]/")
            if n:
                out.append(n)
        return out

    def roots(self):
        cfg_roots = self.cfg.get("transfer_roots")
        if cfg_roots:
            return [{"path": r, "label": r, "note": ""} for r in cfg_roots
                    if not (self.has_card() and r.strip("/").split("/")[0].upper() in self.CARD_DRIVES)]
        try:
            drives = self.drives()
        except TransferError:
            drives = []
        if not drives:
            drives = ["CNC_MEM"]
        out = []
        for n in drives:
            label, note = self.DRIVE_LABELS.get(n.upper(), (n, ""))
            path = "//CNC_MEM/USER/PATH1/" if n.upper() == "CNC_MEM" else f"//{n}/"
            out.append({"path": path, "label": label, "note": note, "drive": n})
        if self.has_card():
            out = [r for r in out if r["drive"].upper() not in self.CARD_DRIVES]
            out.append({"path": self.CARD, "label": "Memory card", "note": "DNC (CF_TEXT)", "drive": "CARD"})
        order = ["CNC_MEM", "CARD", "CF_TEXT", "MEMCARD", "CF", "HDD", "SSD", "USB_MEM", "USB", "DATA_SV", "DATA"]
        out.sort(key=lambda r: order.index(r["drive"].upper()) if r["drive"].upper() in order else 99)
        return out

    def list(self, folder):
        if self.is_card(folder):
            return self.card_list()
        self._guard(folder)
        C = self.C
        fn = self._need("cnc_rdpdf_alldir")
        folder = folder if folder.endswith("/") else folder + "/"
        out, start = [], 0
        while start < 5000:
            want = 32
            req = self.IDBPDFADIR(path=folder.encode(), req_num=start, size_kind=1, type=1)
            buf = (self.ODBPDFADIR * want)()
            num = C.c_short(want)
            ret = fn(self.h, C.byref(num), C.byref(req), buf)
            if ret != 0:
                if out and ret in (-2, 3, 5):      # some versions answer an error past the last entry
                    break
                raise self._err("cnc_rdpdf_alldir", ret)
            for e in buf[:max(0, num.value)]:
                name = e.d_f.split(b"\0")[0].decode(errors="ignore").strip()
                if not name:
                    continue
                mt = None
                if e.year > 1900:
                    try:
                        mt = time.mktime((e.year, e.mon, e.day, e.hour, e.min, e.sec, 0, 0, -1))
                    except (OverflowError, ValueError):
                        pass
                out.append({"name": name, "dir": e.data_kind == 0, "size": None if e.data_kind == 0 else int(e.size),
                            "mtime": mt, "comment": e.comment.split(b"\0")[0].decode(errors="ignore").strip()})
            if num.value < want:
                break
            start += num.value
        return out

    def fetch(self, path, local, progress=None):
        if self.is_card(path):
            return self.card_fetch(path, local, progress)
        self._guard(path)
        C = self.C
        ret = self._need("cnc_upstart4")(self.h, 0, path.encode())
        if ret != 0:
            raise self._err("cnc_upstart4", ret)
        total, buf, tail, busy = 0, C.create_string_buffer(self.CHUNK + 1), b"", 0
        try:
            with open(local, "wb") as f:
                while True:
                    n = C.c_int32(self.CHUNK)
                    ret = self.L.cnc_upload4(self.h, C.byref(n), buf)
                    if ret == self.EW_BUFFER:
                        busy += 1
                        if busy > 3000:
                            raise TransferError("control stopped sending (timeout)")
                        time.sleep(0.01)
                        continue
                    if ret != 0:
                        raise self._err("cnc_upload4", ret)
                    busy = 0
                    chunk = buf.raw[:n.value]
                    f.write(chunk)
                    total += len(chunk)
                    if progress:
                        progress(total, None)
                    tail = (tail + chunk)[-4:]
                    if b"%" in tail and total > 1 and tail.rstrip(b"\r\n").endswith(b"%"):
                        break
                    if n.value == 0:
                        break
        finally:
            self.L.cnc_upend4(self.h)
        return total

    @staticmethod
    def prepare(data: bytes, name: str) -> bytes:
        """FOCAS download format: LF line ends, starts with LF, a program header line, ends with '%'.
        A file from CAM/another control usually has '%' + 'O1234 (...)' already - that is kept."""
        text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = text.split(b"\n")
        while lines and lines[0].strip() in (b"", b"%"):
            lines.pop(0)
        while lines and lines[-1].strip() in (b"", b"%"):
            lines.pop()
        stem = os.path.splitext(name)[0] if re.fullmatch(r"O\d{1,8}(\.\w+)?", name, re.I) else name
        first = lines[0].strip() if lines else b""
        if re.fullmatch(r"O\d{1,8}", stem, re.I):
            hdr = stem.upper().encode()
            if not re.match(rb"O\d", first, re.I):
                lines.insert(0, hdr)
            else:                                            # name in the file must match the chosen name
                lines[0] = re.sub(rb"^O\d+", hdr, lines[0].strip(), flags=re.I)
        else:
            hdr = b"<" + name.encode() + b">"
            if first.startswith(b"<"):
                lines[0] = re.sub(rb"^<[^>]*>", hdr, first)
            else:
                lines.insert(0, hdr)
        return b"\n" + b"\n".join(lines) + b"\n%"

    def send(self, local, folder, name, progress=None):
        if self.is_card(folder):
            return self.card_send(local, name, progress, getattr(self, "overwrite_next", False))
        self._guard(folder)
        C = self.C
        with open(local, "rb") as f:
            data = self.prepare(f.read(), name)
        ret = self._need("cnc_dwnstart4")(self.h, 0, folder.encode())
        if ret != 0:
            raise self._err("cnc_dwnstart4", ret)
        pos, busy, ok = 0, 0, False
        try:
            while pos < len(data):
                chunk = data[pos:pos + self.CHUNK]
                n = C.c_int32(len(chunk))
                ret = self.L.cnc_download4(self.h, C.byref(n), chunk)
                if ret == self.EW_BUFFER:
                    busy += 1
                    if busy > 3000:
                        raise TransferError("control stopped accepting data (timeout)")
                    time.sleep(0.01)
                    continue
                if ret != 0:
                    raise self._err("cnc_download4", ret)
                busy = 0
                pos += n.value
                if progress:
                    progress(pos, len(data))
            ok = True
        finally:
            ret = self.L.cnc_dwnend4(self.h)
            if ok and ret != 0:
                raise self._err("cnc_dwnend4", ret)

    def delete(self, path):
        if self.is_card(path):
            raise TransferError("deleting on the memory card is not possible from here - delete it at the control")
        self._guard(path)
        ret = self._need("cnc_pdf_del")(self.h, path.encode())
        if ret != 0:
            raise self._err("cnc_pdf_del", ret)


# --------------------------------------------------------------------------------------- HEIDENHAIN
class HeidenhainFiles(FileSession):
    def open(self):
        import pyLSV2
        self.con = pyLSV2.LSV2(self.cfg["host"], port=self.cfg.get("port", 19000),
                               timeout=self.cfg.get("timeout_s", 10), safe_mode=False)
        self.con.connect()

    def close(self):
        try:
            self.con.disconnect()
        except Exception:
            pass

    def roots(self):
        if self.cfg.get("transfer_roots"):
            return [{"path": r, "label": r, "note": ""} for r in self.cfg["transfer_roots"]]
        out = []
        try:
            if self.con.change_directory("TNC:\\nc_prog\\"):
                out.append({"path": "TNC:\\nc_prog\\", "label": "NC programs", "note": ""})
        except Exception:
            pass
        out.append({"path": "TNC:\\", "label": "TNC drive", "note": ""})
        return out

    def list(self, folder):
        from .plc_fetch import _entry
        folder = folder if folder.endswith("\\") else folder + "\\"
        if not self.con.change_directory(folder):
            raise TransferError(f"cannot open {folder}")
        lister = getattr(self.con, "directory_content", None) or getattr(self.con, "get_directory_content")
        out = []
        for e in lister() or []:
            name, is_dir, size, is_drive = _entry(e)
            name = name.rsplit("\\", 1)[-1]
            if not name or name in (".", "..") or is_drive or name.endswith(":"):
                continue
            # TNC 640 reports folders as plain files of 4096 bytes (same as on PLC:\) - a name without an
            # extension, or of exactly that size, that opens as a folder IS a folder
            if not is_dir and ("." not in name or size in (4096, 16384)):
                try:
                    is_dir = bool(self.con.change_directory(folder + name + "\\"))
                except Exception:
                    is_dir = False
            ts = getattr(e, "timestamp", None) if not isinstance(e, dict) else e.get("Date")
            mt = ts.timestamp() if hasattr(ts, "timestamp") else None
            out.append({"name": name, "dir": is_dir, "size": None if is_dir else size, "mtime": mt})
        return out

    @staticmethod
    def _binary(name):
        return os.path.splitext(name)[1].lower() not in NC_TEXT_EXT

    def fetch(self, path, local, progress=None):
        try:
            ok = self.con.recive_file(remote_path=path, local_path=local, override_file=True,
                                      binary_mode=self._binary(path))
        except TypeError:
            ok = self.con.recive_file(path, local, True, self._binary(path))
        if not ok or not os.path.exists(local):
            raise TransferError(f"control refused to send {path} ({_lsv2_err(self.con)})")
        return os.path.getsize(local)

    def send(self, local, folder, name, progress=None):
        remote = join(folder if folder.endswith("\\") else folder + "\\", name)
        try:
            ok = self.con.send_file(local_path=local, remote_path=remote, override_file=True,
                                    binary_mode=self._binary(name))
        except TypeError:
            ok = self.con.send_file(local, remote, True, self._binary(name))
        if not ok:
            raise TransferError(f"control refused {remote} ({_lsv2_err(self.con)})")

    def delete(self, path):
        fn = getattr(self.con, "delete_file", None)
        if fn and not fn(path):
            raise TransferError(f"cannot delete {path}")


def _lsv2_err(con):
    e = getattr(con, "last_error", None)
    return str(getattr(e, "e_code", e)) if e is not None else "no detail"


# ----------------------------------------------------------------------------------------- SIEMENS
class SiemensFiles(FileSession):
    """SINUMERIK OPC UA file system. Paths are given as the control expects them in the file methods:
    'Sinumerik/FileSystem/Part Program/HOUSING.MPF'."""
    FILE_TYPE, FOLDER_TYPE = 11575, 61

    def open(self):
        from asyncua.sync import Client
        from .drivers import siemens_opcua as S
        drv = S.SiemensOpcUaDriver(dict(self.cfg))
        self.client = drv._make_client(Client)
        self.client.connect()
        self._dispose = getattr(S, "_dispose", None)

    def close(self):
        try:
            self.client.disconnect()
        except Exception:
            pass
        if self._dispose:
            try:
                self._dispose(self.client)
            except Exception:
                pass

    def _node(self, path: str):
        """'Sinumerik/FileSystem/Part Program/' -> node, by browse name from Objects."""
        node = self.client.nodes.objects
        for part in [p for p in path.split("/") if p]:
            for ch in node.get_children():
                if ch.read_browse_name().Name == part:
                    node = ch
                    break
            else:
                raise TransferError(f"not found on the control: {path}")
        return node

    def roots(self):
        if self.cfg.get("transfer_roots"):
            return [{"path": r, "label": r.rstrip("/").rsplit("/", 1)[-1], "note": ""}
                    for r in self.cfg["transfer_roots"]]
        fs = self._node("Sinumerik/FileSystem/")
        out = []
        label = {"Part Program": "Part programs", "Sub Program": "Subprograms", "Work Pieces": "Workpieces"}
        for ch in fs.get_children():
            n = ch.read_browse_name().Name
            if self._kind(ch, n) == "dir":
                out.append({"path": f"Sinumerik/FileSystem/{n}/", "label": label.get(n, n), "note": ""})
        order = ["Part Program", "Sub Program", "Work Pieces"]
        out.sort(key=lambda r: (order.index(r["path"].split("/")[2]) if r["path"].split("/")[2] in order else 9,
                                r["label"]))
        return out

    FILE_DIRECTORY_TYPE = 13353
    DIR_METHODS = {"CreateDirectory", "CreateFile", "Delete", "MoveOrCopy"}       # methods of a folder
    FILE_MEMBERS = {"Open", "Close", "Read", "Write", "GetPosition", "SetPosition", "Size", "OpenCount"}
    FILE_EXT = re.compile(r"\.(MPF|SPF|INI|TXT|CSV|LOG|DAT|ARC|TEA|COM|DEF|XML|NC|H)$", re.I)
    DIR_EXT = re.compile(r"\.(WPD|DIR)$", re.I)

    def _kind(self, node, name=""):
        """'file', 'dir' or None (not a file system entry, e.g. the folder's CreateFile method).
        SINUMERIK uses its own subtypes of FileType / FileDirectoryType (DNM6700), so the standard type ids
        are only a first guess; the members of the node decide, the name's extension breaks a tie."""
        try:
            from asyncua import ua
            if node.read_node_class() != ua.NodeClass.Object:
                return None                              # methods (CreateFile, Delete ...) and variables
        except Exception:
            pass
        try:
            ident = getattr(node.read_type_definition(), "Identifier", None)
            if ident == self.FILE_TYPE:
                return "file"
            if ident in (self.FOLDER_TYPE, self.FILE_DIRECTORY_TYPE):
                return "dir"
        except Exception:
            pass
        try:
            members = {c.read_browse_name().Name for c in node.get_children()}
        except Exception:
            members = set()
        if members & self.DIR_METHODS:
            return "dir"
        if members & self.FILE_MEMBERS:
            return "file"
        if self.DIR_EXT.search(name):
            return "dir"
        if self.FILE_EXT.search(name):
            return "file"
        return "dir" if not members else "file"

    def list(self, folder):
        node = self._node(folder)
        out = []
        for ch in node.get_children():
            n = ch.read_browse_name().Name
            kind = self._kind(ch, n)
            if kind not in ("file", "dir"):
                continue
            size = None
            if kind == "file":
                try:
                    sz = self._member(ch, "Size")
                    size = int(sz.read_value()) if sz is not None else None
                except Exception:
                    pass
            out.append({"name": n, "dir": kind == "dir", "size": size, "mtime": None})
        return out

    # Standard OPC UA file access (Part 20, FileType / FileDirectoryType): a file node has Open/Read/Write/Close,
    # a folder node has CreateFile/Delete. The DNM6700 offers these; it has no Sinumerik/Methods/ node, so the
    # Siemens-specific CopyFileFromServer/CopyFileToServer are only a fallback.
    CHUNK = 16384
    MODE_READ, MODE_WRITE, MODE_ERASE = 1, 2, 4

    @staticmethod
    def _member(node, name):
        for ch in node.get_children():
            if ch.read_browse_name().Name == name:
                return ch
        return None

    # Standard FileType methods (OPC UA Part 5). A server may accept these on a file object even when the object
    # doesn't list its own copy (the DNM6700's new file showed no Write member).
    FILETYPE_METHODS = {"Open": 11580, "Close": 11583, "Read": 11585, "Write": 11588}

    def _fmethod(self, node, name):
        m = self._member(node, name)
        if m is not None:
            return m
        try:
            from asyncua import ua
            return self.client.get_node(ua.NodeId(self.FILETYPE_METHODS[name], 0))
        except Exception:
            names = sorted(ch.read_browse_name().Name for ch in node.get_children())
            raise TransferError(f"the file on the control offers no {name} command (it offers: {', '.join(names) or 'nothing'})")

    def _method(self, name):
        """Find a Siemens method by name: Sinumerik/Methods, Methods, or 3 levels deep under Objects."""
        for base in ("Sinumerik/Methods/", "Methods/"):
            try:
                node = self._node(base)
            except TransferError:
                continue
            m = self._member(node, name)
            if m is not None:
                return node, m
        level = [self.client.nodes.objects]
        for _ in range(3):
            nxt = []
            for parent in level:
                try:
                    kids = parent.get_children()
                except Exception:
                    continue
                for ch in kids:
                    try:
                        bn = ch.read_browse_name().Name
                    except Exception:
                        continue
                    if bn == name:
                        return parent, ch
                    if bn not in ("FileSystem", "Server", "Types", "Views"):
                        nxt.append(ch)
            level = nxt[:400]
        raise TransferError(f"the control offers neither standard file access nor the Siemens method {name}")

    # ---- OPC UA user rights (SINUMERIK user administration). Only used from the command line, on request.
    def _method_any(self, name):
        """Like _method, but also tries the documented node ids ns=<i>;s=/Methods/<name> in every namespace
        (the method node may exist without being linked into the browse tree)."""
        try:
            return self._method(name)
        except TransferError:
            pass
        from asyncua import ua
        try:
            uris = self.client.get_namespace_array()
        except Exception:
            uris = []
        for i in range(1, max(len(uris), 4)):
            for ident in (f"/Methods/{name}", f"/Sinumerik/Methods/{name}", name):
                try:
                    node = self.client.get_node(ua.NodeId(ident, i))
                    node.read_browse_name()
                except Exception:
                    continue
                try:
                    parent = node.get_parent() or self.client.get_node(ua.NodeId("/Methods", i))
                except Exception:
                    parent = self.client.get_node(ua.NodeId("/Methods", i))
                return parent, node
        raise TransferError(f"method {name} not found on this control")

    def tree(self, depth=2):
        """Names under Objects (2 levels) + namespaces - to see where a control keeps its methods."""
        out = []
        try:
            out.append("namespaces: " + ", ".join(f"{i}={u}" for i, u in enumerate(self.client.get_namespace_array())))
        except Exception as e:
            out.append(f"namespaces: cannot read ({e})")

        def walk(node, d, indent):
            if d > depth:
                return
            try:
                kids = node.get_children()
            except Exception:
                return
            for ch in kids[:60]:
                try:
                    bn, nid = ch.read_browse_name(), ch.nodeid.to_string()
                except Exception:
                    continue
                out.append(f"{indent}{bn.NamespaceIndex}:{bn.Name}   [{nid}]")
                if bn.Name not in ("Server", "FileSystem"):
                    walk(ch, d + 1, indent + "    ")
        walk(self.client.nodes.objects, 1, "  ")
        return out

    def my_rights(self):
        obj, m = self._method_any("GetMyAccessRights")
        return obj.call_method(m)

    def grant(self, user, right):
        from asyncua import ua
        obj, m = self._method_any("GiveUserAccess")
        return obj.call_method(m, ua.Variant(user, ua.VariantType.String), ua.Variant(right, ua.VariantType.String))

    def fetch(self, path, local, progress=None):
        from asyncua import ua
        node = self._node(path)
        if self._member(node, "Open") is None and self._member(node, "Read") is None:
            try:
                return self._fetch_siemens(path, local)
            except TransferError:
                pass
        op, rd, cl = (self._fmethod(node, n) for n in ("Open", "Read", "Close"))
        size = None
        try:
            sz = self._member(node, "Size")
            size = int(sz.read_value()) if sz is not None else None
        except Exception:
            pass
        handle = node.call_method(op, ua.Variant(self.MODE_READ, ua.VariantType.Byte))
        total = 0
        try:
            with open(local, "wb") as f:
                while True:
                    chunk = node.call_method(rd, ua.Variant(handle, ua.VariantType.UInt32),
                                             ua.Variant(self.CHUNK, ua.VariantType.Int32))
                    chunk = _pick_bytes(chunk) or b""
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if progress:
                        progress(total, size)
                    if size is not None and total >= size:
                        break
        finally:
            try:
                node.call_method(cl, ua.Variant(handle, ua.VariantType.UInt32))
            except Exception:
                pass
        return total

    def _fetch_siemens(self, path, local):
        obj, m = self._method("CopyFileFromServer")
        res = obj.call_method(m, path)
        data = _pick_bytes(res)
        if data is None:
            raise TransferError(f"CopyFileFromServer returned no data ({res!r:.80})")
        with open(local, "wb") as f:
            f.write(data)
        return len(data)

    def send(self, local, folder, name, progress=None):
        from asyncua import ua
        name = name.upper()              # SINUMERIK stores names in upper case (TEST.mpf becomes TEST.MPF)
        dnode = self._node(folder)
        create = self._member(dnode, "CreateFile")
        if create is None:
            return self._send_siemens(local, folder, name)
        self.step = "create file"
        res = dnode.call_method(create, ua.Variant(name, ua.VariantType.String),
                                ua.Variant(True, ua.VariantType.Boolean))        # requestFileOpen -> open for write
        log.info("SINUMERIK CreateFile(%s) answered %r", name, res)
        # The answer should be [fileNodeId, fileHandle]; the DNM6700 (828D) does not give a usable node id, so the
        # new file is looked up by name in the folder, and opened for writing if no handle came back.
        vals = list(res) if isinstance(res, (list, tuple)) else [res]
        handle = next((v for v in vals if isinstance(v, int) and not isinstance(v, bool)), None)
        # The node id in the answer can't be trusted (DNM6700: '.../TEST.mpf' while the file is TEST.MPF - that
        # node has no members). Use the file as the folder lists it.
        fnode = next((ch for ch in dnode.get_children()
                      if ch.read_browse_name().Name.upper() == name.upper()), None)
        if fnode is None:
            raise TransferError(f"the control created {name} but it can't be found in the folder")
        size, done, ok = os.path.getsize(local), 0, False
        try:
            self.step = "open the new file"
            wr, cl = self._fmethod(fnode, "Write"), self._fmethod(fnode, "Close")
            if handle is None:
                op = self._fmethod(fnode, "Open")
                handle = fnode.call_method(op, ua.Variant(self.MODE_WRITE | self.MODE_ERASE, ua.VariantType.Byte))
            self.step = "write"
            try:
                with open(local, "rb") as f:
                    first = True
                    while True:
                        chunk = f.read(self.CHUNK)
                        if not chunk:
                            break
                        try:
                            fnode.call_method(wr, ua.Variant(handle, ua.VariantType.UInt32),
                                              ua.Variant(chunk, ua.VariantType.ByteString))
                        except Exception as e:
                            if not first:
                                raise
                            # the handle from CreateFile isn't valid on this node: open the file ourselves
                            log.info("SINUMERIK write with CreateFile handle failed (%s) - opening the file", e)
                            self.step = "open the new file"
                            op = self._fmethod(fnode, "Open")
                            handle = fnode.call_method(op, ua.Variant(self.MODE_WRITE | self.MODE_ERASE,
                                                                      ua.VariantType.Byte))
                            self.step = "write"
                            fnode.call_method(wr, ua.Variant(handle, ua.VariantType.UInt32),
                                              ua.Variant(chunk, ua.VariantType.ByteString))
                        first = False
                        done += len(chunk)
                        if progress:
                            progress(done, size)
            except Exception:
                try:                            # close quietly - the write error is the one to report
                    fnode.call_method(cl, ua.Variant(handle, ua.VariantType.UInt32))
                except Exception:
                    pass
                raise
            self.step = "close"
            fnode.call_method(cl, ua.Variant(handle, ua.VariantType.UInt32))
            ok = True
        finally:
            if not ok:                          # don't leave an empty / half file behind on the control
                try:
                    log.warning("SINUMERIK new file %s offers: %s", name,
                                sorted(ch.read_browse_name().Name for ch in fnode.get_children()))
                except Exception:
                    pass
                try:
                    dl = self._member(dnode, "Delete")
                    if dl is not None:
                        dnode.call_method(dl, fnode.nodeid)
                except Exception:
                    pass

    def _send_siemens(self, local, folder, name):
        from asyncua import ua
        with open(local, "rb") as f:
            data = f.read()
        obj, m = self._method("CopyFileToServer")
        res = obj.call_method(m, join(folder, name), ua.Variant(data, ua.VariantType.ByteString),
                              ua.Variant(True, ua.VariantType.Boolean))
        if isinstance(res, (list, tuple)) and res and isinstance(res[0], str) and res[0] and "good" not in res[0].lower():
            raise TransferError(f"control answered: {res[0]}")

    def delete(self, path):
        self.step = "delete the old version"
        parent, _, name = path.rstrip("/").rpartition("/")
        dnode = self._node(parent + "/")
        dl = self._member(dnode, "Delete")
        if dl is None:
            obj, m = self._method("DeleteFile")
            obj.call_method(m, path)
            return
        dnode.call_method(dl, self._node(path).nodeid)


def _pick_bytes(res):
    if isinstance(res, (bytes, bytearray)):
        return bytes(res)
    if isinstance(res, (list, tuple)):
        for r in res:
            if isinstance(r, (bytes, bytearray)):
                return bytes(r)
    return None


# =====================================================================================================
# Service used by the web server
# =====================================================================================================
class TransferService:
    def __init__(self, machines: list, store, data_dir: str, settings: dict | None = None):
        self.machines = {m["id"]: m for m in machines}
        self.store = store
        self.data_dir = data_dir
        self.settings = settings or {}
        self.pin = str(self.settings.get("pin", "") or "")
        self.max_bytes = int(float(self.settings.get("max_mb", 200)) * 1024 * 1024)
        self.tmp = os.path.join(data_dir, "transfer_tmp")
        os.makedirs(self.tmp, exist_ok=True)
        for f in os.listdir(self.tmp):             # leftovers from before a restart
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        # backups of replaced programs are limited, so they can never fill the Pi's disk
        self.backup_on = self.settings.get("backup", True) not in (False, "off", "no")
        self.backup_keep = int(self.settings.get("backup_keep", 3))           # versions per program
        self.backup_days = float(self.settings.get("backup_days", 30))        # older ones are removed
        self.backup_max = int(float(self.settings.get("backup_max_mb", 200)) * 1048576)   # all machines
        self.bdir = os.path.join(data_dir, "transfer_backup")
        try:
            self.prune_backups()
        except Exception as e:
            log.warning("backup clean-up failed: %s", e)
        self.locks = {mid: threading.Lock() for mid in self.machines}
        self.jobs: dict = {}
        self.jlock = threading.Lock()

    # ---------------------------------------------------------------- helpers
    def _m(self, mid):
        m = self.machines.get(mid)
        if not m:
            raise ValueError("unknown machine")
        if not self.supported(m):
            raise ValueError("file transfer is not available for this machine")
        return m

    @staticmethod
    def supported(m) -> bool:
        if m.get("transfer") in (False, "off", "no"):
            return False
        # FANUC: OFF unless the machine has transfer = "read" or "send". On 2026-11-11 a probe that opened drive
        # names the control did not report (cnc_rdpdf_alldir on //CF_TEXT/, //HDD/ ...) crashed a Series 0i-D
        # (Puma 2600LY) with SYS_ALM198 - a power cycle was needed. Never call file functions on a FANUC
        # control that hasn't been switched on deliberately, and only on drives the control itself reports.
        if m.get("control") == "fanuc" and m.get("transfer") not in ("read", "send"):
            return False
        if m.get("control") == "simulator":
            return m.get("flavor") in DemoFiles.ROOTS
        return m.get("control") in ("fanuc", "heidenhain", "siemens")

    @staticmethod
    def can_send(m) -> bool:
        return m.get("transfer") in ("send", "both", True, "on") or (
            m.get("control") == "simulator" and m.get("transfer") is None)

    def session(self, m) -> FileSession:
        c = m.get("control")
        if c == "simulator":
            return DemoFiles(m, self.data_dir)
        if c == "fanuc":
            return FanucFiles(m)
        if c == "heidenhain":
            return HeidenhainFiles(m)
        if c == "siemens":
            return SiemensFiles(m)
        raise ValueError("no file access for this control")

    def _lock(self, mid, wait=20):
        lk = self.locks[mid]
        if not lk.acquire(timeout=wait):
            raise TransferError("another transfer to this machine is still busy - try again in a moment")
        return lk

    def _protected(self, m, name) -> bool:
        pats = m.get("transfer_protect")
        if pats is None:
            pats = DEFAULT_PROTECT.get(m.get("control"), [])
        n = base_name(name)
        return any(re.search(p, n, re.I) for p in pats)

    def _in_use(self, mid, name) -> str:
        """'selected' / 'running' when this name is the control's main or active program right now."""
        live = {}
        try:
            for x in self.store.machines():
                if x["id"] == mid:
                    live = x.get("live") or {}
        except Exception:
            pass
        n = base_name(name)
        if not n:
            return ""
        if n == base_name(live.get("current_program")) and live.get("state") not in (None, "IDLE", "OFFLINE"):
            return "running"
        if n in (base_name(live.get("program")), base_name(live.get("current_program"))):
            return "selected"
        return ""

    def prune_backups(self) -> int:
        """Keep at most backup_keep versions per program per machine, nothing older than backup_days, and
        at most backup_max bytes in total (oldest go first). Returns the number of files removed."""
        if not os.path.isdir(self.bdir):
            return 0
        files = []
        for mid in os.listdir(self.bdir):
            d = os.path.join(self.bdir, mid)
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    st = os.stat(p)
                    prog = n.split("_", 1)[1] if re.match(r"^\d{8}-\d{6}_", n) else n
                    files.append({"p": p, "key": (mid, prog.upper()), "t": st.st_mtime, "size": st.st_size})
        files.sort(key=lambda f: f["t"], reverse=True)              # newest first
        keep, seen, total, gone = [], {}, 0, 0
        cutoff = time.time() - self.backup_days * 86400
        for f in files:
            seen[f["key"]] = seen.get(f["key"], 0) + 1
            if seen[f["key"]] > self.backup_keep or f["t"] < cutoff or total + f["size"] > self.backup_max:
                try:
                    os.remove(f["p"])
                    gone += 1
                except OSError:
                    pass
                continue
            total += f["size"]
            keep.append(f)
        if gone:
            log.info("transfer backups: removed %d old file(s), %d kept (%.1f MB)", gone, len(keep), total / 1e6)
        return gone

    # ---------------------------------------------------------------- API
    def info(self, mid) -> dict:
        m = self.machines.get(mid)
        if not m or not self.supported(m):
            return {"supported": False}
        return {"supported": True, "send": self.can_send(m), "pin": bool(self.pin),
                "max_mb": round(self.max_bytes / 1048576), "control": m.get("control")}

    def _live(self, mid) -> dict:
        try:
            for x in self.store.machines():
                if x["id"] == mid:
                    return x.get("live") or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def program_folder(control, path):
        """Folder of the selected main program, in the form the file session uses - or None."""
        if not path:
            return None
        p = str(path).strip().rstrip("\x00").strip()
        if control == "siemens" or p.startswith("/_N_"):
            # /_N_MPF_DIR/_N_PART_MPF  /_N_SPF_DIR/...  /_N_WKS_DIR/_N_X_WPD/_N_PART_MPF
            parts = [x for x in p.split("/") if x]
            names = {"_N_MPF_DIR": "Part Program", "_N_SPF_DIR": "Sub Program", "_N_WKS_DIR": "Work Pieces"}
            if len(parts) < 2 or parts[0] not in names:
                return None
            out = ["Sinumerik", "FileSystem", names[parts[0]]]
            for d in parts[1:-1]:
                out.append(re.sub(r"^_N_|_WPD$", "", d) + (".WPD" if d.endswith("_WPD") else ""))
            return "/".join(out) + "/"
        sep = "\\" if "\\" in p else "/"
        if sep not in p:
            return None
        return p[: p.rfind(sep) + 1]

    def roots(self, mid):
        m = self._m(mid)
        lk = self._lock(mid)
        try:
            with self.session(m) as s:
                roots = s.roots()
        finally:
            lk.release()
        roots = self._remember_roots(mid, roots)
        # open where the selected program lives: an existing location gets the default mark, a deeper folder
        # becomes its own "Program folder" button (the TNC's own nc_prog shortcut is then left out)
        folder = self.program_folder(m.get("control") if m.get("control") != "simulator" else m.get("flavor"),
                                     self._live(mid).get("program_path"))
        if folder:
            same = [r for r in roots if r["path"].lower() == folder.lower()]
            if same:
                same[0]["default"] = True
            else:
                roots = [r for r in roots if not (m.get("control") == "heidenhain" and r["path"].lower() == "tnc:\\nc_prog\\")]
                roots.insert(0, {"path": folder, "label": "Program folder", "note": "", "default": True})
        return roots

    def _remember_roots(self, mid, roots):
        """Drives can come and go (a memory card that is inserted later, a drive that is busy while the machine
        runs). Locations seen before stay listed, marked unavailable, instead of silently vanishing.
        Remembered in data/transfer_drives.json so a restart of the monitor doesn't forget them."""
        fp = os.path.join(self.data_dir, "transfer_drives.json")
        with self.jlock:
            try:
                with open(fp, encoding="utf-8") as f:
                    seen = json.load(f)
            except (OSError, ValueError):
                seen = {}
            known = {r["path"].lower(): r for r in seen.get(mid, [])}
            m = self.machines.get(mid) or {}
            if m.get("control") == "fanuc" and str(m.get("memory_card", "")).lower() == "dnc":
                known = {k: r for k, r in known.items()
                         if k.strip("/").split("/")[0].upper() not in FanucFiles.CARD_DRIVES}
            now = {r["path"].lower() for r in roots}
            for r in roots:
                r["available"] = True
                known[r["path"].lower()] = {k: v for k, v in r.items() if k in ("path", "label", "note", "drive")}
            gone = [{**r, "available": False} for k, r in known.items() if k not in now]
            seen[mid] = list(known.values())
            try:
                with open(fp + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(seen, f, indent=1)
                os.replace(fp + ".tmp", fp)
            except OSError:
                pass
        return roots + gone

    def list(self, mid, folder):
        m = self._m(mid)
        lk = self._lock(mid)
        try:
            with self.session(m) as s:
                items = s.list(folder)
        finally:
            lk.release()
        for it in items:
            if not it["dir"]:
                it["protected"] = self._protected(m, it["name"])
                it["in_use"] = self._in_use(mid, it["name"])
        items.sort(key=lambda e: (not e["dir"], e["name"].upper()))
        return items

    def history(self, mid, limit=30):
        return self.store.transfers(mid, limit)

    def job(self, jid) -> dict:
        with self.jlock:
            j = self.jobs.get(jid)
            return {k: v for k, v in j.items() if not k.startswith("_")} if j else {"state": "unknown"}

    def job_file(self, jid):
        with self.jlock:
            j = self.jobs.get(jid)
            if not j or j.get("state") != "done" or j.get("kind") != "fetch":
                return None
            return j["_file"], j["name"]

    def _new_job(self, **kw) -> dict:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "state": "running", "msg": "connecting ...", "done": 0, "total": None,
               "started": time.time(), **kw}
        with self.jlock:
            for k in [k for k, j in self.jobs.items() if time.time() - j.get("started", 0) > 3600]:
                f = self.jobs[k].get("_file")
                if f and os.path.exists(f):
                    os.remove(f)
                del self.jobs[k]
            self.jobs[jid] = job
        return job

    def _set(self, job, **kw):
        with self.jlock:
            job.update(kw)

    def start_fetch(self, mid, path, client=""):
        m = self._m(mid)
        name = re.split(r"[\\/]", path.rstrip("/\\"))[-1]
        job = self._new_job(kind="fetch", machine=mid, path=path, name=name)
        local = os.path.join(self.tmp, job["id"])
        job["_file"] = local

        def run():
            lk = None
            try:
                lk = self._lock(mid, 120)
                with self.session(m) as s:
                    self._set(job, msg="copying from the control ...")
                    n = s.fetch(path, local, lambda d, t: self._set(job, done=d, total=t))
                self._set(job, state="done", done=n, total=n, msg="ready", ended=time.time())
                self.store.add_transfer(mid, "fetch", path, n, "ok", client=client)
            except Exception as e:
                log.warning("[%s] fetch %s failed: %s", mid, path, e)
                self._set(job, state="error", msg=str(e), ended=time.time())
                self.store.add_transfer(mid, "fetch", path, None, "error", str(e), client=client)
            finally:
                if lk:
                    lk.release()
        threading.Thread(target=run, daemon=True, name=f"fetch-{mid}").start()
        return self.job(job["id"])

    def check_send(self, mid, folder, name, overwrite, pin):
        """Raises ValueError for anything the request may never do. Returns the machine config."""
        m = self._m(mid)
        if not self.can_send(m):
            raise ValueError('sending is switched off for this machine (config.toml: transfer = "send")')
        if self.pin and str(pin or "") != self.pin:
            raise PermissionError("wrong PIN")
        if not NAME_RE.match(name or "") or name.strip(" .") != name:
            raise ValueError("file name may only use letters, digits, - _ . and spaces")
        if m.get("control") == "fanuc" and len(name) > 32:
            raise ValueError("FANUC program names are at most 32 characters")
        if self._protected(m, name):
            raise ValueError(f"{name} is a protected program on this machine - it is never replaced from here")
        use = self._in_use(mid, name)
        if use:
            raise ValueError(f"{name} is {use} on the control right now - it is not replaced while in use")
        return m

    def start_send(self, mid, folder, name, upload_file, overwrite=False, pin="", client=""):
        if (self.machines.get(mid) or {}).get("control") == "siemens":
            name = (name or "").upper()          # SINUMERIK file names are upper case
        try:
            m = self.check_send(mid, folder, name, overwrite, pin)
        except (ValueError, PermissionError) as e:
            self.store.add_transfer(mid, "send", join(folder, name or "?"), None, "refused", str(e), client=client)
            os.remove(upload_file)
            raise
        size = os.path.getsize(upload_file)
        job = self._new_job(kind="send", machine=mid, path=join(folder, name), name=name, total=size)

        def run():
            lk, backup, sess = None, "", None
            target = join(folder, name)
            try:
                lk = self._lock(mid, 120)
                with self.session(m) as s:
                    sess = s
                    self._set(job, msg="checking the folder on the control ...")
                    exists = [e for e in s.list(folder) if not e["dir"] and base_name(e["name"]) == base_name(name)]
                    if exists and not overwrite:
                        self._set(job, state="exists", msg=f"{name} already exists on the control",
                                  existing=exists[0], ended=time.time())
                        return
                    use = self._in_use(mid, name)                # check again right before writing
                    if use:
                        raise TransferError(f"{name} became {use} on the control - not replaced")
                    if exists:
                        old = join(folder, exists[0]["name"])
                        osize = exists[0].get("size") or 0
                        if self.backup_on and osize <= self.backup_max:
                            self._set(job, msg="keeping a copy of the current version ...")
                            bdir = os.path.join(self.bdir, mid)
                            os.makedirs(bdir, exist_ok=True)
                            backup = os.path.join(bdir, time.strftime("%Y%m%d-%H%M%S_") + exists[0]["name"])
                            s.fetch(old, backup)
                        if getattr(s, "overwrites", lambda f: False)(folder):
                            s.overwrite_next = True          # memory card: replaced in one step, no delete
                        else:
                            self._set(job, msg="removing the old version ...")
                            s.delete(old)
                    self._set(job, msg="sending to the control ...")
                    s.send(upload_file, folder, name, lambda d, t: self._set(job, done=d, total=t or size))
                if backup:
                    self.prune_backups()
                self._set(job, state="done", done=size, msg="sent" + (" - old version kept on the monitor" if backup else ""),
                          backup=os.path.basename(backup), ended=time.time())
                self.store.add_transfer(mid, "send", target, size, "ok", "replaced" if backup else "",
                                        os.path.relpath(backup, self.data_dir) if backup else "", client)
            except Exception as e:
                log.exception("[%s] send %s failed", mid, target)
                step = getattr(sess, "step", None) if sess is not None else None
                detail = (f"{step}: " if step else "") + str(e) + (
                    f" (old version is kept in {os.path.basename(backup)})" if backup else "")
                self._set(job, state="error", msg=detail, ended=time.time())
                self.store.add_transfer(mid, "send", target, size, "error", detail,
                                        os.path.relpath(backup, self.data_dir) if backup else "", client)
            finally:
                if lk:
                    lk.release()
                if job.get("state") != "exists":
                    try:
                        os.remove(upload_file)
                    except OSError:
                        pass
                else:
                    job["_upload"] = upload_file      # kept so "Replace" doesn't need a new upload
        threading.Thread(target=run, daemon=True, name=f"send-{mid}").start()
        return self.job(job["id"])

    def replace(self, jid, pin="", client=""):
        """User confirmed 'replace' after a send ended in state 'exists'."""
        with self.jlock:
            j = self.jobs.get(jid)
            if not j or j.get("state") != "exists" or not j.get("_upload"):
                raise ValueError("nothing to replace - send the file again")
            upload, j["_upload"] = j["_upload"], None
            j["state"] = "replaced-by-new-job"
        folder = j["path"][: len(j["path"]) - len(j["name"])]
        return self.start_send(j["machine"], folder, j["name"], upload, True, pin, client)

    def new_upload_path(self) -> str:
        return os.path.join(self.tmp, "up_" + uuid.uuid4().hex[:12])

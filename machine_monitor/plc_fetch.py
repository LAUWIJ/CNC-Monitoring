"""One-time, READ-ONLY copy of the PLC text files from a HEIDENHAIN control (python run.py --fetch-plc <id>).

Why: the machine builder's PLC messages ("door open", "lube low", ...) are not readable over LSV2 on a
TNC 640/620/TNC7. They are defined in files on the control's PLC: drive (error/message table .PET, the
message texts, symbol/comment files). With those files we can find which PLC marker belongs to which
message, and the monitor can then read just those markers.

What it does: logs in for PLC file access (needs the PLC code number of the control), lists PLC:\\ and
downloads the text-like files into data/plc_<id>/. It never writes, deletes or changes anything on the
control. Run it when the machine is idle - it only takes a moment.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("plc_fetch")

# never download these (binaries, images, archives) - names are still listed in filelist.txt
SKIP_EXT = {".zip", ".exe", ".dll", ".so", ".bin", ".bmp", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".wav",
            ".mp3", ".pdf", ".chm", ".7z", ".gz", ".tar", ".img", ".iso", ".hex", ".pck", ".tgz", ".bz2"}
MAX_FILE = 3 * 1024 * 1024        # bytes per file
MAX_MAP = 40 * 1024 * 1024        # the PLC compiler's map file (MainPgm.map, ~3 MB): holds the addresses of
                                  # symbols the compiler placed itself, e.g. DG_TOOL_IN_SPINDLE -> D17032


def _too_big(path: str, size) -> bool:
    # .map (binary, TNC 640 BMO) / .lst (compiler listing, ~7 MB - the only compiler output on the DVF Erowa):
    # the files that hold the addresses the compiler gave to symbols like DG_TOOL_IN_SPINDLE
    limit = MAX_MAP if path.lower().endswith((".map", ".lst")) else MAX_FILE
    return size is not None and int(size) > limit
MAX_TOTAL = 150 * 1024 * 1024     # bytes in total
MAX_FILES = 3000


def _plc_login(con, password: str) -> bool:
    import pyLSV2
    login = None
    enum = getattr(pyLSV2, "Login", None)
    if enum is None:
        try:
            from pyLSV2.const import Login as enum      # noqa: N813
        except Exception:
            enum = None
    if enum is not None:
        login = getattr(enum, "FILEPLC", None)
    if login is None:
        login = getattr(pyLSV2, "LOGIN_FILEPLC", "FILEPLC")
    try:
        return bool(con.login(login, password))
    except TypeError:
        return bool(con.login(login=login, password=password))


# folders that never hold PLC messages - listed, but not walked into (they can hold thousands of files)
SKIP_DIRS = {"lost+found", "tncguide", "python", "renishaw", "blum", "oemcyc", "home", "proto", "ccfiles",
             "config.bak", "jh"}


def _entry(e):
    """(name, is_dir, size, is_drive) from a pyLSV2 1.x FileEntry or a 0.x dict."""
    if isinstance(e, dict):
        name = e.get("Name") or e.get("name") or ""
        attrs = str(e.get("Attributs", e.get("attributes", "")))
        return (name, ("DIR" in attrs.upper() or bool(e.get("is_directory"))), e.get("Size", e.get("size")),
                bool(e.get("is_drive")))
    return (getattr(e, "name", "") or "", bool(getattr(e, "is_directory", False)), getattr(e, "size", None),
            bool(getattr(e, "is_drive", False)))


def _walk(con, root: str, out: list, depth: int = 0):
    if depth > 8 or len(out) > 20000:
        return
    if not con.change_directory(root):
        out.append((root, None, "cannot open"))
        return
    lister = getattr(con, "directory_content", None) or getattr(con, "get_directory_content")
    items = lister() or []
    for e in items:
        name, is_dir, size, is_drive = _entry(e)
        name = name.rsplit("\\", 1)[-1]
        if not name or name in (".", "..") or is_drive or name.endswith(":"):
            continue
        path = root + name
        # some TNC 640 versions report folders on PLC:\ as plain files (size 4096, no flag):
        # a name without extension that can be opened as a folder IS a folder
        if not is_dir and ("." not in name or size in (4096, 16384)):
            try:
                is_dir = bool(con.change_directory(path + "\\"))
            except Exception:
                is_dir = False
        if is_dir:
            if name.lower() in SKIP_DIRS:
                out.append((path + "\\", None, "dir (skipped)"))
                continue
            out.append((path + "\\", None, "dir"))
            _walk(con, path + "\\", out, depth + 1)
        else:
            out.append((path, size, "file"))


def _cfg_value(text: str, key: str):
    import re
    m = re.search(rf'\b{key}\s*:=\s*"?([^",\n)]+)"?', text)
    return m.group(1).strip() if m else None


def _known_message_files(dest: str) -> list:
    """Message table + texts named in the PLC configuration (often in folders the listing hides).
    plc.cfg CfgPlcPath: errorTable:="%OEM%/plc/language/PlcError.pet", errorText:="ErrorText.csv";
    oem.cfg CfgDisplayLanguage: plcErrorLanguage:=ENGLISH."""
    def read(rel):
        try:
            with open(os.path.join(dest, rel), encoding="latin-1") as f:
                return f.read()
        except OSError:
            return ""
    plc, oem = read(os.path.join("config", "plc.cfg")), read(os.path.join("config", "oem.cfg"))
    table = _cfg_value(plc, "errorTable") or "%OEM%/plc/language/PlcError.pet"
    lang = (_cfg_value(oem, "plcErrorLanguage") or "ENGLISH").upper()
    table = table.replace("%OEM%", "PLC:").replace("/", "\\").replace("PLC:\\\\", "PLC:\\")
    if not table.upper().startswith("PLC:"):
        table = "PLC:\\" + table.lstrip("\\")
    folder = table.rsplit("\\", 1)[0]
    out = [table]
    for key, default in (("errorText", "ErrorText.csv"), ("fn14ErrorText", "ErrorTextFN14.csv")):
        name = _cfg_value(plc, key) or default
        for l in (lang, lang.capitalize(), lang.lower(), "ENGLISH", "english"):
            out.append(f"{folder}\\{l}\\{name}")
        out.append(f"{folder}\\{name}")
    for key in ("mainPgm", "compCfgFile"):
        v = _cfg_value(plc, key)
        if v:
            v = v.replace("%OEM%", "PLC:").replace("/", "\\").replace("\\\\", "\\")
            if v[:4].upper() == "PLC:":
                v = "PLC:" + v[4:]
            out.append(v)
    seen, res = set(), []
    for x in out:
        if x not in seen:                 # the control's file system is case-sensitive
            seen.add(x)
            res.append(x)
    return res


def fetch_plc_files(mcfg: dict, password: str, dest_root: str, progress=None) -> str:
    import pyLSV2
    say = progress or (lambda msg: None)
    dest = os.path.join(dest_root, f"plc_{mcfg['id']}")
    if os.path.isdir(dest):                  # replace an earlier copy completely
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    con = pyLSV2.LSV2(mcfg["host"], port=mcfg.get("port", 19000), timeout=mcfg.get("timeout_s", 10),
                      safe_mode=False)
    con.connect()
    try:
        if not _plc_login(con, password):
            raise RuntimeError("PLC login refused - wrong PLC code number? (nothing was read)")
        log.info("PLC login OK - listing PLC:\\ ...")
        say("login OK - listing files on the PLC drive ...")
        entries = []
        _walk(con, "PLC:\\", entries)
        with open(os.path.join(dest, "filelist.txt"), "w", encoding="utf-8") as f:
            for path, size, kind in entries:
                f.write(f"{kind}\t{size if size is not None else ''}\t{path}\n")
        files = [(p, s) for p, s, k in entries if k == "file"]
        log.info("%d folders, %d files on PLC:\\ (list in %s)", sum(1 for e in entries if e[2].startswith("dir")),
                 len(files), os.path.join(dest, "filelist.txt"))
        say(f"{len(files)} files found - copying text files ...")
        total = got = 0
        for path, size in files:
            ext = os.path.splitext(path)[1].lower()
            if ext in SKIP_EXT or _too_big(path, size):
                continue
            if total > MAX_TOTAL or got >= MAX_FILES:
                log.warning("download limit reached - stopped (see filelist.txt for the rest)")
                break
            local = os.path.join(dest, *path.replace("PLC:\\", "").split("\\"))
            os.makedirs(os.path.dirname(local), exist_ok=True)
            try:
                ok = con.recive_file(remote_path=path, local_path=local, override_file=True, binary_mode=True)
            except TypeError:
                ok = con.recive_file(path, local, True, True)
            except Exception as e:
                log.warning("  %s: %s", path, e)
                continue
            if ok and os.path.exists(local) and os.path.getsize(local) == 0 and size not in (0, None):
                os.remove(local)              # nothing came through - don't keep an empty file
                continue
            if ok and os.path.exists(local):
                got += 1
                total += os.path.getsize(local)
                if got % 10 == 0:
                    say(f"copied {got} files ({total / 1e6:.1f} MB) ...")
            time.sleep(0.02)              # be gentle with the control
        # the message table / texts named in plc.cfg - fetched directly, the listing may hide their folder
        say("fetching the PLC message table and program files named in plc.cfg ...")
        extra = []
        # their folders are hidden in the listing, but may still open directly: list + copy them too
        known = _known_message_files(dest)
        for folder in sorted({k.rsplit("\\", 1)[0] + "\\" for k in known}):
            sub = []
            try:
                _walk(con, folder, sub)
            except Exception as e:
                sub = [(folder, None, f"cannot open ({e})")]
            for path, size, kind in sub:
                extra.append(f"{kind}\t{size if size is not None else ''}\t{path}\t(direct folder)")
                if kind != "file":
                    continue
                if os.path.splitext(path)[1].lower() in SKIP_EXT or _too_big(path, size):
                    continue
                local = os.path.join(dest, *path.replace("PLC:\\", "").split("\\"))
                if os.path.exists(local):
                    continue
                os.makedirs(os.path.dirname(local), exist_ok=True)
                try:
                    ok = con.recive_file(remote_path=path, local_path=local, override_file=True, binary_mode=True)
                except TypeError:
                    ok = con.recive_file(path, local, True, True)
                except Exception:
                    ok = False
                if ok and os.path.exists(local) and os.path.getsize(local) > 0:
                    got += 1
                    total += os.path.getsize(local)
                elif os.path.exists(local):
                    os.remove(local)
                time.sleep(0.02)
        for path in known:
            local = os.path.join(dest, *path.replace("PLC:\\", "").split("\\"))
            if os.path.exists(local):
                continue
            os.makedirs(os.path.dirname(local), exist_ok=True)
            try:
                ok = con.recive_file(remote_path=path, local_path=local, override_file=True, binary_mode=True)
            except TypeError:
                ok = con.recive_file(path, local, True, True)
            except Exception:
                ok = False
            if ok and os.path.exists(local) and os.path.getsize(local) > 0:
                got += 1
                total += os.path.getsize(local)
                extra.append(f"file\t{os.path.getsize(local)}\t{path}\t(direct)")
                log.info("  got %s", path)
            else:
                if os.path.exists(local):
                    os.remove(local)
                extra.append(f"missing\t\t{path}\t(direct)")
            # don't leave empty folders behind for files that weren't there
            try:
                os.removedirs(os.path.dirname(local))
            except OSError:
                pass
        with open(os.path.join(dest, "filelist.txt"), "a", encoding="utf-8") as f:
            f.write("\n".join(extra) + "\n")
        log.info("downloaded %d files (%.1f MB) to %s", got, total / 1e6, dest)
        say(f"copied {got} files ({total / 1e6:.1f} MB)")
        try:
            con.logout(getattr(getattr(pyLSV2, "Login", None), "FILEPLC", None))
        except Exception:
            pass
    finally:
        try:
            con.disconnect()
        except Exception:
            pass
    return dest


def summarize(dest: str) -> dict:
    """What was copied: number of files and the likely message tables (.PET = PLC error table, texts)."""
    out = {"files": 0, "mb": 0.0, "tables": []}
    if not os.path.isdir(dest):
        return out
    size = 0
    for root, _, files in os.walk(dest):
        for f in files:
            if f == "filelist.txt":
                continue
            full = os.path.join(root, f)
            out["files"] += 1
            size += os.path.getsize(full)
            low = f.lower()
            if low.endswith((".pet", ".csv")) or any(k in low for k in ("errortext", "msg", "message", "alarm")):
                out["tables"].append(os.path.relpath(full, dest).replace(os.sep, "/"))
    out["mb"] = round(size / 1e6, 1)
    try:
        from . import plc_messages
        out["messages"] = len(plc_messages.load(dest) or [])
    except Exception:
        out["messages"] = 0
    try:
        from . import plc_messages
        sym = plc_messages.load_symbols(dest)
        out["symbols_built"] = os.path.exists(os.path.join(dest, "plc_symbols.json"))
        ent = sym.get("tool_in_spindle") or {}
        out["tool_in_spindle"] = ent.get("address") or (
            " or ".join(ent["candidates"]) + " (the one holding the last called tool)" if ent.get("candidates") else None)
        out["map_files"] = (sym.get("_info") or {}).get("map_files", [])
        out["symbol_hints"] = (sym.get("_info") or {}).get("hints", [])[:6]
    except Exception:
        pass
    out["tables"] = sorted(out["tables"])[:30]
    out["ts"] = os.path.getmtime(os.path.join(dest, "filelist.txt")) if os.path.exists(os.path.join(dest, "filelist.txt")) else None
    return out


class PlcJobs:
    """Runs fetch_plc_files in the background for the dashboard button (one job per machine at a time).
    The files stay on the monitor (data/plc_<id>/); they are the source for the machine's PLC message list."""

    def __init__(self, machines: list, data_dir: str):
        import threading
        self.machines = {m["id"]: m for m in machines if m.get("control") == "heidenhain"}
        self.data_dir = data_dir
        self.jobs: dict = {}
        self.lock = threading.Lock()
        # copies made before the clean-up existed: tidy them once at start
        from . import plc_messages
        for mid in self.machines:
            try:
                plc_messages.prune(self.dest(mid))
                if os.path.exists(os.path.join(self.dest(mid), "filelist.txt")):
                    plc_messages.build_symbols(self.dest(mid))     # search again with the current rules
            except Exception as e:
                log.warning("[%s] PLC file clean-up failed: %s", mid, e)

    def dest(self, mid: str) -> str:
        return os.path.join(self.data_dir, f"plc_{mid}")

    def status(self, mid: str) -> dict:
        with self.lock:
            j = dict(self.jobs.get(mid) or {"state": "idle"})
        j["supported"] = mid in self.machines
        if j["state"] != "running":
            j["stored"] = summarize(self.dest(mid))
        return j

    def start(self, mid: str, password: str) -> dict:
        import threading
        m = self.machines.get(mid)
        if m is None:
            raise ValueError("not a Heidenhain machine")
        with self.lock:
            if (self.jobs.get(mid) or {}).get("state") == "running":
                return self.jobs[mid]
            job = {"state": "running", "msg": "connecting ...", "started": time.time()}
            self.jobs[mid] = job

        def say(msg):
            with self.lock:
                job["msg"] = msg

        def run():
            try:
                dest = fetch_plc_files(m, password, self.data_dir, say)
                say("building the PLC message list ...")
                from . import plc_messages
                res = plc_messages.build(dest)
                if res.get("messages"):
                    say("cleaning up files that are not needed ...")
                    plc_messages.prune(dest)
                tool = res.get("tool_in_spindle")
                tool_msg = f" · tool in spindle at {tool}" if tool else " · tool in spindle: not found"
                with self.lock:
                    job.update(state="done", ended=time.time(),
                               msg=(f"copied - {res.get('messages', 0)} PLC messages ready" if res.get("messages")
                                    else f"copied - no PLC message list ({res.get('reason', 'no markers with a fixed address')})")
                               + tool_msg)
            except Exception as e:
                log.warning("[%s] PLC file copy failed: %s", mid, e)
                with self.lock:
                    job.update(state="error", msg=str(e), ended=time.time())

        threading.Thread(target=run, daemon=True, name=f"plc-fetch-{mid}").start()
        return job

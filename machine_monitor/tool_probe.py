"""Read-only probe: which tool data (current tool, tool life) a control offers.

    python run.py --probe-tools <machine id>

Nothing is written to the control. Each control gets its own connection, closed afterwards.

FANUC      current T code (macro #4120) + tool life management (tool groups, e.g. T0199 = group 1):
           cnc_rdtlinfo, cnc_rdngrp, cnc_rdtlusegrp, cnc_rdtlgrp, cnc_rdtltool - all read functions from
           FANUC's fwlib32.h. Only groups that exist are read (1 .. number of groups the control reports).
HEIDENHAIN tool in the spindle (if pyLSV2 offers it) + the tool table TNC:\\table\\tool.t (read as a file):
           TIME1 / TIME2 / CUR_TIME (tool life), TL (locked), RT (replacement tool).
SINUMERIK  active tool (/Channel/State/actTNumber ...), a few tool management paths, and the names of
           tool related objects in the OPC UA tree.
"""
from __future__ import annotations

import ctypes as C
import os
import tempfile


def probe(m: dict, out=print) -> None:
    ctrl = m.get("control")
    if ctrl == "fanuc":
        _fanuc(m, out)
    elif ctrl == "heidenhain":
        _heidenhain(m, out)
    elif ctrl == "siemens":
        _siemens(m, out)
    else:
        out(f"no tool probe for control '{ctrl}'")


# ----------------------------------------------------------------------------------------------- FANUC
class ODBM(C.Structure):
    _fields_ = [("datano", C.c_short), ("dummy", C.c_short), ("mcr_val", C.c_int32), ("dec_val", C.c_short)]


class ODBTLINFO(C.Structure):
    _fields_ = [("max_group", C.c_int32), ("max_tool", C.c_int32), ("max_minute", C.c_int32),
                ("max_cycle", C.c_int32)]


class ODBTLIFE2(C.Structure):
    _fields_ = [("dummy", C.c_short * 2), ("data", C.c_int32)]


class ODBUSEGRP(C.Structure):
    _fields_ = [("next", C.c_int32), ("use", C.c_int32), ("slct", C.c_int32),
                ("opt_next", C.c_int32), ("opt_use", C.c_int32), ("opt_slct", C.c_int32)]


class IODBTLGRP(C.Structure):
    _fields_ = [("ntool", C.c_int32), ("nfree", C.c_int32), ("life", C.c_int32), ("count", C.c_int32),
                ("use_tool", C.c_int32), ("opt_grpno", C.c_int32), ("life_rest", C.c_int32),
                ("rest_sig", C.c_short), ("count_type", C.c_short)]


class IODBTLTOOL(C.Structure):
    _fields_ = [("tool_num", C.c_int32), ("h_code", C.c_int32), ("d_code", C.c_int32), ("tool_inf", C.c_int32)]


MAX_GROUPS_SHOWN = 40
MAX_TOOLS_PER_GROUP = 16


def _fanuc(m, out):
    from .drivers import fanuc_focas as F
    drv = F.FanucFocasDriver(dict(m))
    drv.connect()
    L, h = drv.lib, drv.h.value
    try:
        def fn(name, argtypes):
            if not hasattr(L, name):
                return None
            f = getattr(L, name)
            f.argtypes, f.restype = argtypes, C.c_short
            return f

        # current tool: macro #4120 = T code of the active tool (lathe: T0199 -> 199)
        rd = fn("cnc_rdmacro", [C.c_ushort, C.c_short, C.c_short, C.POINTER(ODBM)])
        if rd:
            mv = ODBM()
            ret = rd(h, 4120, C.sizeof(ODBM), C.byref(mv))
            val = mv.mcr_val / (10 ** mv.dec_val) if mv.dec_val > 0 else mv.mcr_val
            out(f"current T code (#4120): {val:g}   (ret {ret})")

        info = fn("cnc_rdtlinfo", [C.c_ushort, C.POINTER(ODBTLINFO)])
        ti = ODBTLINFO()
        ret = info(h, C.byref(ti)) if info else None
        out(f"tool life management info: ret {ret}  max groups {ti.max_group}, max tools/group {ti.max_tool}, "
            f"max life {ti.max_minute} min / {ti.max_cycle} cycles")
        if ret not in (0, None):
            out("   -> tool life management not available or not used on this control (ret 6 = option not "
                "installed). Stop here.")
            return

        ngrp = fn("cnc_rdngrp", [C.c_ushort, C.POINTER(ODBTLIFE2)])
        ng = ODBTLIFE2()
        ret = ngrp(h, C.byref(ng)) if ngrp else None
        n_groups = ng.data if ret == 0 else 0
        out(f"number of tool groups set up: {n_groups}   (cnc_rdngrp ret {ret})")

        use = fn("cnc_rdtlusegrp", [C.c_ushort, C.POINTER(ODBUSEGRP)])
        ug = ODBUSEGRP()
        if use and use(h, C.byref(ug)) == 0:
            out(f"group in use: {ug.use}   selected: {ug.slct}   next: {ug.next}")

        if n_groups <= 0:
            return
        rdg = fn("cnc_rdtlgrp", [C.c_ushort, C.c_int32, C.POINTER(C.c_short), C.POINTER(IODBTLGRP)])
        rdt = fn("cnc_rdtltool", [C.c_ushort, C.c_int32, C.c_int32, C.POINTER(C.c_short), C.POINTER(IODBTLTOOL)])
        upto = min(n_groups, ti.max_group or n_groups, MAX_GROUPS_SHOWN)
        out(f"\ngroup  type    life   count   rest  tools free  in use  | tools (number:H/D:status)")
        for g in range(1, upto + 1):
            num = C.c_short(1)                      # read exactly one group into a one-group buffer
            gd = IODBTLGRP()
            ret = rdg(h, g, C.byref(num), C.byref(gd))
            if ret != 0 or num.value < 1:
                out(f"{g:>5}  ret {ret}")
                continue
            kind = {0: "cycles", 1: "minutes"}.get(gd.count_type, f"type {gd.count_type}")
            tools = ""
            if rdt and gd.ntool > 0:
                k = min(gd.ntool, MAX_TOOLS_PER_GROUP, ti.max_tool or MAX_TOOLS_PER_GROUP)
                buf = (IODBTLTOOL * k)()
                tn = C.c_short(k)                   # buffer holds exactly k tools
                if rdt(h, g, 1, C.byref(tn), buf) == 0:
                    tools = "  ".join(f"T{t.tool_num}:{t.h_code}/{t.d_code}:{t.tool_inf}"
                                      for t in buf[:max(0, tn.value)])
            out(f"{g:>5}  {kind:<7}{gd.life:>6}  {gd.count:>6}  {gd.life_rest:>5}  {gd.ntool:>5} {gd.nfree:>4}"
                f"  {gd.use_tool:>6}  | {tools}")
        if n_groups > upto:
            out(f"... {n_groups - upto} more groups not shown")
        out("\nstatus per tool (tool_inf): usually 0 = not used yet, 1 = in use, 2 = life expired, 3 = skipped")
    finally:
        drv.close()


# ------------------------------------------------------------------------------------------ HEIDENHAIN
TOOL_TABLE = "TNC:\\table\\tool.t"
SHOW_COLS = ("T", "NAME", "L", "R", "TIME1", "TIME2", "CUR_TIME", "TL", "RT", "LAST_USE", "CUT")


def parse_tnc_table(text: str):
    """HEIDENHAIN .t table: 'BEGIN ...' line, a header line with fixed-width column names, then rows."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], []
    head = lines[1]
    cols, starts = [], []
    i = 0
    while i < len(head):
        if head[i] != " ":
            j = i
            while j < len(head) and head[j] != " ":
                j += 1
            cols.append(head[i:j])
            starts.append(i)
            i = j
        else:
            i += 1
    rows = []
    for ln in lines[2:]:
        if ln.startswith("[END]") or ln.startswith("END"):
            break
        row = {}
        for k, c in enumerate(cols):
            a = starts[k]
            b = starts[k + 1] if k + 1 < len(cols) else len(ln)
            row[c] = ln[a:b].strip()
        rows.append(row)
    return cols, rows


def _heidenhain(m, out):
    import pyLSV2
    con = pyLSV2.LSV2(m["host"], port=m.get("port", 19000), timeout=m.get("timeout_s", 10), safe_mode=False)
    con.connect()
    try:
        out(f"pyLSV2 {getattr(pyLSV2, '__version__', '?')}, control: {getattr(getattr(con, 'versions', None), 'control', '?')}")
        # route 1: pyLSV2 spindle_tool_status, after the DNC login (the monitoring driver uses it too)
        try:
            if hasattr(con, "login") and hasattr(pyLSV2, "Login"):
                out(f"DNC login: {con.login(pyLSV2.Login.DNC)}")
        except Exception as e:
            out(f"DNC login: {e}")
        if hasattr(con, "spindle_tool_status"):
            try:
                r = con.spindle_tool_status()
                out(f"route 1 spindle_tool_status: {getattr(r, '__dict__', r)!r}")
                from .drivers.heidenhain_lsv2 import _tool_number
                out("   -> monitor uses: " + ("DIRECT route (tool number from here)" if _tool_number(r) is not None
                                             else "PLC route (DG_TOOL_IN_SPINDLE + tool table)"))
                le = getattr(con, "last_error", None)
                if r is None and le is not None:
                    out(f"   last error: {le!r}")
            except Exception as e:
                out(f"route 1 spindle_tool_status: error {e}")
        else:
            out("route 1: this pyLSV2 version has no spindle_tool_status()")
        # route 2: data paths (documented for iTNC; try whether this control answers)
        if hasattr(con, "read_data_path"):
            for path in ("/TABLE/TOOL/T/9/NAME", "/TABLE/TOOL/T/9/CUR_TIME", "/TABLE/TOOL/T/9/CUR.TIME",
                         "/TABLE/TOOL/T/9/TIME1", "/TABLE/TOOL/T/9/TIME2", "/TABLE/TOOL/T/9/TL"):
                try:
                    out(f"route 2 read_data_path {path}: {con.read_data_path(path)!r}")
                except Exception as e:
                    out(f"route 2 read_data_path {path}: error {e}")
        fd, tmp = tempfile.mkstemp(suffix=".t")
        os.close(fd)
        try:
            try:
                ok = con.recive_file(remote_path=TOOL_TABLE, local_path=tmp, override_file=True, binary_mode=False)
            except TypeError:
                ok = con.recive_file(TOOL_TABLE, tmp, True, False)
            if not ok:
                out(f"tool table {TOOL_TABLE}: cannot read")
                return
            text = open(tmp, encoding="latin-1").read()
        finally:
            os.remove(tmp)
        raw = [ln for ln in text.splitlines() if ln.strip()]
        out("raw header line : " + (raw[1] if len(raw) > 1 else ""))
        cols, rows = parse_tnc_table(text)
        out(f"tool table {TOOL_TABLE}: {len(rows)} tools, {len(cols)} columns")
        out("columns: " + " ".join(cols))
        shown = [c for c in SHOW_COLS if c in cols]
        with_life = [r for r in rows if _num(r.get("TIME1")) or _num(r.get("TIME2"))]
        out(f"tools with a life time set (TIME1/TIME2 > 0): {len(with_life)}")
        out("\n" + "  ".join(f"{c:<10}" for c in shown))
        for r in (with_life or rows)[:25]:
            out("  ".join(f"{r.get(c, ''):<10}" for c in shown))
        for r in with_life[:3]:                          # raw line, to check the column positions
            line = next((ln for ln in raw[2:] if ln.split()[:1] == [r.get("T")]), "")
            out("raw tool line   : " + line)

        # route 5: the PLC double word DG_TOOL_IN_SPINDLE (HEIDENHAIN example PLC: D16356 or D17032)
        _plc_tool_in_spindle(con, m, out, {r.get("T"): r.get("NAME", "") for r in rows})

        # route 3: tool related symbols in the machine builder's PLC files copied earlier (data/plc_<id>/)
        _plc_tool_symbols(con, m, out)
    finally:
        try:
            con.disconnect()
        except Exception:
            pass


TOOL_IN_SPINDLE_D = (16356, 17032)       # DG_TOOL_IN_SPINDLE in two versions of HEIDENHAIN's example PLC


def _plc_tool_in_spindle(con, m, out, names):
    """Read-only: the PLC double words that hold the tool in the spindle in HEIDENHAIN's example PLC
    (<Global>\\SPINDLE[0].DG_TOOL_IN_SPINDLE). Which one applies depends on the PLC version, so read both and
    compare with the tool that is really in the spindle. Override with TOOL_PROBE_D=16356,17032."""
    import re
    addrs = TOOL_IN_SPINDLE_D
    if os.environ.get("TOOL_PROBE_D"):
        addrs = tuple(int(a.strip().lstrip("Dd")) for a in os.environ["TOOL_PROBE_D"].split(",") if a.strip())
    # does the copied PLC (data/plc_<id>/) name the symbol? then that address is the one
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", f"plc_{m['id']}")
    hits = []
    if os.path.isdir(root):
        for dp, _, fn in os.walk(root):
            for f in fn:
                try:
                    for ln in open(os.path.join(dp, f), encoding="latin-1"):
                        if re.search(r"TOOL_IN_SPINDLE", ln, re.I):
                            hits.append(f"{f}: {ln.strip()[:120]}")
                except OSError:
                    pass
    out(f"\nroute 5: DG_TOOL_IN_SPINDLE in the copied PLC files: {len(hits)} line(s)")
    for h in hits[:10]:
        out("   " + h)
    try:
        from pyLSV2.const import MemoryType
        mt = MemoryType.DWORD
    except Exception:
        import pyLSV2
        mt = getattr(pyLSV2, "PLC_MEM_TYPE_DWORD", 4)
    for a in addrs:
        try:
            v = con.read_plc_memory(a // 4, mt, 1)       # pyLSV2 counts double words, D16356 = element 4089
            t = v[0] if isinstance(v, (list, tuple)) and v else v
            name = names.get(str(t), "") if t is not None else ""
            out(f"route 5: D{a} = {t!r}" + (f"   -> tool table: T{t} {name}" if name else ""))
        except Exception as e:
            out(f"route 5: D{a} read: {e}")
            le = getattr(con, "last_error", None)
            if le is not None:
                out(f"   last error: {le!r}")


def _plc_tool_symbols(con, m, out):
    """HEIDENHAIN's API symbol files (Api*.def, copied with the PLC files) list NC data offered to the PLC with
    their addresses, e.g. 'nn_ChnAfcSpindleLoad (corresponds to W330)'. Print every tool-related line, and read
    the value of addresses written as 'corresponds to W330' / 'W330' outside a comment."""
    import re
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", f"plc_{m['id']}")
    if not os.path.isdir(root):
        out(f"\nroute 3: no copied PLC files in {root} (copy them via the detail view first)")
        return
    pat = re.compile(r"tool|tnr|t_no|toolno|t.?call|t.?code|tcd|t.?num|pocket|magaz|spindle|spdl|\bT\b", re.I)
    addr = re.compile(r"(?:corr?esponds to|cooresponds to)\s+([BWD])(\d+)", re.I)
    lines = []
    for dp, _, fn in os.walk(root):
        for f in sorted(fn):
            if f.lower().startswith("api") and f.lower().endswith(".def"):
                try:
                    for ln in open(os.path.join(dp, f), encoding="latin-1"):
                        if pat.search(ln):
                            lines.append((f, ln.rstrip()))
                except OSError:
                    pass
    out(f"\nroute 3: {len(lines)} tool lines in HEIDENHAIN's API symbol files (Api*.def):")
    try:
        from pyLSV2.const import MemoryType
    except Exception:
        MemoryType = None
    seen = set()
    for f, ln in lines[:120]:
        key = ln.strip()
        if key in seen:
            continue
        seen.add(key)
        val = ""
        a = addr.search(ln)
        if a and MemoryType is not None:
            kind, n = a.group(1).upper(), int(a.group(2))
            mt = {"B": "BYTE", "W": "WORD", "D": "DWORD"}[kind]
            try:
                val = f"   -> {kind}{n} = {con.read_plc_memory(n // {'B': 1, 'W': 2, 'D': 4}[kind], getattr(MemoryType, mt), 1)}"
            except Exception as e:
                val = f"   -> {kind}{n} read: {e}"
        out(f"   {f:<16} {key[:110]}{val}")


def _num(v):
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


# ------------------------------------------------------------------------------------------- SINUMERIK
SIEMENS_PATHS = [
    "/Channel/State/actTNumber", "/Channel/State/actDNumber", "/Channel/State/actToolIdent",
    "/Channel/State/actDuploNumber", "/Channel/State/actToolLength1", "/Channel/State/actToolRadius",
    "/Tool/Catalogue/numTools", "/Tool/Catalogue/toolIdent[u1,1]", "/Tool/Catalogue/duploNo[u1,1]",
    "/Tool/Supervision/data[u1,c1,1]", "/Tool/Supervision/data[u1,1]", "/Tool/MagazineConfiguration/numMagazines",
]


def _siemens(m, out):
    from .drivers.siemens_opcua import SiemensOpcUaDriver
    drv = SiemensOpcUaDriver(dict(m))
    drv.connect()
    try:
        cl = drv.client
        for p in SIEMENS_PATHS:
            try:
                out(f"{p:<45} = {cl.get_node(drv._nid(p)).read_value()!r}")
            except Exception as e:
                out(f"{p:<45}   not readable ({str(e).split(':')[0][:60]})")
        try:
            t = int(cl.get_node(drv._nid("/Channel/State/actTNumber")).read_value())
        except Exception:
            t = 0
        if t > 0:
            out(f"\ntool monitoring of the tool in the spindle (T{t}), /Tool/Supervision/data[u1,c<k>,{t}]:")
            vals = []
            for k in range(1, 14):
                try:
                    vals.append(f"c{k}={cl.get_node(drv._nid(f'/Tool/Supervision/data[u1,c{k},{t}]')).read_value()!r}")
                except Exception:
                    vals.append(f"c{k}=-")
            out("   " + "  ".join(vals))
            for p in (f"/Tool/Catalogue/toolIdent[u1,{t}]", f"/Tool/Data/data[u1,c9,{t}]", f"/Tool/Data/data[u1,c8,{t}]",
                      f"/Tool/Catalogue/toolInMag[u1,{t}]", f"/Tool/Catalogue/toolInPlace[u1,{t}]"):
                try:
                    out(f"   {p:<42} = {cl.get_node(drv._nid(p)).read_value()!r}")
                except Exception as e:
                    out(f"   {p:<42}   not readable ({str(e).split(':')[0][:50]})")
        for p in (f"/Tool/Data/toolMon[u1,{t}]", f"/Tool/Data/toolState[u1,{t}]"):
            try:
                out(f"   {p:<42} = {cl.get_node(drv._nid(p)).read_value()!r}")
            except Exception as e:
                out(f"   {p:<42}   not readable ({str(e).split(':')[0][:50]})")
        # scan: catalogue position i -> tool number (toolNo) + name (toolIdent); monitoring per TOOL NUMBER
        try:
            n = int(cl.get_node(drv._nid("/Tool/Catalogue/numTools")).read_value())
        except Exception:
            n = 0

        def batch(paths):
            vals = []
            for i in range(0, len(paths), 50):
                nodes = [cl.get_node(drv._nid(x)) for x in paths[i:i + 50]]
                try:
                    vals += cl.read_values(nodes)
                except Exception:
                    for nd in nodes:
                        try:
                            vals.append(nd.read_value())
                        except Exception:
                            vals.append(None)
            return vals
        if n:
            tnos = batch([f"/Tool/Catalogue/toolNo[u1,{i}]" for i in range(1, n + 1)])
            names = batch([f"/Tool/Catalogue/toolIdent[u1,{i}]" for i in range(1, n + 1)])
            if all(v is None for v in tnos):
                out("\n/Tool/Catalogue/toolNo not readable - cannot map catalogue entries to tool numbers")
            else:
                pairs = [(int(t), str(nm)) for t, nm in zip(tnos, names) if t]
                mons = batch([f"/Tool/Data/toolMon[u1,{t}]" for t, _ in pairs])
                on = [(t, nm, mv) for (t, nm), mv in zip(pairs, mons) if mv]
                out(f"\ntools with monitoring switched on: {len(on)} of {len(pairs)} "
                    f"(toolMon: 1 = time, 2 = count, 4 = wear)")
                for t, nm, mv in on[:15]:
                    sup = []
                    for c in (1, 2, 3, 4, 11, 13):
                        v = batch([f"/Tool/Supervision/data[u1,c{c},{t}]"])[0]
                        sup.append(f"c{c}={v if v is not None else '-'}")
                    out(f"   T{t:<5} {nm:<24} toolMon={mv}   " + " ".join(sup))
        try:
            kids = cl.get_node("ns=2;s=/Tool").get_children()
            out("\nareas under /Tool: " + ", ".join(k.read_browse_name().Name for k in kids[:40]))
        except Exception as e:
            out(f"\nareas under /Tool: cannot list ({e})")
        found = 0
        if os.environ.get("TOOL_PROBE_TREE"):
            out("\ntool related objects in the OPC UA tree:")

        def walk(node, d, path):
            nonlocal found
            if d > 4 or found > 80:
                return
            try:
                kids = node.get_children()
            except Exception:
                return
            for ch in kids[:200]:
                try:
                    name = ch.read_browse_name().Name
                except Exception:
                    continue
                p = f"{path}/{name}"
                if "adapt" in p.lower():
                    continue
                if "tool" in name.lower() or "tool" in path.lower():
                    found += 1
                    out(f"   {p}   [{ch.nodeid.to_string()}]")
                if name not in ("Server", "FileSystem", "Methods") and (d < 2 or "tool" in p.lower()):
                    walk(ch, d + 1, p)
        if os.environ.get("TOOL_PROBE_TREE"):
            walk(cl.nodes.objects, 1, "")
    finally:
        drv.close()

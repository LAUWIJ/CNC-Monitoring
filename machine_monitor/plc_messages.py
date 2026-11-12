"""PLC message list for a HEIDENHAIN control, built from the files the dashboard copied (data/plc_<id>/).

  PLC:\\plc\\language\\PlcError.pet   NR | ERROR (text) | MARKER (symbol name) | ...
  PLC:\\plc\\program\\*.def           symbol -> marker address, e.g.
        #type M
           PN_error                     4800[500]
        /c PN_error_circuit_breaker     &PN_error +  000

When the PLC sets such a marker, the control shows that message. So reading markers M4800..M5299 tells us
which machine-builder messages ("Machine guard is open !", "Robot interlock", ...) are active right now.
Only markers with a FIXED address can be read; symbols that the PLC compiler places itself are skipped.
"""
from __future__ import annotations

import glob
import json
import os
import re

_BASE = re.compile(r"^\s*([A-Za-z_]\w*)\s+(\d+)\s*(?:\[(\d+)\])?\s*(?:;.*)?$")
_MEMBER = re.compile(r"^\s*/c\s+([A-Za-z_]\w*)\s+&([A-Za-z_]\w*)\s*\+\s*(\d+)")
_DIRECT = re.compile(r"^\s*(?:/c\s+)?([A-Za-z_]\w*)\s+M(\d+)\b", re.I)


def parse_def_files(paths) -> dict:
    """symbol -> marker address (only fixed addresses, only '#type M' sections)."""
    bases, members, out = {}, [], {}
    for p in paths:
        try:
            with open(p, encoding="latin-1") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        typ = None
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("*"):
                continue
            if s.lower().startswith("#type"):
                typ = s.split()[1].upper() if len(s.split()) > 1 else None
                continue
            m = _MEMBER.match(ln)
            if m:
                if typ == "M":
                    members.append((m.group(1), m.group(2), int(m.group(3))))
                continue
            if typ != "M":
                continue
            m = _BASE.match(ln)
            if m:
                bases[m.group(1)] = int(m.group(2))
                out[m.group(1)] = int(m.group(2))
                continue
            m = _DIRECT.match(ln)
            if m:
                out[m.group(1)] = int(m.group(2))
    for name, base, off in members:
        if base in bases:
            out[name] = bases[base] + off
    return out


def parse_pet(path: str) -> list:
    """Rows of a TNC .PET table: [{nr, text, marker}] (fixed-width columns, taken from the header line)."""
    with open(path, encoding="latin-1") as f:
        lines = f.read().splitlines()
    head = next((i for i, l in enumerate(lines) if l.startswith("NR")), None)
    if head is None:
        return []
    hdr = lines[head]
    cols = [(m.group(0), m.start()) for m in re.finditer(r"\S+", hdr)]
    pos = {n: i for i, (n, _) in enumerate(cols)}

    def cell(line, name):
        i = pos.get(name)
        if i is None:
            return ""
        a = cols[i][1]
        b = cols[i + 1][1] if i + 1 < len(cols) else len(line)
        return line[a:b].strip()

    rows = []
    for l in lines[head + 1:]:
        if l.startswith("[END]"):
            break
        nr = cell(l, "NR")
        if not nr.isdigit():
            continue
        text, marker = cell(l, "ERROR"), cell(l, "MARKER")
        if not marker:
            continue
        text = re.sub(r"^#\d+\s*", "", text).strip() or marker
        rows.append({"nr": int(nr), "text": text, "marker": marker})
    return rows


# symbols the monitor reads besides the messages; their address is placed by the PLC compiler, so it is only
# in the compiler's output (MainPgm.map / the "PlcDataMarker" list), e.g.
#   <Global>\SPINDLE[0].DG_TOOL_IN_SPINDLE   D17032   (HEIDENHAIN example PLC; D16356 in another version)
SYMBOLS = {"tool_in_spindle": "DG_TOOL_IN_SPINDLE"}
# the address must FOLLOW the symbol directly ("...DG_TOOL_IN_SPINDLE   D17032"); a looser match took "B01" from
# further along a line on the DVF BMO. DG_ = double word, so only a D address is accepted for it.
_SYM_ADDR = r"\b{sym}\b\s*[:=]?\s*({kind})(\d+)\b"


# the struct variable a symbol usually lives in when no PLC source names it (HEIDENHAIN example PLC: Spindle[..])
STRUCT_DEFAULTS = {"tool_in_spindle": ["SPINDLE"]}


def map_address(data: bytes, member: str, struct_vars) -> str | None:
    """Address of a symbol from the PLC compiler's BINARY map file ('PLCCOMP V 4.10 MAPFILE', TNC 640).
    Plain global:  NAME, zero padding, type letter, 0x00, uint32 address  (e.g. DG_TEMPERATURE_SPINDLE D 8220)
    Struct member: type letter + 3 zero bytes, uint32 offset in the struct, uint32, NAME, 0x00
    Struct global: NAME, zero padding, 's', 0x00, uint32 base address, struct type name
    -> member address = base of the struct variable (element [0]) + offset. Verified on the DVF BMO:
    SPINDLE (s, base 16632) + DG_TOOL_IN_SPINDLE (D, offset 400) = D17032, the address that holds T59."""
    import struct as st
    up = member.upper().encode()

    def glob(name):
        m = re.search(rb"(?<![A-Z0-9_])" + re.escape(name) + rb"\x00{2,40}([sSBWDMKT])\x00(.{4})", data, re.S)
        return (m.group(1).decode(), st.unpack("<I", m.group(2))[0]) if m else None

    g = glob(up)
    if g and g[0] in "BWD":
        return f"{g[0]}{g[1]}"
    members = [(m.group(1).decode(), st.unpack("<I", m.group(2))[0])
               for m in re.finditer(rb"([BWDM])\x00\x00\x00(.{4}).{4}" + re.escape(up) + rb"\x00", data, re.S)]
    if not members:
        return None
    kind, off = members[0]
    for var in struct_vars:
        b = glob(var.upper().encode())
        if b and b[0] == "s":
            return f"{kind}{b[1] + off}"
    return None


def listing_addresses(path: str, member: str) -> list:
    """Addresses of a symbol from the PLC compiler LISTING (MainPgm.lst, 'PLCCOMP V6.21 ...', DVF Erowa).
    The listing gives struct members without an address, but every place that takes the ADDRESS of the symbol,
    e.g. '076589 8B 46 04 83 E8 04  73  PS  K&SPINDLE[0].DG_TOOL_IN_SPINDLE', is followed by the compiled code,
    which stores it as a constant: 'C7 04 02 E4 3F 00 00' = mov [edx+eax], 0x00003FE4 = 16356 (verified: D16356
    holds the tool in the spindle on the Erowa). Returns the distinct addresses as ['D16356', ...]."""
    import struct as st
    kind = {"DG": "D", "WG": "W", "BG": "B"}.get(member[:2].upper(), "D")
    ref = re.compile(r"K\s*&\s*(?:\w+\s*\[\s*0\s*\]\s*\.\s*)?" + re.escape(member) + r"\b", re.I)
    hexline = re.compile(r"^(?:[0-9A-F]{6} )?((?:[0-9A-F]{2} ){1,6})", re.I)
    found = []
    try:
        fh = open(path, encoding="latin-1")
    except OSError:
        return found
    with fh:
        grab = None
        for line in fh:
            if grab is not None:
                m = hexline.match(line.strip("\r\n").lstrip(" ") if not re.match(r"^[0-9A-F]{6} ", line) else line)
                if m and not re.match(r"^[0-9A-F]{6} ", line):
                    grab += bytes.fromhex(m.group(1).replace(" ", ""))
                    continue
                i = grab.find(b"\xC7\x04\x02")
                if i >= 0 and len(grab) >= i + 7:
                    a = f"{kind}{st.unpack('<I', grab[i + 3:i + 7])[0]}"
                    if a not in found:
                        found.append(a)
                grab = None
            if ref.search(line) and re.match(r"^[0-9A-F]{6} ", line):
                m = hexline.match(line)
                grab = bytes.fromhex(m.group(1).replace(" ", "")) if m else b""
    return found


def find_symbols(dest: str) -> dict:
    """{key: {"address": "D17032", "source": file}} for the SYMBOLS, plus "_info": which map files were copied,
    the struct variables the PLC sources use them in, and up to 12 source lines that name them.
    The address comes from the compiler's binary map file (map_address); a text form
    '...DG_TOOL_IN_SPINDLE  D17032' (HEIDENHAIN's marker list) is accepted too."""
    found, hints, maps = {}, [], []
    old = (load_symbols(dest).get("_info") or {}).get("struct_vars") or {}
    svars = {k: list(old.get(k, [])) for k in SYMBOLS}
    names = re.compile("|".join(re.escape(v) for v in SYMBOLS.values()), re.I)
    kinds = {"DG": "D", "WG": "W", "BG": "B", "MG": "M"}
    pats = {k: re.compile(_SYM_ADDR.format(sym=v, kind=kinds.get(v[:2].upper(), "[BWD]")), re.I)
            for k, v in SYMBOLS.items()}
    use = {k: re.compile(r"(\w+)\s*(?:\[[^\]]*\])?\s*\.\s*" + re.escape(v) + r"\b", re.I) for k, v in SYMBOLS.items()}
    for root, _, files in os.walk(dest):
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(root, f), dest)
            if f.lower().endswith(".map"):
                maps.append(rel)
                try:
                    with open(os.path.join(root, f), "rb") as fh:
                        if fh.read(7) == b"PLCCOMP":
                            continue         # binary compiler map: read below with map_address
                except OSError:
                    continue
            if f.lower().endswith((".json", ".pet", ".ptx", ".def", ".txt", "._pj")):
                continue                     # .def files only give the type (DG_x  D), never the address
            try:
                with open(os.path.join(root, f), encoding="latin-1") as fh:
                    for line in fh:
                        if not names.search(line):
                            continue
                        if len(hints) < 12:
                            hints.append(f"{rel}: {line.strip()[:160]}")
                        for k, pat in pats.items():
                            u = use[k].search(line)
                            if u and u.group(1).upper() not in svars[k]:
                                svars[k].append(u.group(1).upper())
                            m = pat.search(line)
                            if not m:
                                continue
                            addr = f"{m.group(1).upper()}{m.group(2)}"
                            first = "[0]" in line[:m.start()] or "[" not in line[:m.start()]
                            if k not in found or (first and not found[k][1]):
                                found[k] = (addr, first, rel)
            except OSError:
                pass
    for rel in maps:
        try:
            with open(os.path.join(dest, rel), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        for k, v in SYMBOLS.items():
            a = map_address(data, v, svars[k] + [x for x in STRUCT_DEFAULTS.get(k, []) if x not in svars[k]])
            if a:
                found[k] = (a, True, rel)            # the compiler's own output wins over text matches
    cands = {}
    for root, _, files in os.walk(dest):
        for f in sorted(files):
            if not f.lower().endswith(".lst"):
                continue
            rel = os.path.relpath(os.path.join(root, f), dest)
            maps.append(rel)
            for k, v in SYMBOLS.items():
                if k in found and found[k][2].lower().endswith(".map"):
                    continue
                for a in listing_addresses(os.path.join(root, f), v):
                    cands.setdefault(k, (rel, []))[1].append(a) if a not in cands.get(k, ("", []))[1] else None
    out = {k: {"address": v[0], "source": v[2]} for k, v in found.items()}
    for k, (rel, lst) in cands.items():
        if len(lst) == 1:
            out[k] = {"address": lst[0], "source": rel}
        elif lst:                            # several: the driver keeps the one holding the last called tool
            out[k] = {"candidates": lst, "source": rel}
    out["_info"] = {"map_files": maps, "hints": hints, "struct_vars": svars}
    return out


def build_symbols(dest: str) -> dict:
    """Write dest/plc_symbols.json (kept after the clean-up). Returns {key: "D17032"}."""
    syms = find_symbols(dest)
    with open(os.path.join(dest, "plc_symbols.json"), "w", encoding="utf-8") as f:
        json.dump(syms, f, indent=1)
    return {k: v.get("address") or "/".join(v.get("candidates", [])) for k, v in syms.items() if not k.startswith("_")}


def load_symbols(dest: str) -> dict:
    try:
        with open(os.path.join(dest, "plc_symbols.json"), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def build(dest: str) -> dict:
    """Create dest/messages.json (and plc_symbols.json) from the copied files. Returns a small summary."""
    try:
        syms = build_symbols(dest)
    except Exception:
        syms = {}
    res = _build_messages(dest)
    res.update(syms)
    return res


def _build_messages(dest: str) -> dict:
    pets = sorted(glob.glob(os.path.join(dest, "plc", "language", "*.pet")) +
                  glob.glob(os.path.join(dest, "plc", "language", "*.PET")))
    pet = next((p for p in pets if os.path.basename(p).lower() == "plcerror.pet"), pets[0] if pets else None)
    if not pet:
        return {"messages": 0, "reason": "no PlcError.pet copied"}
    defs = glob.glob(os.path.join(dest, "plc", "program", "*.def")) + \
        glob.glob(os.path.join(dest, "plc", "program", "*.DEF"))
    addr = parse_def_files(defs)
    rows = parse_pet(pet)
    msgs, unknown = [], 0
    for r in rows:
        a = addr.get(r["marker"])
        if a is None:
            unknown += 1
            continue
        msgs.append({**r, "address": a})
    with open(os.path.join(dest, "messages.json"), "w", encoding="utf-8") as f:
        json.dump({"source": os.path.relpath(pet, dest), "messages": msgs}, f, indent=0)
    return {"messages": len(msgs), "without_address": unknown}


def load(dest: str):
    """[{nr, text, marker, address}] or None when no list has been built."""
    p = os.path.join(dest, "messages.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("messages") or None
    except (OSError, ValueError):
        return None


# what has to stay after the message list is built (enough to build it again); the rest is deleted
KEEP = ("messages.json", "plc_symbols.json", "filelist.txt", "marker_snapshot_v2.json")


def prune(dest: str) -> dict:
    """Delete the copied PLC files that are not needed any more. Only when messages.json exists."""
    import shutil
    if not load(dest):
        return {"deleted": 0}
    keep = set()
    for name in KEEP:
        keep.add(os.path.normcase(os.path.join(dest, name)))
    for rel in (("config", "plc.cfg"), ("config", "oem.cfg")):
        keep.add(os.path.normcase(os.path.join(dest, *rel)))
    for p in glob.glob(os.path.join(dest, "plc", "language", "*")):
        if p.lower().endswith(".pet"):
            keep.add(os.path.normcase(p))
    for p in glob.glob(os.path.join(dest, "plc", "program", "*")):
        if p.lower().endswith((".def", ".map", ".lst")):   # .map/.lst: compiler output with the symbol addresses
            keep.add(os.path.normcase(p))
    deleted = 0
    for root, dirs, files in os.walk(dest, topdown=False):
        for f in files:
            full = os.path.join(root, f)
            if os.path.normcase(full) not in keep:
                try:
                    os.remove(full)
                    deleted += 1
                except OSError:
                    pass
        if root != dest:
            try:
                os.rmdir(root)          # only succeeds for empty folders
            except OSError:
                pass
    return {"deleted": deleted}

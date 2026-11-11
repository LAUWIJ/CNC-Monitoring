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


def build(dest: str) -> dict:
    """Create dest/messages.json from the copied files. Returns a small summary."""
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
KEEP = ("messages.json", "filelist.txt", "marker_snapshot_v2.json")


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
        if p.lower().endswith(".def"):
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

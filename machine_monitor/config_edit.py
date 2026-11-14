"""Change [[machine]] blocks in config.toml in place, so every comment and line that isn't changed stays.

Used by the dashboard's Settings page. Only these edits are made:
  - set / add / remove plain `key = value` lines in a machine's own table
  - replace the machine's [[machine.wait]] rules
  - add a machine at the end of the file, or remove a machine's block
Everything else (other machines, [server], [defaults], sub-tables like [machine.nodes], comments) is left
exactly as it was. After every edit the whole file is parsed again and checked; if anything else changed or the
result is not valid TOML, nothing is written.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import tomllib
from dataclasses import dataclass
from typing import Optional

_KEY = re.compile(r'^\s*([A-Za-z0-9_\-]+)\s*=')
_HDR = re.compile(r'^\s*(\[\[?)\s*([A-Za-z0-9_.\-]+)\s*(\]\]?)\s*(#.*)?$')


@dataclass
class Item:
    kind: str            # header / kv / comment / blank / other
    first: int           # first line index
    last: int            # last line index (multi-line arrays span several lines)
    name: str = ""       # header name or key
    array: bool = False  # [[header]]


def _scan(code: str) -> tuple[int, int]:
    """(bracket depth change, index where a comment starts or -1) for one line, strings respected."""
    depth, q, i = 0, None, 0
    while i < len(code):
        c = code[i]
        if q:
            if c == "\\" and q == '"':
                i += 2
                continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "#":
            return depth, i
        i += 1
    return depth, -1


def parse_items(lines: list[str]) -> list[Item]:
    items, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            items.append(Item("blank", i, i))
        elif s.startswith("#"):
            items.append(Item("comment", i, i))
        elif s.startswith("["):
            m = _HDR.match(lines[i])
            items.append(Item("header", i, i, m.group(2) if m else s, bool(m and m.group(1) == "[[")))
        else:
            m = _KEY.match(lines[i])
            if m:
                depth = _scan(lines[i][m.end():])[0]
                j = i
                while depth > 0 and j + 1 < len(lines):
                    j += 1
                    depth += _scan(lines[j])[0]
                items.append(Item("kv", i, j, m.group(1)))
                i = j + 1
                continue
            items.append(Item("other", i, i))
        i += 1
    return items


@dataclass
class Block:
    first: int           # line of the [[machine]] header
    end: int             # one past the last line that belongs to this machine
    items: list          # items inside [first, end)


def machine_blocks(lines: list[str]) -> list[Block]:
    """The [[machine]] blocks in file order (the same order as tomllib's cfg["machine"] list)."""
    items = parse_items(lines)
    blocks = []
    for n, it in enumerate(items):
        if not (it.kind == "header" and it.array and it.name == "machine"):
            continue
        k = n + 1
        while k < len(items) and not (items[k].kind == "header" and not items[k].name.startswith("machine.")):
            k += 1
        own = items[n:k]
        # comment lines just above the next header, after the last blank line, belong to what follows
        # (e.g. a "# ---- Fanuc" separator). With nothing following (end of file), comments right after the
        # block without a blank line in between still belong to it.
        t = len(own)
        while t > 1 and own[t - 1].kind in ("comment", "blank"):
            t -= 1
        tail = own[t:]
        blanks = [x for x, it2 in enumerate(tail) if it2.kind == "blank"]
        if blanks:
            own = own[:t + blanks[-1] + 1]
        elif k < len(items):
            own = own[:t]
        blocks.append(Block(it.first, own[-1].last + 1, own))
    return blocks


# ------------------------------------------------------------------------------------------- values
def fmt(v) -> str:
    """A Python value as TOML. Strings with backslashes (regular expressions, Windows paths) become literal
    'strings' so they read the same as typed."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    s = str(v)
    if "\\" in s and "'" not in s and "\n" not in s:
        return f"'{s}'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _set_line(line: str, key: str, value) -> str:
    """Replace the value in `key = value   # comment`, keeping the spacing and the comment."""
    eol = line[len(line.rstrip("\r\n")):]
    body = line[:len(line) - len(eol)]
    m = _KEY.match(body)
    prefix = body[:m.end()]
    rest = body[m.end():]
    lead = rest[:len(rest) - len(rest.lstrip())] or " "
    _, c = _scan(rest)
    new = prefix + lead + fmt(value)
    if c >= 0:
        code = prefix + rest[:c]
        new += " " * max(1, len(code) - len(new)) + rest[c:]
    return new + eol


def _eol(lines):
    return "\r\n" if lines and lines[0].endswith("\r\n") else "\n"


# ------------------------------------------------------------------------------------------- edits
def update_machine(text: str, index: int, values: dict, remove: set | None = None,
                   wait: Optional[list] = None) -> str:
    """Machine number `index` (0-based, file order): set `values`, delete the keys in `remove`, and when
    `wait` is not None replace its [[machine.wait]] rules with that list of dicts."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += _eol(lines)
    eol = _eol(lines)
    blk = machine_blocks(lines)[index]
    sub = next((it for it in blk.items[1:] if it.kind == "header"), None)
    main_end = sub.first if sub else blk.end
    kvs = {it.name: it for it in blk.items if it.kind == "kv" and it.last < main_end}
    edits = []                                     # (first, last, order, new lines)
    for k, v in values.items():
        it = kvs.get(k)
        if it is None:
            continue
        new = [_set_line(lines[it.first], k, v)] if it.first == it.last else [f"{k} = {fmt(v)}{eol}"]
        edits.append((it.first, it.last, 0, new))
    for k in remove or ():
        if k in kvs and k not in values:
            it = kvs[k]
            edits.append((it.first, it.last, 0, []))
    add = [f"{k} = {fmt(v)}{eol}" for k, v in values.items() if k not in kvs]
    if add:
        last_kv = max((it.last for it in kvs.values()), default=blk.first)
        edits.append((last_kv + 1, last_kv, 0, add))  # insert after the last key of the machine's own table
    if wait is not None:
        regions = []
        items = blk.items
        for n, it in enumerate(items):
            if it.kind == "header" and it.array and it.name == "machine.wait":
                k = n + 1
                while k < len(items) and items[k].kind != "header":
                    k += 1
                body = [x for x in items[n + 1:k] if x.kind == "kv"]
                regions.append((it.first, (body[-1].last if body else it.last)))
        new = []
        for r in wait:
            new += [eol, f"[[machine.wait]]{eol}"] + [f"{k} = {fmt(v)}{eol}" for k, v in r.items()]
        if regions:
            for a, b in regions[1:]:
                edits.append((a, b, 0, []))
            a, b = regions[0]
            # the blank line in front of the first rule is part of what is written again
            if new and a > 0 and not lines[a - 1].strip():
                a -= 1
            edits.append((a, b, 0, new))
        elif new:
            # at the end of the machine's block (after any commented examples), so a commented key that
            # belongs to the machine's own table is never pulled into a wait table
            last = max(it.last for it in blk.items if it.kind != "blank")
            edits.append((last + 1, last, 1, new))
    # bottom-up so earlier line numbers stay valid; at the same spot the wait rules go in first, so the new
    # keys end up above them
    for a, b, _, new in sorted(edits, key=lambda e: (e[0], e[1], e[2]), reverse=True):
        lines[a:b + 1] = new
    return "".join(lines)


def add_machine(text: str, values: dict, wait: Optional[list] = None) -> str:
    lines = text.splitlines(keepends=True)
    eol = _eol(lines)
    out = text
    if out and not out.endswith(("\n", "\r")):
        out += eol
    if out.strip() and not out.endswith(eol * 2):
        out += eol
    out += f"[[machine]]{eol}" + "".join(f"{k} = {fmt(v)}{eol}" for k, v in values.items())
    for r in wait or []:
        out += eol + f"[[machine.wait]]{eol}" + "".join(f"{k} = {fmt(v)}{eol}" for k, v in r.items())
    return out


def remove_machine(text: str, index: int) -> str:
    lines = text.splitlines(keepends=True)
    blk = machine_blocks(lines)[index]
    del lines[blk.first:blk.end]
    return "".join(lines)


# ------------------------------------------------------------------------------------------- checked save
class ConfigError(ValueError):
    pass


def _without(cfg: dict, mid: Optional[str]) -> dict:
    c = dict(cfg)
    c["machine"] = [m for m in cfg.get("machine", []) if m.get("id") != mid]
    return c


def checked(old_text: str, new_text: str, mid: str, expect: Optional[dict]) -> dict:
    """Parse the new text and make sure only machine `mid` changed, to exactly `expect` (None = removed)."""
    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"the change would make config.toml invalid ({e}) - nothing was saved")
    old = tomllib.loads(old_text)
    if _without(old, mid) != _without(new, mid):
        raise ConfigError("the change would also touch other settings - nothing was saved")
    got = [m for m in new.get("machine", []) if m.get("id") == mid]
    if expect is None:
        if got:
            raise ConfigError("the machine could not be removed - nothing was saved")
    elif len(got) != 1 or got[0] != expect:
        raise ConfigError("the change did not come out as expected - nothing was saved")
    return new


def write(path: str, text: str, backup_dir: Optional[str] = None, keep: int = 20) -> None:
    """Write atomically; the previous version goes to backup_dir (the newest `keep` are kept)."""
    if backup_dir and os.path.exists(path):
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(path, os.path.join(backup_dir, time.strftime("config-%Y%m%d-%H%M%S.toml")))
        olds = sorted(f for f in os.listdir(backup_dir) if f.startswith("config-") and f.endswith(".toml"))
        for f in olds[:-keep]:
            try:
                os.remove(os.path.join(backup_dir, f))
            except OSError:
                pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

"""Tool life from the control (FANUC tool life management, verified on a Puma 2600LY / 0i-D, 2026-11-12).

A tool group (called as T0199 = group 1 on a lathe) has a life per tool (in cycles or minutes), a counter for
the tool now in use, and sister tools. Per tool a status: 1 = available, 2 = life expired, 3 = skipped
(0 is also seen as 'not used yet'). The control moves to the next available tool when the counter reaches
the life and starts counting again.

Left in the group = (life - counter) on the tool in use + life x every other available tool.
Example (2600LY, group 1): life 9, counter 1, tools T101 expired, T1313 in use, T1212 skipped, T2424
available -> (9 - 1) + 9 = 17 cycles.

HEIDENHAIN has no groups, but a life per tool in the tool table (TIME1/TIME2 and CUR_TIME, minutes). The driver
reports each such tool as a one-tool 'group' with number -T (negative = single HEIDENHAIN tool), so the same
storage and display work. Minutes are turned into parts with the tool's median running time per part.
"""
from __future__ import annotations

EXPIRED, SKIPPED = 2, 3


def group_left(g: dict):
    """g: {"life", "count", "count_type" ('cycles'|'minutes'), "nfree", "use_tool" (1-based position),
    "tools": [{"num", "status"}]}. Returns (amount, unit) or (None, None) when nothing is known."""
    life, count = float(g.get("life") or 0), float(g.get("count") or 0)
    if life <= 0:
        return None, None
    unit = "parts" if g.get("count_type") == "cycles" else "min"
    tools = g.get("tools") or []
    if tools:
        use = int(g.get("use_tool") or 0)
        left = 0
        for i, t in enumerate(tools, 1):
            if t.get("status") in (EXPIRED, SKIPPED):
                continue
            left += max(life - count, 0) if i == use else life
        left = int(left)
        if use == 0:                     # group not started yet: nothing counted on any tool
            left = int(sum(life for t in tools if t.get("status") not in (EXPIRED, SKIPPED)))
        return left, unit
    nfree = int(g.get("nfree") or 0)     # tool list not readable: count from the free tools
    if nfree <= 0:
        return 0, unit
    return int(max(life - count, 0) + (nfree - 1) * life), unit

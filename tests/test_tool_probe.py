import ctypes as C

from machine_monitor import tool_probe as T


def test_fanuc_structs_match_fwlib32_h():
    # sizes from FANUC's fwlib32.h (long = 32 bit)
    assert C.sizeof(T.ODBTLINFO) == 16
    assert C.sizeof(T.ODBUSEGRP) == 24
    assert C.sizeof(T.IODBTLGRP) == 32
    assert C.sizeof(T.IODBTLTOOL) == 16
    assert C.sizeof(T.ODBTLIFE2) == 8


def test_parse_heidenhain_tool_table():
    widths = [("T", 6), ("NAME", 18), ("L", 12), ("R", 9), ("TIME1", 6), ("TIME2", 6), ("CUR_TIME", 9),
              ("TL", 3), ("RT", 4)]

    def line(vals):
        return "".join(str(v).ljust(w) for v, (_, w) in zip(vals, widths))
    text = "\n".join(["BEGIN TOOL     .T MM", line([c for c, _ in widths]),
                      line([0, "NULLWERKZEUG", "+0", "+0", 0, 0, 0, "", ""]),
                      line([12, "DRILL_6.8", "+112.5", "+3.4", 30, 0, 12.5, "", 13]),
                      line([13, "DRILL_6.8_SISTER", "+112.4", "+3.4", 30, 0, 0, "", ""]), "[END]"])
    cols, rows = T.parse_tnc_table(text)
    assert cols[:5] == ["T", "NAME", "L", "R", "TIME1"]
    assert len(rows) == 3
    assert rows[1]["NAME"] == "DRILL_6.8" and rows[1]["TIME1"] == "30" and rows[1]["CUR_TIME"] == "12.5"
    assert rows[1]["RT"] == "13"


class _Fn:
    """Stands in for a FOCAS function: accepts argtypes/restype like ctypes, calls a Python function."""
    def __init__(self, f):
        self.f = f

    def __call__(self, *a):
        return self.f(*a)


def _obj(x):
    return x._obj if hasattr(x, "_obj") else x


def test_fanuc_probe_reads_groups_and_tools(monkeypatch):
    calls = []

    def rdmacro(h, no, ln, p):
        _obj(p).mcr_val, _obj(p).dec_val = 199, 0
        return 0

    def rdtlinfo(h, p):
        o = _obj(p); o.max_group, o.max_tool, o.max_minute, o.max_cycle = 128, 4, 9999, 9999
        return 0

    def rdngrp(h, p):
        _obj(p).data = 2
        return 0

    def rdtlusegrp(h, p):
        _obj(p).use = 1
        return 0

    def rdtlgrp(h, g, num, p):
        calls.append(("grp", g, _obj(num).value))
        o = _obj(p); o.ntool, o.nfree, o.life, o.count, o.use_tool, o.life_rest, o.count_type = 2, 1, 500, 120, 101, 380, 0
        return 0

    def rdtltool(h, g, start, num, buf):
        calls.append(("tools", g, _obj(num).value, len(buf)))
        buf[0].tool_num, buf[0].tool_inf = 101, 1
        buf[1].tool_num, buf[1].tool_inf = 111, 0
        _obj(num).value = 2
        return 0

    class Lib:
        cnc_rdmacro, cnc_rdtlinfo, cnc_rdngrp = _Fn(rdmacro), _Fn(rdtlinfo), _Fn(rdngrp)
        cnc_rdtlusegrp, cnc_rdtlgrp, cnc_rdtltool = _Fn(rdtlusegrp), _Fn(rdtlgrp), _Fn(rdtltool)

    class Drv:
        def __init__(self, cfg):
            self.lib, self.h = Lib(), C.c_ushort(1)

        def connect(self):
            pass

        def close(self):
            calls.append("closed")

    from machine_monitor.drivers import fanuc_focas
    monkeypatch.setattr(fanuc_focas, "FanucFocasDriver", Drv)
    lines = []
    T.probe({"control": "fanuc", "host": "x"}, out=lines.append)
    text = "\n".join(lines)
    assert "current T code (#4120): 199" in text
    assert "number of tool groups set up: 2" in text and "group in use: 1" in text
    assert "T101:0/0:1" in text and "T111:0/0:0" in text and "cycles" in text
    assert ("grp", 1, 1) in calls and ("grp", 2, 1) in calls       # one group at a time, buffer of one
    assert ("tools", 1, 2, 2) in calls                              # asks exactly as many as the buffer holds
    assert calls[-1] == "closed"


def test_fanuc_probe_stops_without_tool_life_option(monkeypatch):
    class Lib:
        cnc_rdtlinfo = _Fn(lambda h, p: 6)
        cnc_rdngrp = _Fn(lambda h, p: (_ for _ in ()).throw(AssertionError("must not be called")))

    class Drv:
        def __init__(self, cfg):
            self.lib, self.h = Lib(), C.c_ushort(1)

        def connect(self):
            pass

        def close(self):
            pass

    from machine_monitor.drivers import fanuc_focas
    monkeypatch.setattr(fanuc_focas, "FanucFocasDriver", Drv)
    lines = []
    T.probe({"control": "fanuc", "host": "x"}, out=lines.append)
    assert any("not available" in ln for ln in lines)

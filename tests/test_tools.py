import os

from machine_monitor.engine import Cycle, CycleEngine, EngineConfig
from machine_monitor.model import ExecState as E, Mode as M, Sample
from machine_monitor.store import Store
from machine_monitor.tool_life import group_left


def _group(**kw):
    g = {"grp": 1, "count_type": "cycles", "life": 9, "count": 0, "ntool": 4, "nfree": 4, "use_tool": 1,
         "tools": [{"num": 101, "status": 1}, {"num": 1313, "status": 1}, {"num": 1212, "status": 1},
                   {"num": 2424, "status": 1}]}
    g.update(kw)
    return g


def test_group_left_matches_the_2600ly_readings():
    # first reading: T101 in use, counter 8, all four available -> 1 + 3 x 9
    assert group_left(_group(count=8)) == (28, "parts")
    # later: T101 expired, T1313 in use (position 2) with counter 1, T1212 skipped -> 8 + 9
    g = _group(count=1, use_tool=2, nfree=2, tools=[{"num": 101, "status": 2}, {"num": 1313, "status": 1},
                                                     {"num": 1212, "status": 3}, {"num": 2424, "status": 1}])
    assert group_left(g) == (17, "parts")


def test_group_left_without_tool_list_uses_free_count():
    assert group_left(_group(count=1, nfree=2, tools=[])) == (17, "parts")
    assert group_left(_group(life=0)) == (None, None)
    assert group_left(_group(count_type="minutes", count=10, life=60, nfree=1, tools=[])) == (50, "min")


def _drive(e, steps, t=0.0):
    out = []
    for dur, kw in steps:
        for _ in range(int(dur)):
            out += [ev for ev in e.update(Sample(ts=t, mode=M.AUTO, **kw)) if isinstance(ev, Cycle)]
            t += 1.0
    return out, t


def test_engine_adds_running_time_per_tool():
    e = CycleEngine("m", EngineConfig())
    run = dict(exec_state=E.RUNNING, main_program="O1234", current_program="O1234")
    cycles, t = _drive(e, [
        (5, dict(exec_state=E.IDLE)),
        (30, dict(run, tool="101", tool_group=1)),
        (20, dict(run, tool="505")),
        (10, dict(exec_state=E.PAUSED, main_program="O1234", current_program="O1234", tool="505")),
        (15, dict(run, tool="202", tool_group=2, tool_name=None)),
        (10, dict(exec_state=E.IDLE)),
    ])
    assert len(cycles) == 1
    c = cycles[0]
    assert round(c.tool_times["101"]) == 30 and round(c.tool_times["505"]) == 20 and round(c.tool_times["202"]) == 15
    assert c.tool_info["101"]["group"] == 1 and c.tool_info["505"]["group"] is None


def test_store_tool_life_only_on_change_and_program_tools(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    assert st.add_tool_life("m", 1.0, _group(count=8))
    assert not st.add_tool_life("m", 2.0, _group(count=8))           # unchanged -> not stored again
    assert st.add_tool_life("m", 3.0, _group(count=0, use_tool=2, tools=[{"num": 101, "status": 2}] +
                                              _group()["tools"][1:]))
    now = st.tool_life_now("m")[1]
    assert now["use_tool"] == 2 and now["left_amount"] == 27 and now["left_unit"] == "parts"

    for i, (a, b) in enumerate([(30, 20), (32, 18), (31, 0)]):
        c = Cycle("m", "O1234", None, "control", 100 + i * 100, end_ts=150 + i * 100, result="COMPLETED",
                  tool_times={"101": a, **({"505": b} if b else {})},
                  tool_info={"101": {"name": None, "group": 1}, "505": {"name": None, "group": None}})
        st.add_cycle(c)
    tools = st.program_tools("m", "O1234")
    assert [t["tool"] for t in tools] == ["G1", "505"]
    assert tools[0]["median_s"] == 31 and tools[0]["group"] == 1
    assert tools[1]["median_s"] == 18 and tools[1]["used_in"] == 2


def test_program_tools_combines_sister_tools_of_a_group(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    for i, (tool, t) in enumerate([("101", 30), ("101", 31), ("1313", 29)]):
        st.add_cycle(Cycle("m", "O1", None, "control", 100 + i * 100, end_ts=150 + i * 100, result="COMPLETED",
                           tool_times={tool: t, "505": 10}, tool_info={tool: {"name": None, "group": 1},
                                                                     "505": {"name": None, "group": None}}))
    tools = st.program_tools("m", "O1")
    assert [t["tool"] for t in tools] == ["G1", "505"]
    assert tools[0]["median_s"] == 30 and tools[0]["used_in"] == 3 and set(tools[0]["tools_seen"]) == {"101", "1313"}


def test_fanuc_m01_from_pmc_f9():
    """DM00 / DM01 = F9.7 / F9.6: a machine waiting at M01 while the control still says STRT shows as paused."""
    import ctypes as C
    from machine_monitor.drivers.fanuc_focas import FanucFocasDriver

    class L:
        val = 0x40

        def pmc_rdpmcrng(self, h, adr, typ, s, e, n, buf):
            assert (adr, typ, s, e, n) == (1, 0, 9, 9, 9)
            C.cast(buf, C.POINTER(C.c_ubyte))[8] = self.val
            return 0

    d = FanucFocasDriver.__new__(FanucFocasDriver)
    d.cfg, d.read_m0, d._m0_fail = {"id": "t"}, True, 0
    lib = L()
    assert d._read_m_stop(lib, 1) == "M01"
    lib.val = 0x80
    assert d._read_m_stop(lib, 1) == "M00"
    lib.val = 0x10                                  # DM30 only
    assert d._read_m_stop(lib, 1) is None

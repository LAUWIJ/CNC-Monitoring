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


def test_pitch_is_median_of_start_to_start():
    from machine_monitor.store import Store
    # parts of 10 min, robot loading 2 min between them -> 12 min start to start; one lunch break of 45 min
    starts = [10000 - i * 720 for i in range(8)]
    starts.insert(3, starts[2] - 720)                   # keep it a run
    starts = sorted(set(starts), reverse=True)
    starts = starts[:4] + [s - 2700 for s in starts[4:]]   # a 45 min break between the 4th and 5th part
    assert Store._pitch(starts, 600) == 720               # the median: one break does not count
    assert Store._pitch(starts[:3], 600) is None          # too few parts
    long = [100000 - i * 9000 for i in range(5)]          # 2.5 h cycles: no limit on the gap length
    assert Store._pitch(long, 8400) == 9000


def test_tool_life_used_up_while_standing_still_is_waiting_for_operator(tmp_path):
    from machine_monitor.collector import MachineWorker
    from machine_monitor.model import ExecState, Mode, Sample
    from machine_monitor.store import Store
    st = Store(str(tmp_path / "m.db"))
    w = MachineWorker({"id": "m6", "name": "PUMA 2600MB", "control": "simulator"}, {}, st)
    left = {"tool_left": 0, "tool_left_unit": "parts", "tool_left_expired": True}
    st.tool_status = lambda mid, prog, live: left
    s = Sample(ts=1000.0, exec_state=ExecState.IDLE, mode=Mode.AUTO, main_program="O1005")
    w.tool_life_wait(s)
    assert (s.exec_state, s.wait_reason) == (ExecState.WAITING, "operator")
    assert "tool life" in s.extra["waiting_for"]
    m = Sample(ts=1001.0, exec_state=ExecState.IDLE, mode=Mode.MANUAL)      # operator at the machine
    w.tool_life_wait(m)
    assert m.exec_state == ExecState.IDLE
    left.update(tool_left=7, tool_left_expired=False)                        # life reset / tools changed
    s2 = Sample(ts=1100.0, exec_state=ExecState.IDLE, mode=Mode.AUTO)
    w.tool_life_wait(s2)
    assert s2.exec_state == ExecState.IDLE


def test_heidenhain_life_read_at_part_start_is_stored_right_away(tmp_path):
    """Tool swapped (CUR_TIME back to 0) while the life was used up: the tool table read at the start of the next
    part is stored as the part starts, so the card no longer says 'used up' for that whole first part."""
    from machine_monitor.collector import MachineWorker
    from machine_monitor.drivers.heidenhain_lsv2 import HeidenhainLsv2Driver, _life_group
    from machine_monitor.model import ExecState, Mode, Sample
    st = Store(str(tmp_path / "m.db"))
    w = MachineWorker({"id": "m2", "name": "DVF Erowa", "control": "simulator"}, {}, st)
    st.add_tool_life("m2", 100.0, _life_group(5, 60, 55, 61.0, False))          # used up before the swap
    assert st.tool_life_now("m2")[-5]["tools"][0]["status"] == 2

    d = HeidenhainLsv2Driver.__new__(HeidenhainLsv2Driver)                      # no connection needed
    d._tool_life = {"5": {"time1": 60, "time2": 55, "cur_time": 0.0, "locked": False}}
    d._life_fresh, d._no_life = True, set()
    w.driver = d
    w.handle(Sample(ts=200.0, online=True, exec_state=ExecState.IDLE, mode=Mode.AUTO, main_program="P.h"))
    # the read flagged as fresh is handed over once, at the next sample
    r = st.tool_life_now("m2")[-5]
    assert r["tools"][0]["status"] == 1 and r["count"] == 0.0
    assert d.fresh_tool_life() is None

    d._tool_life["5"]["cur_time"] = 2.0                                          # read as a part starts
    d._life_fresh = True
    w.handle(Sample(ts=300.0, online=True, exec_state=ExecState.RUNNING, mode=Mode.AUTO, main_program="P.h"))
    r = st.tool_life_now("m2")[-5]
    assert r["count"] == 2.0 and w.engine.cycle is not None and r["ts"] == w.engine.cycle.start_ts

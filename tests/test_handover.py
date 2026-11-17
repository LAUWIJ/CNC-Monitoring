"""Open part handed over across a restart of the monitor (deploy, reboot)."""
from machine_monitor.engine import CycleEngine, EngineConfig
from machine_monitor.model import ExecState as E, Mode as M, Sample


def S(ts, ex=E.RUNNING, prog="P1.h", tool="5", part=None, online=True):
    return Sample(ts=ts, online=online, exec_state=ex, mode=M.AUTO, main_program=prog, tool=tool, part_count=part)


def running_engine(part=None):
    e = CycleEngine("m2", EngineConfig())
    e.update(S(999.0, ex=E.IDLE, part=part))             # seen standing still first: a real start
    for t in range(1000, 1061, 1):                       # part started at 1000, running 60 s
        e.update(S(float(t), part=part))
    return e


def test_part_continues_after_a_short_restart():
    old = running_engine()
    h = old.export_open()
    new = CycleEngine("m2", EngineConfig())
    new.take_over(h, max_wall_s=600)
    ev = new.update(S(1090.0))                            # back 30 s later, same program running
    assert ev == [] or all(not hasattr(x, "result") for x in ev)
    c = new.cycle
    assert c.start_ts == 1000.0 and not c.partial
    assert abs(c.run_s - 89.0) < 1.5                      # 59 s before + 30 s gap counted as running
    assert abs(c.tool_times["5"] - 89.0) < 1.5
    for t in range(1091, 1100):
        new.update(S(float(t)))
    closed = [x for x in new.update(S(1110.0, ex=E.IDLE)) + new.update(S(1116.0, ex=E.IDLE)) if hasattr(x, "result")]
    assert closed and closed[0].result == "COMPLETED" and closed[0].start_ts == 1000.0


def test_next_part_could_have_started_gives_partial():
    h = running_engine().export_open()
    new = CycleEngine("m2", EngineConfig())
    new.take_over(h, max_wall_s=80)                       # usual start-to-start 89 s: 90 s after the start
    ev = new.update(S(1090.0))                            # a new part may already be running
    done = [x for x in ev if hasattr(x, "result")]
    assert done[0].result == "PARTIAL" and done[0].parts == 1 and done[0].end_reason == "MONITOR_RESTART"
    assert new.cycle.partial                              # the running one: start not seen


def test_counter_rose_during_the_gap():
    h = running_engine(part=7).export_open()
    new = CycleEngine("m2", EngineConfig())
    new.take_over(h, max_wall_s=600)
    done = [x for x in new.update(S(1090.0, part=8)) if hasattr(x, "result")]
    assert done[0].result == "PARTIAL" and done[0].parts == 1


def test_reset_during_the_gap_with_a_counter_is_no_part():
    h = running_engine(part=7).export_open()
    new = CycleEngine("m2", EngineConfig())
    new.take_over(h)
    done = [x for x in new.update(S(1090.0, ex=E.IDLE, part=7)) if hasattr(x, "result")]
    assert done[0].parts == 0


def test_too_long_ago_or_other_program():
    h = running_engine().export_open()
    late = CycleEngine("m2", EngineConfig())
    late.take_over(h)
    late.update(S(1061.0 + 400))
    assert late.cycle.partial and late.cycle.start_ts == 1461.0
    other = CycleEngine("m2", EngineConfig())
    other.take_over(h)
    done = [x for x in other.update(S(1090.0, prog="P2.h")) if hasattr(x, "result")]
    assert done[0].program == "P1.h" and done[0].result == "PARTIAL"
    assert other.cycle.program == "P2.h" and other.cycle.partial


def test_save_and_load_file(tmp_path):
    from machine_monitor.collector import load_handover, save_handover

    class W:
        id = "m2"
        engine = running_engine()
    assert save_handover([W()], str(tmp_path)) == 1
    d = load_handover(str(tmp_path))
    assert d["m2"]["cycle"]["start_ts"] == 1000.0
    assert load_handover(str(tmp_path)) == {}             # used once

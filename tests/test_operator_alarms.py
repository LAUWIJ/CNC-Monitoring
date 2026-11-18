"""A site-listed alarm turns a machine that still reports "running" into waiting for the operator."""
from machine_monitor.collector import MachineWorker
from machine_monitor.model import ExecState as E, Mode as M, Sample
from machine_monitor.store import Store


def S(ts, ex=E.RUNNING):
    return Sample(ts=ts, online=True, exec_state=ex, mode=M.AUTO, main_program="AMF.h", tool="12")


def test_plc_message_makes_running_machine_wait(tmp_path):
    st = Store(str(tmp_path / "m.db"))
    w = MachineWorker({"id": "m2", "name": "Erowa", "control": "simulator",
                       "operator_alarms": ["correct tool in spindle"]}, {}, st)
    w.handle(S(1000.0, E.IDLE))
    for t in range(1001, 1061):
        w.handle(S(float(t)))
    w.track_alarms([{"code": "PLC 215", "text": "Correct tool in spindle ?"}], 1060.0)
    s = S(1061.0)
    w.handle(s)
    assert s.exec_state == E.WAITING and s.extra["waiting_for"] == "PLC 215 Correct tool in spindle ?"
    for t in range(1062, 1121):
        w.handle(S(float(t)))
    c = w.engine.cycle
    assert c is not None and abs(c.wait_s - 59) < 1.5 and abs(c.run_s - 60) < 1.5      # part open, time = waiting
    w.track_alarms([], 1121.0)                             # answered: running again
    s = S(1122.0)
    w.handle(s)
    assert s.exec_state == E.RUNNING


def test_other_alarms_change_nothing(tmp_path):
    w = MachineWorker({"id": "m2", "name": "Erowa", "control": "simulator", "operator_alarms": ["PLC 215"]}, {},
                      Store(str(tmp_path / "m.db")))
    w.track_alarms([{"code": "PLC 2150", "text": "x"}, {"code": "PLC 21", "text": "y"}], 1.0)
    s = S(2.0)
    w.handle(s)
    assert s.exec_state == E.RUNNING          # whole words: "PLC 215" is not "PLC 2150"
    w2 = MachineWorker({"id": "m3", "name": "x", "control": "simulator", "operator_alarms": ["PLC 215 "]}, {},
                       Store(str(tmp_path / "n.db")))
    w2.track_alarms([{"code": "PLC 21", "text": "Spindle"}], 1.0)
    s = S(2.0)
    w2.handle(s)
    assert s.exec_state == E.RUNNING

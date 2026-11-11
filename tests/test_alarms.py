"""Alarm reading: display only, never touches states or cycles.   python -m unittest discover tests"""
import ctypes as C
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from machine_monitor.drivers import fanuc_focas as F  # noqa: E402
from machine_monitor.drivers.heidenhain_lsv2 import HeidenhainLsv2Driver  # noqa: E402
from machine_monitor.drivers.siemens_opcua import _AlarmHandler  # noqa: E402
from machine_monitor.store import Store  # noqa: E402


class FakeFocas:
    """Mimics the FOCAS calls: fills the caller's buffers like the real DLL does."""

    def __init__(self, alarms, msgs=(), alm2=True, opmsg_ret=0):
        self.alarms, self.msgs, self.opmsg_ret = alarms, msgs, opmsg_ret
        if alm2:
            self.cnc_rdalmmsg2 = self._alm(F.ODBALMMSG2)
        else:
            self.cnc_rdalmmsg2 = lambda *a: 1          # EW_FUNC on an old series
            self.cnc_rdalmmsg = self._alm(F.ODBALMMSG)

    def _alm(self, struct):
        def fn(h, typ, num, buf):
            arr = C.cast(buf, C.POINTER(struct))
            n = num._obj if hasattr(num, "_obj") else num
            for i, (t, no, axis, text) in enumerate(self.alarms):
                arr[i].type, arr[i].alm_no, arr[i].axis = t, no, axis
                arr[i].alm_msg = text.encode()
                arr[i].msg_len = len(text)
            n.value = len(self.alarms)
            return 0
        return fn

    def cnc_rdopmsg(self, h, typ, length, buf):
        if self.opmsg_ret:
            return self.opmsg_ret
        arr = C.cast(buf, C.POINTER(F.OPMSG))
        for i in range(5):
            arr[i].datano = -1
        for i, (no, text) in enumerate(self.msgs):
            arr[i].datano, arr[i].data, arr[i].char_num = no, text.encode(), len(text)
        return 0


def fanuc(lib):
    d = F.FanucFocasDriver({"host": "x"})
    d.lib, d.connected = lib, True
    return d


class FanucAlarmTests(unittest.TestCase):
    def test_alarm_and_operator_message(self):
        d = fanuc(FakeFocas([(6, 401, 1, "SERVO V-READY OFF"), (3, 11, 0, "FEED ZERO")],
                            [(2010, "DOOR OPEN")]))
        self.assertEqual(d.read_alarms(), [
            {"code": "SV0401", "text": "SERVO V-READY OFF (axis 1)"},
            {"code": "PS0011", "text": "FEED ZERO"},
            {"code": "MSG 2010", "text": "DOOR OPEN"}])

    def test_no_alarms_is_empty_list_not_none(self):
        self.assertEqual(fanuc(FakeFocas([])).read_alarms(), [])

    def test_old_series_falls_back_to_rdalmmsg(self):
        d = fanuc(FakeFocas([(15, 1001, 0, "LUBE LOW")], alm2=False))
        self.assertEqual(d.read_alarms(), [{"code": "EX1001", "text": "LUBE LOW"}])

    def test_all_types_empty_but_alarm_flag_asks_per_type(self):
        lib = FakeFocas([])
        real = lib._alm(F.ODBALMMSG2)

        def per_type(h, typ, num, buf):          # "all" gives nothing, type 6 (SV) gives the alarm
            if typ == 6:
                lib.alarms = [(6, 401, 0, "SERVO V-READY OFF")]
            else:
                lib.alarms = []
            return real(h, typ, num, buf)
        lib.cnc_rdalmmsg2 = per_type
        d = fanuc(lib)
        d._st_alarm, d._st_emg = True, True
        self.assertEqual(d.read_alarms(), [{"code": "EMG", "text": "Emergency stop"},
                                           {"code": "SV0401", "text": "SERVO V-READY OFF"}])
        self.assertIn("per type", d.last_alarm_raw)

    def test_operator_messages_unsupported_switches_off(self):
        d = fanuc(FakeFocas([], opmsg_ret=6))
        self.assertEqual(d.read_alarms(), [])
        self.assertFalse(d.read_opmsg)


class HeidenhainAlarmTests(unittest.TestCase):
    def test_objects_and_dicts(self):
        d = HeidenhainLsv2Driver({"host": "x"})
        d.con = types.SimpleNamespace(get_error_messages=lambda: [
            types.SimpleNamespace(e_number=1080, e_text="Spindle not ready", e_class=1),
            {"Number": 200, "Text": "Emergency stop", "Class": 5}])
        self.assertEqual(d.read_alarms(), [{"code": "1080", "text": "Spindle not ready"},
                                           {"code": "200", "text": "Emergency stop"}])

    def test_only_itnc530_is_asked(self):
        calls = []
        d = HeidenhainLsv2Driver({"host": "x"})
        d.con = types.SimpleNamespace(versions=types.SimpleNamespace(is_itnc=lambda: False),
                                      get_error_messages=lambda: calls.append(1) or [])
        self.assertIsNone(d.read_alarms())
        self.assertIsNone(d.read_alarms())
        self.assertEqual(calls, [])                         # TNC 640: never asked
        d = HeidenhainLsv2Driver({"host": "x"})
        d.con = types.SimpleNamespace(versions=types.SimpleNamespace(is_itnc=lambda: True),
                                      get_error_messages=lambda: [{"Number": 7, "Text": "Door open"}])
        self.assertEqual(d.read_alarms(), [{"code": "7", "text": "Door open"}])

    def test_old_pylsv2_without_function(self):
        d = HeidenhainLsv2Driver({"host": "x"})
        d.con = types.SimpleNamespace()
        self.assertIsNone(d.read_alarms())


class HeidenhainProgramEndTests(unittest.TestCase):
    def test_started_then_idle_is_a_normal_end(self):
        seq = iter(["STARTED", "IDLE", "IDLE", "STARTED", "CANCELLED"])
        d = HeidenhainLsv2Driver({"host": "x"})
        d.v1 = True
        d.con = types.SimpleNamespace(execution_state=lambda: "AUTOMATIC", program_status=lambda: next(seq),
                                      program_stack=lambda: None)
        got = [(x.program_finished, x.program_aborted) for x in (d.read() for _ in range(5))]
        self.assertEqual(got, [(False, False), (True, False), (False, False), (False, False), (False, True)])


class HeidenhainPlcMessageTests(unittest.TestCase):
    PET = ("BEGIN PLCERROR.PET MM\n"
           "NR      ERROR                            MARKER                                    RESET\n"
           "0       #000 Circuit breaker             PN_error_circuit_breaker                  0\n"
           "2       #002 Machine guard is open !     PN_error_guard                            0\n"
           "3       #003                                                                       0\n"
           "[END]\n")
    DEF = ("#type M\n   PN_error                                 4800[500]\n"
           "/c PN_error_circuit_breaker                 &PN_error +  000 ;;0000\n"
           "/c PN_error_guard                           &PN_error +  002\n"
           "#type M\n   A_auto                                   [150]\n")

    def test_build_list_and_read_markers(self):
        from machine_monitor import plc_messages
        from machine_monitor.drivers import heidenhain_lsv2 as H
        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "data", "plc_m1")
            os.makedirs(os.path.join(dest, "plc", "language"))
            os.makedirs(os.path.join(dest, "plc", "program"))
            for rel, text in ((("plc", "language", "PlcError.pet"), self.PET), (("plc", "program", "PlcError.def"), self.DEF)):
                with open(os.path.join(dest, *rel), "w") as f:
                    f.write(text)
            self.assertEqual(plc_messages.build(dest), {"messages": 2, "without_address": 0})
            junk = os.path.join(dest, "plc", "program", "Spindle.src")
            with open(junk, "w") as f:
                f.write("x")
            plc_messages.prune(dest)
            self.assertFalse(os.path.exists(junk))
            self.assertTrue(os.path.exists(os.path.join(dest, "plc", "program", "PlcError.def")))
            self.assertTrue(os.path.exists(os.path.join(dest, "messages.json")))
            old = H.PROJECT_DIR
            H.PROJECT_DIR = td
            try:
                asked = []

                def read_plc_memory(first, mt, count):
                    asked.append((first, count))
                    return [False, False, True]          # M4802 set -> "Machine guard is open !"
                d = H.HeidenhainLsv2Driver({"host": "x", "id": "m1"})
                d.con = types.SimpleNamespace(versions=types.SimpleNamespace(is_itnc=lambda: False),
                                              read_plc_memory=read_plc_memory)
                self.assertEqual(d.read_alarms(), [{"code": "PLC 2", "text": "Machine guard is open !"}])
                self.assertEqual(asked, [(4800, 3)])
                # big ranges are read in single-telegram blocks
                asked.clear()
                H._read_markers(types.SimpleNamespace(read_plc_memory=lambda a, t, n: asked.append((a, n)) or [0] * n),
                                4800, 399)
                self.assertEqual(asked, [(4800, 128), (4928, 128), (5056, 128), (5184, 15)])
            finally:
                H.PROJECT_DIR = old


class SiemensAlarmTests(unittest.TestCase):
    def test_condition_active_then_cleared(self):
        h = _AlarmHandler()
        lt = lambda t: types.SimpleNamespace(Text=t)  # noqa: E731
        ev = types.SimpleNamespace(NodeId="c1", Message=lt("700012 | [M,Door] Door not closed"),
                                   ConditionName=lt("SystemCondition"),
                                   SourceName=lt("PLC"), Retain=True, **{"ActiveState/Id": True})
        h.event_notification(ev)
        self.assertEqual(list(h.active.values()), [{"code": "700012", "text": "[M,Door] Door not closed"}])
        h.event_notification(types.SimpleNamespace(NodeId="c1", Message=lt("700012 | [M,Door] Door not closed"),
                                                   Retain=False, **{"ActiveState/Id": False}))
        self.assertEqual(h.active, {})
        h.event_notification(types.SimpleNamespace(Message=None))      # refresh start/end: ignored
        self.assertEqual(h.active, {})


class CollectorAlarmTests(unittest.TestCase):
    def test_open_close_and_summary(self):
        from machine_monitor.collector import MachineWorker
        with tempfile.TemporaryDirectory() as td:
            st = Store(os.path.join(td, "t.db"))
            w = MachineWorker({"id": "m1", "control": "simulator", "name": "M1"}, {}, st)
            a = {"code": "SV0401", "text": "SERVO"}
            w.track_alarms([a], 100)
            w.track_alarms([a], 105)
            w.track_alarms([], 130)                     # cleared
            w.track_alarms([a], 200)
            w.track_alarms(None, 210)                   # could not read: keep it open
            r = st.alarms("m1", 0, 1e12)
            self.assertEqual(len(r["active"]), 1)
            self.assertEqual(r["summary"][0]["count"], 2)
            w.close_alarms(250)                         # machine went offline
            r = st.alarms("m1", 0, 1e12)
            self.assertEqual(r["active"], [])
            self.assertAlmostEqual(r["summary"][0]["total_s"], 30 + 50)
            self.assertEqual(st.alarms("m1", 0, 1e12)["total"], 2)
            st.db.close()


if __name__ == "__main__":
    unittest.main()

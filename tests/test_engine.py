"""Scenario tests for the cycle engine.   python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from machine_monitor.engine import Cycle, CycleEngine, EngineConfig, StateChange  # noqa: E402
from machine_monitor.model import ExecState as E, Mode as M, Sample  # noqa: E402

R, P, I = E.RUNNING, E.PAUSED, E.IDLE


def run(engine, script, t=0.0):
    """script: list of (seconds, dict(sample fields)). One sample per second."""
    cycles = []
    for dur, kw in script:
        for _ in range(int(dur)):
            kw2 = {"mode": M.AUTO, **kw}
            for ev in engine.update(Sample(ts=t, **kw2)):
                if isinstance(ev, Cycle):
                    cycles.append(ev)
            t += 1
    return cycles, t


class EngineTests(unittest.TestCase):
    def test_fanuc_macro_does_not_split_cycle(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (10, dict(exec_state=I, main_program="O1234", part_count=5)),
            (30, dict(exec_state=R, main_program="O1234", current_program="O1234", part_count=5)),
            (8, dict(exec_state=R, main_program="O1234", current_program="O9001", part_count=5)),   # M6 macro
            (30, dict(exec_state=R, main_program="O1234", current_program="O1234", part_count=5)),
            (1, dict(exec_state=R, main_program="O1234", current_program="O1234", part_count=6)),   # M30 counts
            (10, dict(exec_state=I, main_program="O1234", part_count=6)),
        ])
        self.assertEqual(len(cyc), 1)
        c = cyc[0]
        self.assertEqual((c.program, c.result, c.end_reason, c.parts), ("O1234", "COMPLETED", "PART_COUNTER", 1))
        self.assertIn("O9001", c.subprograms)
        self.assertAlmostEqual(c.duration_s, 68, delta=1)

    def test_feed_hold_and_m0_stay_in_cycle(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (20, dict(exec_state=R, current_program="P1", program_end_count=10)),
            (60, dict(exec_state=P, current_program="P1", program_end_count=10)),   # M0, operator measures
            (19, dict(exec_state=R, current_program="P1", program_end_count=10)),
            (1, dict(exec_state=R, current_program="P1", program_end_count=11)),    # M30
            (10, dict(exec_state=I, program_end_count=11)),
        ])
        self.assertEqual(len(cyc), 1)
        self.assertEqual(cyc[0].end_reason, "PROGRAM_END")
        self.assertAlmostEqual(cyc[0].pause_s, 60, delta=1)
        self.assertAlmostEqual(cyc[0].run_s, 40, delta=1)

    def test_generic_latches_first_non_sub_program(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (2, dict(exec_state=I)),
            (5, dict(exec_state=R, current_program="O9001")),   # starts with tool change macro
            (20, dict(exec_state=R, current_program="PART_100")),
            (10, dict(exec_state=R, current_program="L6")),
            (5, dict(exec_state=R, current_program="SOME_OTHER_SUB")),
            (20, dict(exec_state=R, current_program="PART_100")),
            (10, dict(exec_state=I)),
        ])
        self.assertEqual(len(cyc), 1)
        self.assertEqual(cyc[0].program, "PART_100")
        self.assertEqual(cyc[0].program_source, "latched")
        self.assertEqual(cyc[0].result, "COMPLETED")      # no counters known -> reset = complete
        self.assertEqual(cyc[0].end_reason, "RESET")

    def test_reset_without_counter_is_aborted_once_counter_known(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (2, dict(exec_state=I, main_program="O1", part_count=1)),
            (20, dict(exec_state=R, main_program="O1", part_count=1)),
            (1, dict(exec_state=R, main_program="O1", part_count=2)),
            (10, dict(exec_state=I, main_program="O1", part_count=2)),
            (20, dict(exec_state=R, main_program="O1", part_count=2)),
            (10, dict(exec_state=I, main_program="O1", part_count=2)),   # operator reset mid-program
        ])
        self.assertEqual([c.result for c in cyc], ["COMPLETED", "ABORTED"])

    def test_m99_loop_splits_on_counter(self):
        e = CycleEngine("m")
        script = [(1, dict(exec_state=I, main_program="O5", part_count=0))]
        for n in range(1, 4):
            script.append((30, dict(exec_state=R, main_program="O5", part_count=n - 1)))
            script.append((1, dict(exec_state=R, main_program="O5", part_count=n)))
        script.append((10, dict(exec_state=I, main_program="O5", part_count=3)))
        cyc, _ = run(e, script)
        self.assertEqual([c.result for c in cyc], ["COMPLETED"] * 3)

    def test_heidenhain_finished_and_cancelled(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (2, dict(exec_state=I)),
            (30, dict(exec_state=R, main_program="TNC:\\nc_prog\\A.h", current_program="TNC:\\nc_prog\\A.h")),
            (10, dict(exec_state=R, main_program="TNC:\\nc_prog\\A.h", current_program="TNC:\\nc_prog\\TC.h")),
            (5, dict(exec_state=I, main_program="TNC:\\nc_prog\\A.h", program_finished=True)),
            (30, dict(exec_state=R, main_program="TNC:\\nc_prog\\A.h", program_finished=False)),
            (5, dict(exec_state=I, main_program="TNC:\\nc_prog\\A.h", program_aborted=True)),
        ])
        self.assertEqual([(c.program, c.result) for c in cyc], [("A.h", "COMPLETED"), ("A.h", "ABORTED")])

    def test_siemens_path_cleanup(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (20, dict(exec_state=R, main_program="/_N_WKS_DIR/_N_FL_WPD/_N_FLANGE_A_MPF",
                      current_program="L6", program_end_count=1)),
            (1, dict(exec_state=R, main_program="/_N_WKS_DIR/_N_FL_WPD/_N_FLANGE_A_MPF", program_end_count=2)),
            (10, dict(exec_state=I, program_end_count=2)),
        ])
        self.assertEqual(cyc[0].program, "FLANGE_A.MPF")

    def test_start_reset_noise_dropped(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [(10, dict(exec_state=I)), (1, dict(exec_state=R, current_program="X")),
                         (10, dict(exec_state=I))])
        self.assertEqual(cyc, [])

    def test_short_network_drop_keeps_cycle(self):
        e = CycleEngine("m", EngineConfig(offline_grace_s=60))
        cyc, _ = run(e, [
            (30, dict(exec_state=R, main_program="O1", part_count=1)),
            (20, dict(online=False, exec_state=E.OFFLINE)),
            (30, dict(exec_state=R, main_program="O1", part_count=1)),
            (1, dict(exec_state=R, main_program="O1", part_count=2)),
            (10, dict(exec_state=I, main_program="O1", part_count=2)),
        ])
        self.assertEqual(len(cyc), 1)
        self.assertAlmostEqual(cyc[0].duration_s, 81, delta=1)

    def test_mdi_is_setup_not_cycle(self):
        e = CycleEngine("m")
        states = []
        t = 0
        for kw, n in ((dict(exec_state=R, mode=M.MDI), 20), (dict(exec_state=I, mode=M.MDI), 10)):
            for _ in range(n):
                for ev in e.update(Sample(ts=t, **kw)):
                    if isinstance(ev, StateChange):
                        states.append(ev.state.value)
                    else:
                        self.fail("MDI produced a cycle")
                t += 1
        self.assertEqual(states, ["SETUP", "IDLE"])


    def test_pallet_m60_wait_not_in_cycle(self):
        """NHP5000: PROGRAMSTATUS 1000 at start, 4001 at end, then program keeps running in M60
        waiting for pallet clearance (driver reports WAITING). Counter goes up during the wait."""
        W = E.WAITING
        e = CycleEngine("m")
        script = [(5, dict(exec_state=I, end_marker=True, part_count=10))]
        for n in range(3):
            script += [
                (100, dict(exec_state=R, end_marker=False, current_program="PAL1", part_count=10 + n)),
                (3, dict(exec_state=R, end_marker=False, current_program="L6", part_count=10 + n)),
                (300 if n == 1 else 20, dict(exec_state=W, end_marker=True, current_program="PAL1", part_count=10 + n)),
                (5, dict(exec_state=W, end_marker=True, current_program="PAL1", part_count=11 + n)),   # M30 count
            ]
        cyc, _ = run(e, script)
        self.assertEqual(len(cyc), 3)
        for c in cyc:
            self.assertEqual((c.result, c.end_reason, c.parts), ("COMPLETED", "END_MARKER", 1))
            self.assertAlmostEqual(c.duration_s, 103, delta=1)      # the 300 s wait is NOT in the cycle

    def test_marker_counter_before_end_does_not_split(self):
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (5, dict(exec_state=I, end_marker=True, part_count=1)),
            (50, dict(exec_state=R, end_marker=False, part_count=1, current_program="P")),
            (5, dict(exec_state=R, end_marker=False, part_count=2, current_program="P")),   # counts early
            (10, dict(exec_state=E.WAITING, end_marker=True, part_count=2)),
        ])
        self.assertEqual(len(cyc), 1)
        self.assertAlmostEqual(cyc[0].duration_s, 55, delta=1)


    def test_robot_load_program_between_parts(self):
        """DVF BMO: Erowa robot runs LOAD_PRODUCT_0.H as its own main program between parts."""
        e = CycleEngine("m", EngineConfig(wait_rules=[{"program": "^LOAD_PRODUCT", "reason": "robot", "action": "end"}]))
        H, LP = "TNC:\\nc_prog\\PART_1190_OP10.h", "TNC:\\nc_prog\\LOAD_PRODUCT_0.H"
        script = [(2, dict(exec_state=I))]
        for n in range(3):
            script += [(92, dict(exec_state=R, main_program=H, current_program=H)),
                       (40, dict(exec_state=R, main_program=LP, current_program=LP)),
                       (1, dict(exec_state=I, main_program=LP, program_finished=True))]
        script.append((5, dict(exec_state=R, main_program=H, current_program=H, program_finished=False)))
        states = []
        cyc, t = [], 0
        for dur, kw in script:
            for _ in range(dur):
                for ev in e.update(Sample(ts=t, mode=M.AUTO, **kw)):
                    (cyc.append(ev) if isinstance(ev, Cycle) else states.append(ev.state.value))
                t += 1
        self.assertEqual([(c.program, c.result, c.end_reason) for c in cyc],
                         [("PART_1190_OP10.h", "COMPLETED", "ROBOT_WAIT")] * 3)
        for c in cyc:
            self.assertAlmostEqual(c.duration_s, 92, delta=1)
        self.assertIn("WAIT_ROBOT", states)

    def test_robot_macro_inside_cycle_not_counted(self):
        """Puma: robot handshake macro O9022 inside the cycle; 10 min robot wait must not count."""
        e = CycleEngine("m", EngineConfig(wait_rules=[{"program": "^O9022$", "reason": "robot", "action": "pause"}]))
        cyc, _ = run(e, [
            (5, dict(exec_state=I, main_program="O0300", part_count=5)),
            (30, dict(exec_state=R, main_program="O0300", current_program="O9022", part_count=5)),   # quick load
            (400, dict(exec_state=R, main_program="O0300", current_program="O0300", part_count=5)),
            (600, dict(exec_state=R, main_program="O0300", current_program="O9022", part_count=5)),  # robot late
            (20, dict(exec_state=R, main_program="O0300", current_program="O0300", part_count=5)),
            (1, dict(exec_state=R, main_program="O0300", current_program="O0300", part_count=6)),
            (10, dict(exec_state=I, main_program="O0300", part_count=6)),
        ])
        self.assertEqual(len(cyc), 1)
        c = cyc[0]
        self.assertEqual((c.result, c.end_reason), ("COMPLETED", "PART_COUNTER"))
        # the 30 s robot wait at program start is before the cycle (cycle starts when work starts);
        # the 600 s wait in the middle is inside the cycle but not counted
        self.assertAlmostEqual(c.wait_s, 600, delta=2)
        self.assertAlmostEqual(c.duration_s, 421, delta=2)
        self.assertAlmostEqual(c.sub_times["O9022"], 600, delta=2)


    def test_counter_before_door_open_is_one_cycle(self):
        """Lynx 220LSY: part counter rises ~4 s before M30 (then the door opens). Must be ONE cycle of
        ~92 s, not 88 s + a 4 s fragment."""
        e = CycleEngine("m")
        script = [(5, dict(exec_state=I, main_program="P2", part_count=100, program_end_count=50))]
        for n in range(3):
            script += [(88, dict(exec_state=R, main_program="P2", part_count=100 + n, program_end_count=50 + n)),
                       (4, dict(exec_state=R, main_program="P2", part_count=101 + n, program_end_count=50 + n)),
                       (1, dict(exec_state=R, main_program="P2", part_count=101 + n, program_end_count=51 + n)),
                       (20, dict(exec_state=I, main_program="P2", part_count=101 + n, program_end_count=51 + n))]
        cyc, _ = run(e, script)
        self.assertEqual(len(cyc), 3)
        for c in cyc:
            self.assertEqual((c.result, c.end_reason, c.parts), ("COMPLETED", "PART_COUNTER", 1))
            self.assertAlmostEqual(c.duration_s, 92, delta=1)

    def test_bar_feeder_split_at_counter_moment(self):
        """M99 loop: counter rises, program never ends -> after the window the cycle is split exactly
        at the counter moment, and the time after it goes to the next part."""
        e = CycleEngine("m", EngineConfig(counter_end_window_s=30))
        script = [(1, dict(exec_state=I, main_program="O5", part_count=0))]
        for n in range(1, 4):
            script += [(59, dict(exec_state=R, main_program="O5", part_count=n - 1)),
                       (1, dict(exec_state=R, main_program="O5", part_count=n))]
        script += [(20, dict(exec_state=R, main_program="O5", part_count=3)),
                   (10, dict(exec_state=I, main_program="O5", part_count=3))]
        cyc, _ = run(e, script)
        self.assertEqual(len(cyc), 3)
        for c in cyc[:2]:
            self.assertAlmostEqual(c.duration_s, 60, delta=1)
            self.assertAlmostEqual(c.run_s, 60, delta=1)
        self.assertAlmostEqual(cyc[2].duration_s, 60 + 20, delta=1)   # last part ends at the real reset


    def test_idle_second_before_robot_program_not_in_cycle(self):
        """DVF BMO: PART_1190 ends, control idle ~1 s, then the robot program LOAD_PRODUCT starts.
        That idle second is not part of the cycle (showed up as 'paused' in the trend)."""
        e = CycleEngine("m", EngineConfig(wait_rules=[{"program": "^LOAD_PRODUCT", "reason": "robot", "action": "end"}]))
        H, LP = "TNC:\\nc_prog\\PART_1190.h", "TNC:\\nc_prog\\LOAD_PRODUCT_0.H"
        script = []
        for _ in range(3):
            script += [(92, dict(exec_state=R, main_program=H)),
                       (1, dict(exec_state=I, main_program=H)),
                       (40, dict(exec_state=R, main_program=LP))]
        script.append((5, dict(exec_state=R, main_program=H)))
        cyc, _ = run(e, script)
        self.assertEqual(len(cyc), 3)
        for c in cyc:
            self.assertAlmostEqual(c.duration_s, c.run_s, delta=0.01)    # no unexplained time
            self.assertAlmostEqual(c.duration_s, 92, delta=0.01)


    def test_robot_wait_rule_only_in_automatic_mode(self):
        """Puma 2600MB: program held inside the robot macro O9022, operator switched to JOG for hours.
        That is not 'waiting for robot' (showed pink for 3 h while the machine was not in a cycle)."""
        from machine_monitor.model import MachineState as S
        e = CycleEngine("m", EngineConfig(wait_rules=[{"program": "^O9022$", "reason": "robot", "action": "pause"}]))
        run(e, [(10, dict(exec_state=R, main_program="O1234", current_program="O9022"))])
        self.assertEqual(e.state, S.WAIT_ROBOT)                     # automatic: robot handshake
        run(e, [(10, dict(exec_state=P, mode=M.MANUAL, main_program="O1234", current_program="O9022"))], t=10)
        self.assertNotEqual(e.state, S.WAIT_ROBOT)                  # JOG + held: not waiting for the robot

    def test_cycle_already_running_at_monitor_start_is_partial(self):
        """Monitor (re)started while a part was running: the first cycle only saw the tail -> PARTIAL,
        the next full part is COMPLETED as usual."""
        e = CycleEngine("m")
        cyc, _ = run(e, [
            (4, dict(exec_state=R, main_program="O1", part_count=7)),        # monitor starts here
            (1, dict(exec_state=R, main_program="O1", part_count=8)),
            (10, dict(exec_state=I, main_program="O1", part_count=8)),
            (60, dict(exec_state=R, main_program="O1", part_count=8)),
            (1, dict(exec_state=R, main_program="O1", part_count=9)),
            (10, dict(exec_state=I, main_program="O1", part_count=9)),
        ])
        self.assertEqual([c.result for c in cyc], ["PARTIAL", "COMPLETED"])

    def test_reconnect_mid_part_is_partial(self):
        e = CycleEngine("m", EngineConfig(offline_grace_s=60))
        cyc, _ = run(e, [
            (10, dict(exec_state=I, main_program="O1", part_count=1)),
            (30, dict(exec_state=R, main_program="O1", part_count=1)),
            (200, dict(online=False, exec_state=E.OFFLINE)),                  # long network outage
            (20, dict(exec_state=R, main_program="O1", part_count=1)),
            (1, dict(exec_state=R, main_program="O1", part_count=2)),
            (10, dict(exec_state=I, main_program="O1", part_count=2)),
        ])
        self.assertEqual([(c.result, c.end_reason) for c in cyc],
                         [("UNKNOWN", "CONNECTION_LOST"), ("PARTIAL", "PART_COUNTER")])


if __name__ == "__main__":
    unittest.main()

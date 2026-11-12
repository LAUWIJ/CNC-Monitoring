"""Cycle detection engine - the "clever" part.

The naive approach ("cycle = time the program name stays the same") breaks
because many controls report whatever is executing *right now*: a sub-program
(M98 / CALL PGM / L123), an M-code or G-code macro (Fanuc O9001 for M6), a
measuring cycle, ... Each of those would otherwise look like a new part.

This engine therefore decides cycle start/end from the *channel state* and
from *completion signals*, never from the current program name alone:

  START  - channel goes RUNNING in AUTO while no cycle is open.

  STAY   - sub-programs, macros, feed hold, M0/M1, NC-stop, alarms, a mode
           switch to JOG to clear chips... none of these end the cycle. The
           current program name is only recorded as "sub-program seen".

  END    - by the strongest signal the control offers, in this order:
             1. part counter increments          (Fanuc #3901, Siemens actParts)
             2. program-end counter increments   (Siemens oldProgNetTimeCounter, +1 on M30/M2)
             3. program status FINISHED          (Heidenhain)
             4. program status CANCELLED         (Heidenhain -> ABORTED)
             5. channel back in RESET/IDLE for `end_grace_s` seconds
                (used when nothing better exists; if the machine has ever
                produced signal 1-3, a reset without it is classed ABORTED)
             6. connection lost longer than `offline_grace_s`

  MAIN PROGRAM NAME - taken from the control when it reports one (Fanuc
           main-program number, Heidenhain program stack, Siemens workpiece
           program). Otherwise the *first* program seen at cycle start is
           latched, skipping names matching `subprogram_patterns`, and kept
           until the cycle ends.

  M99 LOOPS / BAR FEEDERS - when the counter increments but the channel keeps
           running, the cycle is closed and a new one opened immediately.

  END MARKER (optional, per machine) - a variable the NC program sets, e.g.
           PROGRAMSTATUS = 1000 at start / 4001 at end. The end value closes the
           cycle immediately; while the program then still runs (M60 waiting for
           pallet clearance) the machine is WAITING and no new cycle opens until
           the start value comes back.

  WAIT RULES (optional, per machine) - program names (or NC blocks) that mean
           "waiting", e.g. a robot handshake macro O9022 or a robot load program
           LOAD_PRODUCT_0.H. Reason operator/robot is shown on the timeline.
             action "end"   the part is finished when the wait starts; the next
                            cycle starts when the program continues after it
             action "pause" the wait happens inside the cycle; it is shown but
                            NOT counted in the cycle time

  NOISE   - a cycle that ran less than `min_run_s` and ended without a
           completion signal (cycle start + immediate reset) is dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .model import ExecState, MachineState, Mode, Sample, display_name


@dataclass
class EngineConfig:
    end_grace_s: float = 5.0
    offline_grace_s: float = 120.0
    state_debounce_s: float = 2.0
    min_run_s: float = 3.0
    max_gap_s: float = 30.0
    count_mdi_as_cycles: bool = False
    # part counter rises while the program is still running (e.g. counted before the door opens):
    # keep the cycle open until the program really ends; if it keeps running this long (M99 loop /
    # bar feeder, no program end), split the cycle at the moment the counter rose.
    counter_end_window_s: float = 30.0
    # [{"program": regex} or {"block": regex}, "reason": "robot"|"operator", "action": "end"|"pause"}]
    wait_rules: list = field(default_factory=list)
    subprogram_patterns: list = field(default_factory=lambda: [
        r"^O?9\d{3}$",          # Fanuc macro programs O9000-O9999 (M/G-code macros)
        r"^L\d+",               # Siemens L-subprograms
        r"\.SPF$",              # Siemens sub-program files
        r"^CYCLE\d+",           # Siemens standard cycles
        r"^_?TC\b|TOOL_?CHANGE|TOOLCHANGE|^L6$",  # tool change programs
        r"^TCH",                # Heidenhain touch probe helpers
    ])


@dataclass
class Cycle:
    machine_id: str
    program: Optional[str]
    program_path: Optional[str]
    program_source: str                 # control | latched | pending
    start_ts: float
    end_ts: Optional[float] = None
    run_s: float = 0.0
    pause_s: float = 0.0
    alarm_s: float = 0.0
    wait_s: float = 0.0
    result: Optional[str] = None        # COMPLETED | ABORTED | UNKNOWN
    end_reason: Optional[str] = None
    part_count_start: Optional[int] = None
    part_count_end: Optional[int] = None
    parts: int = 0
    subprograms: list = field(default_factory=list)
    sub_times: dict = field(default_factory=dict)   # seconds spent in each sub-program / macro
    tool_times: dict = field(default_factory=dict)  # running seconds per tool (key = tool number)
    tool_info: dict = field(default_factory=dict)   # tool number -> {"name": .., "group": ..}
    partial: bool = False     # already running when monitoring started/reconnected: start not seen
    mode: str = "AUTO"

    @property
    def duration_s(self) -> float:
        """Cycle time: start to end, minus waiting time (robot/operator waits inside the cycle)."""
        return max(0.0, (self.end_ts or self.start_ts) - self.start_ts - self.wait_s)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["duration_s"] = self.duration_s
        return d


@dataclass
class StateChange:
    machine_id: str
    state: MachineState
    start_ts: float
    program: Optional[str]


def events_closed(events) -> bool:
    return any(isinstance(e, Cycle) for e in events)


class CycleEngine:
    def __init__(self, machine_id: str, cfg: Optional[EngineConfig] = None,
                 has_completion_signal: bool = False):
        self.machine_id = machine_id
        self.cfg = cfg or EngineConfig()
        self._sub_re = [re.compile(p, re.I) for p in self.cfg.subprogram_patterns]
        self._wait_rules = []
        for r in self.cfg.wait_rules or []:
            on = "block" if r.get("block") else "program"
            self._wait_rules.append((on, re.compile(r.get(on), re.I),
                                     "robot" if str(r.get("reason", "operator")).lower() == "robot" else "operator",
                                     "end" if str(r.get("action", "pause")).lower() == "end" else "pause"))
        self._wait_action = None
        self._pending = None          # part counted, waiting for the program to really end
        self.has_completion_signal = has_completion_signal

        self.cycle: Optional[Cycle] = None
        self.prev: Optional[Sample] = None
        self._prev_raw: Optional[MachineState] = None
        self._last_part: Optional[int] = None
        self._last_end: Optional[int] = None
        self._last_finished = False
        self._last_aborted = False
        self._last_marker = False
        self.uses_marker = False
        self._idle_since: Optional[float] = None
        self._offline_since: Optional[float] = None

        self.raw_state: Optional[MachineState] = None
        self.raw_since: float = 0.0
        self.state: Optional[MachineState] = None
        self.state_since: float = 0.0
        self.last_sample: Optional[Sample] = None

    # ------------------------------------------------------------------ helpers
    def is_subprogram(self, name: Optional[str]) -> bool:
        return bool(name) and any(r.search(name) for r in self._sub_re)

    @staticmethod
    def classify(s: Sample) -> MachineState:
        if not s.online or s.exec_state == ExecState.OFFLINE:
            return MachineState.OFFLINE
        if s.exec_state == ExecState.RUNNING:
            return MachineState.RUNNING if s.mode in (Mode.AUTO, Mode.UNKNOWN) else MachineState.SETUP
        if s.exec_state == ExecState.WAITING:
            return MachineState.WAIT_ROBOT if s.wait_reason == "robot" else MachineState.WAITING
        if s.alarm or s.exec_state == ExecState.ALARM:
            return MachineState.ALARM
        if s.exec_state == ExecState.PAUSED:
            return MachineState.PAUSED
        return MachineState.IDLE

    def _counts_as_cycle_run(self, s: Sample) -> bool:
        if not s.online or s.exec_state != ExecState.RUNNING:
            return False
        if s.mode in (Mode.AUTO, Mode.UNKNOWN):
            return True
        return self.cfg.count_mdi_as_cycles and s.mode == Mode.MDI

    # --------------------------------------------------------------- main entry
    def update(self, s: Sample) -> list:
        """Feed one poll result. Returns a list of StateChange / Cycle (closed) events."""
        events: list = []
        prev = self.prev
        main = display_name(s.main_program)
        cur = display_name(s.current_program)
        wait_hit = self._match_wait(s, main, cur)
        # a wait rule only counts in automatic mode: a program held inside the robot macro while the operator
        # switched to JOG/handwheel (Puma 2600MB, hours in O9022 + JOG) is not "waiting for the robot"
        if (wait_hit and s.online and s.mode in (Mode.AUTO, Mode.UNKNOWN)
                and s.exec_state in (ExecState.RUNNING, ExecState.PAUSED, ExecState.WAITING)):
            s.exec_state, s.wait_reason = ExecState.WAITING, wait_hit[0]
        else:
            wait_hit = None
        raw = self.classify(s)

        # 1. accumulate time of the interval since the previous sample into the open cycle
        if self.cycle is not None and prev is not None and self._prev_raw is not None:
            dt = min(max(s.ts - prev.ts, 0.0), self.cfg.max_gap_s)
            if self._prev_raw in (MachineState.RUNNING, MachineState.SETUP):
                self.cycle.run_s += dt
            elif self._prev_raw == MachineState.ALARM:
                self.cycle.alarm_s += dt
            elif self._prev_raw == MachineState.PAUSED:
                self.cycle.pause_s += dt
            elif self._prev_raw in (MachineState.WAITING, MachineState.WAIT_ROBOT):
                self.cycle.wait_s += dt
            pc = display_name(prev.current_program)
            if pc and pc != self.cycle.program:
                self.cycle.sub_times[pc] = self.cycle.sub_times.get(pc, 0.0) + dt
            if prev.tool and self._prev_raw == MachineState.RUNNING:
                c = self.cycle
                c.tool_times[prev.tool] = c.tool_times.get(prev.tool, 0.0) + dt
                if prev.tool not in c.tool_info and len(c.tool_info) < 100:
                    c.tool_info[prev.tool] = {"name": prev.tool_name, "group": prev.tool_group}
                elif prev.tool_group and not c.tool_info[prev.tool].get("group"):
                    c.tool_info[prev.tool]["group"] = prev.tool_group

        # 2. edge detection on completion signals
        part_inc = end_inc = False
        if s.part_count is not None:
            if self._last_part is not None and s.part_count > self._last_part:
                part_inc = True
            self._last_part = s.part_count
        if s.program_end_count is not None:
            if self._last_end is not None and s.program_end_count > self._last_end:
                end_inc = True
            self._last_end = s.program_end_count
        finished_edge = bool(s.program_finished) and not self._last_finished
        aborted_edge = bool(s.program_aborted) and not self._last_aborted
        if s.program_finished is not None:
            self._last_finished = bool(s.program_finished)
        if s.program_aborted is not None:
            self._last_aborted = bool(s.program_aborted)
        marker_edge = bool(s.end_marker) and not self._last_marker
        if s.end_marker is not None:
            self._last_marker = bool(s.end_marker)
            self.uses_marker = True
        if self.uses_marker:
            # the NC program's own end marker is authoritative: counters/status may change a moment
            # before or after it and must not split the cycle
            part_inc = end_inc = finished_edge = False
        if part_inc or end_inc or finished_edge or marker_edge:
            self.has_completion_signal = True

        # 2b. wait rule with action "end": the part is done when the wait begins
        wait_end_edge = bool(wait_hit and wait_hit[1] == "end") and self._wait_action != "end"
        self._wait_action = wait_hit[1] if wait_hit else None
        if wait_end_edge and self.cycle is not None:
            # if the control already stopped (idle between the part program and the robot/load program),
            # the part ended THEN - not one poll later when the wait program started
            end = self._idle_since if self._idle_since is not None else s.ts
            events += self._close("COMPLETED", "ROBOT_WAIT" if wait_hit[0] == "robot" else "OPERATOR_WAIT", end, s)

        # 3. bookkeeping inside an open cycle (sub-programs, late program name)
        if self.cycle is not None and s.online:
            c = self.cycle
            if c.program_source != "control" and main:
                c.program, c.program_path, c.program_source = main, s.main_program, "control"
            elif c.program_source == "pending" and cur and not self.is_subprogram(cur):
                c.program, c.program_path, c.program_source = cur, s.current_program, "latched"
            if cur and cur != c.program and cur not in c.subprograms and len(c.subprograms) < 50:
                c.subprograms.append(cur)

        # 4. decide whether the open cycle ends
        if self.cycle is None:
            self._pending = None
        elif part_inc and s.exec_state == ExecState.RUNNING and not (marker_edge or end_inc or finished_edge):
            # counted, but the program is still running: wait for the real end (step 4a)
            if self._pending is None:
                c = self.cycle
                self._pending = {"ts": s.ts, "run_s": c.run_s, "pause_s": c.pause_s, "alarm_s": c.alarm_s,
                                 "wait_s": c.wait_s, "sub_times": dict(c.sub_times),
                                 "tool_times": dict(c.tool_times), "part": self._last_part}
            part_inc = False
        if self.cycle is not None and self._pending is not None:
            p = self._pending
            if marker_edge or end_inc or finished_edge or s.exec_state == ExecState.IDLE:
                self._pending = None            # program really ended: close now, as a counted part
                events += self._close("COMPLETED", "PART_COUNTER", s.ts, s)
            elif s.ts - p["ts"] >= self.cfg.counter_end_window_s:
                self._pending = None            # still running: M99 loop -> split at the counter moment
                events += self._split_at(p, s)
        if self.cycle is not None and not events_closed(events):
            c = self.cycle
            if marker_edge or part_inc or end_inc or finished_edge:
                reason = ("END_MARKER" if marker_edge else "PART_COUNTER" if part_inc
                          else "PROGRAM_END" if end_inc else "FINISHED")
                events += self._close("COMPLETED", reason, s.ts, s)
            elif aborted_edge:
                events += self._close("ABORTED", "CANCELLED", s.ts, s)
            elif (s.online and main and c.program_source == "control" and main != c.program
                  and s.exec_state == ExecState.RUNNING and s.mode in (Mode.AUTO, Mode.UNKNOWN)):
                events += self._close("UNKNOWN", "PROGRAM_CHANGED", s.ts, s)
            elif not s.online:
                if self._offline_since is None:
                    self._offline_since = prev.ts if (prev and prev.online) else s.ts
                if s.ts - self._offline_since >= self.cfg.offline_grace_s:
                    events += self._close("UNKNOWN", "CONNECTION_LOST", self._offline_since, s)
            elif s.exec_state == ExecState.IDLE:
                self._offline_since = None
                if self._idle_since is None:
                    self._idle_since = s.ts
                if s.ts - self._idle_since >= self.cfg.end_grace_s:
                    result = "ABORTED" if self.has_completion_signal else "COMPLETED"
                    events += self._close(result, "RESET", self._idle_since, s)
            else:
                self._idle_since = None
                self._offline_since = None

        # 5. open a new cycle
        if self.cycle is None and self._counts_as_cycle_run(s):
            if main:
                prog, path, src = main, s.main_program, "control"
            elif cur and not self.is_subprogram(cur):
                prog, path, src = cur, s.current_program, "latched"
            else:
                prog, path, src = None, None, "pending"
            self.cycle = Cycle(self.machine_id, prog, path, src, s.ts,
                               part_count_start=self._last_part,
                               mode=s.mode.value,
                               # first sample after (re)start or after being offline, and already running:
                               # the real start of this part was never seen
                               partial=(prev is None or self._prev_raw == MachineState.OFFLINE))
            self._idle_since = None
            self._offline_since = None

        # 6. debounced machine state for the timeline
        if raw != self.raw_state:
            self.raw_state, self.raw_since = raw, s.ts
        if self.state is None or (raw != self.state and s.ts - self.raw_since >= self.cfg.state_debounce_s):
            self.state, self.state_since = raw, (self.raw_since if self.state is not None else s.ts)
            events.append(StateChange(self.machine_id, raw, self.state_since, self.current_program_name(s)))

        self.prev = s
        self._prev_raw = raw
        self.last_sample = s
        return events

    def _split_at(self, p: dict, s: Sample) -> list:
        """Close the open cycle at the counter moment p['ts']; the time after it belongs to a new cycle."""
        c = self.cycle
        after = dict(run_s=c.run_s - p["run_s"], pause_s=c.pause_s - p["pause_s"],
                     alarm_s=c.alarm_s - p["alarm_s"], wait_s=c.wait_s - p["wait_s"])
        after_subs = {k: v - p["sub_times"].get(k, 0.0) for k, v in c.sub_times.items()
                      if v - p["sub_times"].get(k, 0.0) > 0}
        after_tools = {k: v - p["tool_times"].get(k, 0.0) for k, v in c.tool_times.items()
                       if v - p["tool_times"].get(k, 0.0) > 0}
        c.run_s, c.pause_s, c.alarm_s, c.wait_s = p["run_s"], p["pause_s"], p["alarm_s"], p["wait_s"]
        c.sub_times = p["sub_times"]
        c.tool_times = p["tool_times"]
        last = self._last_part
        self._last_part = p["part"]
        events = self._close("COMPLETED", "PART_COUNTER", p["ts"], s)
        self._last_part = last
        self.cycle = Cycle(self.machine_id, c.program, c.program_path, c.program_source, p["ts"],
                           part_count_start=p["part"], mode=c.mode, sub_times=after_subs,
                           tool_times=after_tools, tool_info=dict(c.tool_info), **after)
        return events

    def _close(self, result: str, reason: str, end_ts: float, s: Sample) -> list:
        c = self.cycle
        self.cycle = None
        self._idle_since = None
        self._offline_since = None
        if c is None:
            return []
        c.end_ts = max(end_ts, c.start_ts)
        if c.partial and result == "COMPLETED":
            result = "PARTIAL"            # not a full cycle - kept in the log, left out of the statistics
        c.result, c.end_reason = result, reason
        c.part_count_end = self._last_part
        if c.part_count_start is not None and c.part_count_end is not None and c.part_count_end >= c.part_count_start:
            c.parts = c.part_count_end - c.part_count_start
        else:
            c.parts = 1 if result == "COMPLETED" else 0
        if result == "COMPLETED" and c.parts == 0:
            c.parts = 1          # counter increments after the end marker (e.g. at M30 after M60)
        if c.run_s < self.cfg.min_run_s:
            return []          # start + immediate reset, or a sliver between two end signals: noise
        if c.program is None:
            c.program = "(unknown)"
        return [c]

    def _match_wait(self, s: Sample, main, cur):
        for on, rx, reason, action in self._wait_rules:
            if on == "block":
                if s.current_block and rx.search(s.current_block):
                    return reason, action
            elif (cur and rx.search(cur)) or (main and rx.search(main)):
                return reason, action
        return None

    # ------------------------------------------------------------- live status
    def current_program_name(self, s: Optional[Sample] = None) -> Optional[str]:
        if self.cycle and self.cycle.program:
            return self.cycle.program
        s = s or self.last_sample
        if s is None:
            return None
        return display_name(s.main_program) or display_name(s.current_program)

    def snapshot(self) -> dict:
        s = self.last_sample
        c = self.cycle
        return {
            "state": self.state.value if self.state else None,
            "state_since": self.state_since,
            "online": bool(s and s.online),
            "exec_state": s.exec_state.value if s else None,
            "mode": s.mode.value if s else None,
            "alarm": bool(s and s.alarm),
            "program": self.current_program_name(),
            "current_program": display_name(s.current_program) if s else None,
            "program_path": (s.main_program if s else None),     # full path on the control (transfer panel)
            "part_count": s.part_count if s else None,
            "cycle_start": c.start_ts if c else None,
            "cycle_run_s": c.run_s if c else None,
            "cycle_pause_s": (c.pause_s + c.alarm_s) if c else None,
            "cycle_wait_s": c.wait_s if c else None,
            "cycle_tool_times": ({k: round(v) for k, v in c.tool_times.items()} if c else {}),
            "wait_reason": s.wait_reason if s else None,
            "tool": s.tool if s else None,
            "tool_name": s.tool_name if s else None,
            "tool_group": s.tool_group if s else None,
            "last_ts": s.ts if s else None,
            "extra": (s.extra if s else {}),
        }

"""Simulated machine - for trying the dashboard without a real control and for
testing the cycle engine. `flavor` mimics what each control reports:

  fanuc       main program number + current program jumps to O9001/O8xxx macros, part counter
  siemens     workpiece path + L6/CYCLE800 subs, program-end counter + part counter
  heidenhain  program stack main/current, FINISHED / CANCELLED status
  generic     ONLY the current program name, no counters (worst case)

Optional working pattern (makes week / month views look like a real shop):
  hours     = [6, 22]   machine is switched on from 06:00 to 22:00 (local time); [0, 24] = around the clock
  weekends  = false     switched off on Saturday and Sunday
  down_days = 0.04      chance that the machine is off for a whole working day (maintenance, no work)
Without these the simulated machine runs 24/7.
"""
from __future__ import annotations

import random
import time

from ..model import ExecState, Mode, Sample
from .base import Driver

PROGRAMS = {
    "fanuc": [("O1234", 420), ("O2040", 180), ("O3110", 900)],
    "siemens": [("FLANGE_A", 300), ("PIN_D40", 140), ("HOUSING_7", 1100)],
    "heidenhain": [("ADAPTER_PIN.h", 380), ("TYPEPLATE_HOLDER.h", 220), ("BRACKET_RVS.h", 650)],
    "generic": [("PART_100", 260), ("PART_200", 520)],
}
# tools per program: FANUC lathe style T codes ("101" = T01 offset 01) with tool life groups (T0199 calls
# group 1); HEIDENHAIN / SINUMERIK by tool number with a name, as the controls keep them.
TOOL_GROUPS = {1: (9, ["101", "1313", "1212", "2424"]), 2: (24, ["202", "1414"]), 3: (24, ["303", "1515"]),
               7: (100, ["707"])}
TOOLS = {
    "fanuc": {"O1234": ["G1", "505", "G2"], "O2040": ["G3", "606"], "O3110": ["G1", "G2", "G7", "505"]},
    "siemens": {"FLANGE_A": ["1", "4", "9"], "PIN_D40": ["2", "4"], "HOUSING_7": ["1", "3", "4", "9", "12"]},
    "heidenhain": {"ADAPTER_PIN.h": ["5", "12", "21"], "TYPEPLATE_HOLDER.h": ["5", "33", "12"],
                   "BRACKET_RVS.h": ["5", "21", "12", "33", "40"]},
}
TOOL_NAMES = {
    "siemens": {"1": "FACEMILL_D63", "2": "TURN_ROUGH_80", "3": "ENDMILL_D16", "4": "DRILL_D8.5",
                "9": "THREADMILL_M10", "12": "CHAMFER_D12_90"},
    "heidenhain": {"5": "FACEMILL_D50", "12": "DRILL_D6.8", "21": "ENDMILL_D10_RVS", "33": "CHAMFER_D10_90",
                   "40": "TAP_M8"},
}

SUBS = {
    "fanuc": ["O9001", "O8000", "O9010"],
    "siemens": ["L6", "CYCLE800", "CYCLE81"],
    "heidenhain": ["TOOLCHANGE.h", "PROBE_ZERO.h"],
    "generic": ["O9001", "L6", "TOOL_CHANGE"],
}


class SimulatedDriver(Driver):
    control = "simulator"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.flavor = cfg.get("flavor", "fanuc")
        self.rng = random.Random(cfg.get("seed", hash(cfg.get("id", "x")) & 0xFFFF))
        self.speed = float(cfg.get("speed", 1.0))       # >1 = shorter cycles for demos
        self.queue: list = []
        self.phase = None
        self.phase_end = 0.0
        self.parts = self.rng.randint(100, 900)
        self.end_count = self.rng.randint(1000, 5000)
        self.finished = False
        self.aborted = False
        self.job = None
        self.batch_left = 0
        self.hours = cfg.get("hours")                  # e.g. [6, 22]; None = always on
        self.weekends = bool(cfg.get("weekends", True))
        self.down_days = float(cfg.get("down_days", 0.0))
        self._off = False
        # FANUC tool life groups: {grp: {"life", "count", "use" (1-based), "status": [..]}}
        self.groups = {g: {"life": life, "count": self.rng.randint(0, life - 1), "use": 1,
                           "status": [1] * len(tools)} for g, (life, tools) in TOOL_GROUPS.items()}

    def connect(self) -> None:
        pass

    # ----------------------------------------------------------- scheduling
    def _p(self, dur, exec_state, mode=Mode.AUTO, cur=None, alarm=False, online=True, on_start=None, tool=None):
        return {"dur": max(1.0, dur / self.speed),
                "exec": exec_state, "mode": mode, "cur": cur, "alarm": alarm, "online": online,
                "on_start": on_start, "tool": tool}

    def _plan(self):
        r = self.rng
        if self.batch_left <= 0:
            self.job = r.choice(PROGRAMS[self.flavor])
            self.batch_left = r.randint(3, 12)
            # changeover: idle + setup in MDI
            self.queue.append(self._p(r.uniform(600, 2400), ExecState.IDLE, Mode.MANUAL))
            for _ in range(r.randint(1, 3)):
                self.queue.append(self._p(r.uniform(20, 90), ExecState.RUNNING, Mode.MDI, cur="MDI"))
                self.queue.append(self._p(r.uniform(30, 200), ExecState.IDLE, Mode.MDI))
            if r.random() < 0.08:
                self.queue.append(self._p(r.uniform(60, 400), ExecState.OFFLINE, online=False))
        self.batch_left -= 1
        prog, nominal = self.job
        total = nominal * r.uniform(0.97, 1.05)
        # load part
        self.queue.append(self._p(r.uniform(25, 120) if r.random() > 0.15 else r.uniform(300, 1500),
                                  ExecState.IDLE, Mode.AUTO))
        n_chunks = r.randint(4, 8)
        abort_at = r.randint(1, n_chunks - 1) if r.random() < 0.06 else None
        alarm_at = r.randint(1, n_chunks - 1) if r.random() < 0.03 else None
        subs = SUBS[self.flavor]
        tools = TOOLS.get(self.flavor, {}).get(prog, [])
        for i in range(n_chunks):
            first = i == 0
            self.queue.append(self._p(total / n_chunks * 0.85, ExecState.RUNNING, cur=prog,
                                      on_start=self._on_start if first else None,
                                      tool=tools[i % len(tools)] if tools else None))
            self.queue.append(self._p(total / n_chunks * 0.15, ExecState.RUNNING, cur=r.choice(subs)))
            if r.random() < 0.12:
                self.queue.append(self._p(r.uniform(8, 120), ExecState.PAUSED, cur=prog))   # feed hold / M0
            if abort_at == i:
                self.queue.append(self._p(r.uniform(5, 40), ExecState.PAUSED, cur=prog))
                self.queue.append(self._p(1, ExecState.IDLE, on_start=self._on_abort))
                return
            if alarm_at == i:
                self.queue.append(self._p(r.uniform(60, 600), ExecState.ALARM, cur=prog, alarm=True))
                self.queue.append(self._p(1, ExecState.IDLE, on_start=self._on_abort))
                return
        # M30: counter goes up while still "running" for a moment, then reset
        self.queue.append(self._p(1.5, ExecState.RUNNING, cur=prog, on_start=self._on_end))
        self.queue.append(self._p(1, ExecState.IDLE))

    def _on_start(self):
        self.finished = self.aborted = False

    def _on_end(self):
        self.parts += 1
        self.end_count += 1
        self.finished = True
        # count one cycle on every tool group this program called (like FANUC tool life management)
        for t in set(TOOLS.get(self.flavor, {}).get(self.job[0] if self.job else "", [])):
            if t.startswith("G"):
                g = self.groups[int(t[1:])]
                g["count"] += 1
                if g["count"] >= g["life"]:
                    g["status"][g["use"] - 1] = 2                       # life expired -> next sister tool
                    nxt = [i for i, st in enumerate(g["status"], 1) if st == 1]
                    if nxt:
                        g["use"], g["count"] = nxt[0], 0
                    else:                                            # all used up: operator puts new tools in
                        g["status"], g["use"], g["count"] = [1] * len(g["status"]), 1, 0

    def _tool(self, t):
        """(tool number, name, group) of a phase's tool."""
        if t is None:
            return None, None, None
        if t.startswith("G"):
            gno = int(t[1:])
            g = self.groups[gno]
            return TOOL_GROUPS[gno][1][g["use"] - 1], None, gno
        return t, TOOL_NAMES.get(self.flavor, {}).get(t), None

    def read_tool_life(self, groups=None) -> list:
        if self.flavor != "fanuc":
            return []
        out = []
        for gno in (sorted(self.groups) if groups is None else sorted(groups)):
            if gno not in self.groups:
                continue
            g, tools = self.groups[gno], TOOL_GROUPS[gno][1]
            out.append({"grp": gno, "count_type": "cycles", "life": g["life"], "count": g["count"],
                        "ntool": len(tools), "nfree": sum(1 for st in g["status"] if st == 1),
                        "use_tool": g["use"],
                        "tools": [{"num": int(n), "status": st} for n, st in zip(tools, g["status"])]})
        return out

    def _on_abort(self):
        self.aborted = True

    # ---------------------------------------------------------------- read
    def _switched_off(self, ts: float) -> bool:
        if not (self.hours or not self.weekends or self.down_days):
            return False
        lt = time.localtime(ts)
        if not self.weekends and lt.tm_wday >= 5:
            return True
        if self.down_days and random.Random(f"{self.cfg.get('seed')}-{lt.tm_year}-{lt.tm_yday}").random() < self.down_days:
            return True
        if self.hours:
            h = lt.tm_hour + lt.tm_min / 60
            return not (self.hours[0] <= h < self.hours[1])
        return False

    def sample_at(self, ts: float) -> Sample:
        if self._switched_off(ts):
            self._off = True
            return Sample(ts=ts, online=False, exec_state=ExecState.OFFLINE)
        if self._off:                     # switched on again: start the day with a fresh setup
            self._off = False
            self.queue, self.phase, self.batch_left = [], None, 0
        return self._sample(ts)

    def _sample(self, ts: float) -> Sample:
        while self.phase is None or ts >= self.phase_end:
            if not self.queue:
                self._plan()
            start = self.phase_end if self.phase is not None else ts
            self.phase = self.queue.pop(0)
            self.phase_end = start + self.phase["dur"]
            if self.phase["on_start"]:
                self.phase["on_start"]()
        p = self.phase
        prog = self.job[0] if self.job else None
        f = self.flavor
        s = Sample(ts=ts, online=p["online"], exec_state=p["exec"], mode=p["mode"], alarm=p["alarm"])
        if not p["online"]:
            s.exec_state = ExecState.OFFLINE
            return s
        cur = p["cur"] or (prog if p["mode"] == Mode.AUTO else None)
        if p.get("tool") and f != "generic":
            s.tool, s.tool_name, s.tool_group = self._tool(p["tool"])
        if f == "fanuc":
            s.main_program = prog if p["mode"] == Mode.AUTO else "O0001"
            s.current_program = cur
            s.part_count = self.parts
        elif f == "siemens":
            if prog:
                s.main_program = f"/_N_WKS_DIR/_N_{prog.split('_')[0]}_WPD/_N_{prog}_MPF"
            s.current_program = (f"_N_{cur}_MPF" if cur == prog else cur)
            s.part_count = self.parts
            s.program_end_count = self.end_count
        elif f == "heidenhain":
            s.main_program = f"TNC:\\nc_prog\\{prog}" if prog else None
            s.current_program = f"TNC:\\nc_prog\\{cur}" if cur else None
            if p["exec"] == ExecState.IDLE:
                s.program_finished = self.finished
                s.program_aborted = self.aborted
            else:
                s.program_finished = s.program_aborted = False
        else:
            s.current_program = cur
        return s

    def read(self) -> Sample:
        return self.sample_at(time.time())

    def read_alarms(self):
        """Demo: an alarm while the simulated machine is in ALARM, and a message while it is paused."""
        p = self.phase or {}
        if self._off or not p.get("online", True):
            return []
        out = []
        if p.get("alarm"):
            out.append({"code": "SV0401", "text": "SERVO V-READY OFF (X)" if self.flavor == "fanuc"
                        else "Axis X: drive not ready"})
        if p.get("exec") == ExecState.PAUSED:
            out.append({"code": "MSG 2010", "text": "DOOR OPEN - CHECK PART"})
        return out

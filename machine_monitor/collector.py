"""One polling thread per machine: driver -> engine -> store."""
from __future__ import annotations

import logging
import threading
import time

from .drivers import make_driver
from .engine import Cycle, CycleEngine, EngineConfig, StateChange
from .model import ExecState, Mode, Sample
from .store import Store

log = logging.getLogger("collector")

ENGINE_KEYS = ("end_grace_s", "offline_grace_s", "state_debounce_s", "min_run_s", "max_gap_s",
               "count_mdi_as_cycles", "subprogram_patterns")


def engine_config(defaults: dict, mcfg: dict) -> EngineConfig:
    kw = {}
    for k in ENGINE_KEYS:
        if k in mcfg:
            kw[k] = mcfg[k]
        elif k in defaults:
            kw[k] = defaults[k]
    if mcfg.get("wait"):
        kw["wait_rules"] = list(mcfg["wait"])
    if mcfg.get("warmup_programs"):
        w = mcfg["warmup_programs"]
        kw["warmup_programs"] = [w] if isinstance(w, str) else list(w)
    if "extra_subprogram_patterns" in mcfg:
        kw["subprogram_patterns"] = list(kw.get("subprogram_patterns", EngineConfig().subprogram_patterns)) + \
            list(mcfg["extra_subprogram_patterns"])
    return EngineConfig(**kw)


class MachineWorker(threading.Thread):
    def __init__(self, mcfg: dict, defaults: dict, store: Store):
        super().__init__(daemon=True, name=f"machine-{mcfg['id']}")
        self.mcfg = mcfg
        self.id = mcfg["id"]
        self.store = store
        self.poll = float(mcfg.get("poll_interval_s", defaults.get("poll_interval_s", 1.0)))
        self.driver = make_driver(mcfg)
        ctrl = mcfg.get("control", "?")
        if ctrl == "simulator":               # show which control it imitates, clearly marked as demo
            ctrl = f"{mcfg.get('flavor', 'sim')} · demo"
        has_sig = store.register_machine(self.id, mcfg.get("name", self.id), ctrl)
        self.engine = CycleEngine(self.id, engine_config(defaults, mcfg), has_completion_signal=has_sig)
        self.stop_evt = threading.Event()
        self.connected = False
        self._last_live = 0.0
        self.last_error = None
        # alarms: display only - read every alarm_poll_s, stored in their own table, never fed to the engine
        self.read_alarms = bool(mcfg.get("alarms", defaults.get("alarms", True)))
        self.alarm_poll = float(mcfg.get("alarm_poll_s", defaults.get("alarm_poll_s", 5.0)))
        self._alarm_next = 0.0
        self._alarm_open: dict = {}      # (code, text) -> alarm row id
        self._alarm_ok = False
        self._alarm_err_logged = False
        self._tool_out = False           # tool life of the program used up (checked while standing still)
        self._tool_check = 0.0
        self._tool_reread = None         # (due ts, tools): second read shortly after a part (SINUMERIK)

    # ------------------------------------------------------------- handling
    def _tool_life_out(self) -> bool:
        snap = self.engine.snapshot()
        try:
            st = self.store.tool_status(self.id, snap.get("program"), snap)
        except Exception:
            return False
        # really used up (FANUC count reached, HEIDENHAIN time up) - not "less than one part's worth left",
        # which a HEIDENHAIN still runs
        return bool(st.get("tool_left_expired"))

    def tool_life_wait(self, s: Sample) -> None:
        """Standing still in automatic between parts while the program's tool life is used up (FANUC: S-No.2170
        TOOL LIFE COUNT END ALARM, a PMC message - not a CNC alarm): the machine waits for the operator to change
        the tool, so show it as waiting for the operator instead of stationary."""
        if not (s.online and s.exec_state == ExecState.IDLE and s.mode in (Mode.AUTO, Mode.UNKNOWN)
                and self.engine.cycle is None):
            self._tool_check = 0.0                   # look again the next time it stands still
            return
        if s.ts >= self._tool_check:
            self._tool_check = s.ts + (60 if self._tool_out else 15)
            if self._tool_out and (not getattr(self.driver, "tool_life_after_every_cycle", False)
                                   or getattr(self.driver, "tool_life_cheap", False)):
                # FANUC / SINUMERIK: read again, so a life reset by the operator ends the wait without waiting
                # for the next part (HEIDENHAIN tool table reads stay limited to the agreed moments)
                self.read_tool_life(None, s.ts)
            self._tool_out = self._tool_life_out()
        if self._tool_out:
            s.exec_state, s.wait_reason = ExecState.WAITING, "operator"
            s.extra = {**(s.extra or {}), "waiting_for": "tool change - tool life used up"}

    def handle(self, s: Sample, live: bool = True):
        if live:
            if self._tool_reread and s.ts >= self._tool_reread[0]:
                tools, self._tool_reread = self._tool_reread[1], None
                self.read_tool_life(tools, s.ts)
            self.tool_life_wait(s)
        for ev in self.engine.update(s):
            if isinstance(ev, StateChange):
                self.store.add_state(self.id, ev.state.value, ev.start_ts, ev.program)
            elif isinstance(ev, Cycle):
                self.store.add_cycle(ev)
                groups = {i.get("group") for i in (ev.tool_info or {}).values() if i.get("group")}
                if getattr(self.driver, "tool_life_after_every_cycle", False):
                    # HEIDENHAIN: life per tool from the tool table, read once at the end of every part
                    self.read_tool_life(set(ev.tool_times or {}), ev.end_ts or s.ts)
                    delay = getattr(self.driver, "tool_life_reread_s", 0)
                    if delay and live and ev.tool_times:
                        self._tool_reread = (s.ts + delay, set(ev.tool_times))
                elif groups:
                    self.read_tool_life(groups, ev.end_ts or s.ts)
                if live:
                    log.info("[%s] cycle %s %s %.0fs (%s)", self.id, ev.program, ev.result, ev.duration_s, ev.end_reason)
        fresh = getattr(self.driver, "fresh_tool_life", None) if live else None
        if fresh is not None:
            # HEIDENHAIN: tool table read at the start of a part after a setup (tools swapped / life reset):
            # store it now. Read as the part starts: dated at the part's start, so the running time of this part
            # is counted on top of it (store.tool_status); a read later in a part (unknown tool) keeps its own time
            try:
                groups = fresh()
                if groups is not None:
                    c = self.engine.cycle
                    near_start = c is not None and 0 <= s.ts - c.start_ts < 30
                    self._store_tool_life(groups, c.start_ts if near_start else s.ts)
            except Exception as e:
                log.warning("[%s] tool life: %s", self.id, e)
        if live and (s.ts - self._last_live >= 2.0 or self.engine.state_since >= self._last_live):
            snap = self.engine.snapshot()
            snap["error"] = self.last_error
            self.store.update_live(self.id, snap, self.engine.has_completion_signal)
            self._last_live = s.ts

    def read_tool_life(self, groups, ts: float):
        """Tool life of the given groups (None = all) from the control, stored when changed. Never fatal."""
        fn = getattr(self.driver, "read_tool_life", None)
        if fn is None:
            return
        try:
            self._store_tool_life(fn(groups) or [], ts)
        except Exception as e:
            log.warning("[%s] tool life: %s", self.id, e)

    def _store_tool_life(self, groups: list, ts: float) -> None:
        for g in groups:
            self.store.add_tool_life(self.id, ts, g)
        # tools whose life time was set back to 0 on the control (monitoring switched off): store an empty
        # row, so the dashboard stops showing the last value
        cleared = getattr(self.driver, "tools_without_life", lambda: set())()
        if cleared:
            for grp, r in self.store.tool_life_now(self.id).items():
                if grp < 0 and -grp in cleared and r.get("left_amount") is not None:
                    self.store.add_tool_life(self.id, ts, {"grp": grp, "count_type": "minutes", "life": 0,
                                                           "count": 0, "ntool": 1, "nfree": 0, "use_tool": 0,
                                                           "tools": [{"num": -grp, "status": 0}]})
                    log.info("[%s] T%d: no life time on the control any more - not monitored", self.id, -grp)

    def track_alarms(self, alarms, ts: float):
        """Diff the active alarm list against what is open: open new rows, close cleared ones."""
        if alarms is None:
            return
        now = {}
        for a in alarms:
            k = (str(a.get("code") or "").strip(), str(a.get("text") or "").strip())
            if k != ("", ""):
                now[k] = True
        for k in [k for k in self._alarm_open if k not in now]:
            self.store.alarm_close(self._alarm_open.pop(k), ts)
        for k in now:
            if k not in self._alarm_open:
                self._alarm_open[k] = self.store.alarm_open(self.id, k[0], k[1], ts)

    def close_alarms(self, ts: float):
        for rid in self._alarm_open.values():
            self.store.alarm_close(rid, ts)
        self._alarm_open.clear()

    def poll_alarms(self, ts: float):
        if not self.read_alarms or ts < self._alarm_next:
            return
        self._alarm_next = ts + self.alarm_poll
        try:
            res = self.driver.read_alarms()
        except Exception as e:      # never let alarm reading disturb the normal status polling
            if not self._alarm_err_logged:
                log.info("[%s] reading alarms failed: %s", self.id, e)
                self._alarm_err_logged = True
            return
        if res is not None:
            self._alarm_ok = True
            self.track_alarms(res, ts)
        self.store.set_alarm_supported(self.id, self._alarm_ok)

    def backfill(self, hours: float):
        """Simulator only: generate history so the dashboard has something to show."""
        t = time.time() - hours * 3600
        step = max(self.poll, 2.0)
        self.driver.connect()
        while t < time.time():
            self.handle(self.driver.sample_at(t), live=False)
            self.track_alarms(self.driver.read_alarms(), t)
            t += step

    # --------------------------------------------------- restart of the monitor: hand the open part over
    def take_over(self, h) -> None:
        """Continue the part the previous run of the monitor had open (see CycleEngine.take_over)."""
        if not h:
            return
        prog = (h.get("cycle") or {}).get("program")
        lim = None
        try:
            t = self.store.typical(self.id, prog) if prog else {}
            if t.get("pitch_s"):
                lim = 0.9 * t["pitch_s"]          # the next part cannot have started before this
            elif t.get("typical_s"):
                lim = 1.1 * t["typical_s"]
        except Exception:
            pass
        self.engine.take_over(h, lim)
        log.info("[%s] open part of %s taken over from before the restart", self.id, prog)

    # ----------------------------------------------------------------- loop
    def run(self):
        backoff = 2.0
        while not self.stop_evt.is_set():
            t0 = time.time()
            if not self.connected:
                try:
                    self.driver.connect()
                    self.connected = True
                    self.last_error = None
                    backoff = 2.0
                    log.info("[%s] connected", self.id)
                    self.read_tool_life(None, time.time())     # current state of all tool groups
                except Exception as e:
                    self.last_error = f"connect: {e}"
                    log.warning("[%s] connect failed: %s", self.id, e)
                    try:
                        self.driver.close()       # release whatever the failed attempt left behind
                    except Exception:
                        pass
                    self.handle(Sample(ts=time.time(), online=False, exec_state=ExecState.OFFLINE))
                    self.close_alarms(time.time())
                    self.stop_evt.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
            try:
                s = self.driver.read()
                self.last_error = None
            except Exception as e:
                self.last_error = f"read: {e}"
                log.warning("[%s] read failed: %s", self.id, e)
                self.connected = False
                try:
                    self.driver.close()
                except Exception:
                    pass
                s = Sample(ts=time.time(), online=False, exec_state=ExecState.OFFLINE)
            try:
                self.handle(s)
            except Exception:
                log.exception("[%s] engine/store error", self.id)
            try:
                if s.online and self.connected:
                    self.poll_alarms(s.ts)
                else:
                    self.close_alarms(s.ts)
            except Exception:
                log.exception("[%s] alarm store error", self.id)
            self.stop_evt.wait(max(0.05, self.poll - (time.time() - t0)))
        self.close_alarms(time.time())
        self.driver.close()


# ------------------------------------------------------- open parts across a restart of the monitor
HANDOVER_FILE = "handover.json"


def save_handover(workers, data_dir: str) -> int:
    """Clean stop: write the open part of every machine, for the next run to continue. Returns the count."""
    import json
    import os
    out = {}
    for w in workers:
        try:
            h = w.engine.export_open()
        except Exception:
            h = None
        if h:
            out[w.id] = h
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, HANDOVER_FILE)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(path + ".tmp", path)
    return len(out)


def load_handover(data_dir: str) -> dict:
    """Open parts saved by the previous run ({machine id: ...}); the file is removed so it is used only once."""
    import json
    import os
    path = os.path.join(data_dir, HANDOVER_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    try:
        os.remove(path)
    except OSError:
        pass
    return data if isinstance(data, dict) else {}

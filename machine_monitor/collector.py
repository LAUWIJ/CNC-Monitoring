"""One polling thread per machine: driver -> engine -> store."""
from __future__ import annotations

import logging
import threading
import time

from .drivers import make_driver
from .engine import Cycle, CycleEngine, EngineConfig, StateChange
from .model import ExecState, Sample
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
        has_sig = store.register_machine(self.id, mcfg.get("name", self.id), mcfg.get("control", "?"))
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

    # ------------------------------------------------------------- handling
    def handle(self, s: Sample, live: bool = True):
        for ev in self.engine.update(s):
            if isinstance(ev, StateChange):
                self.store.add_state(self.id, ev.state.value, ev.start_ts, ev.program)
            elif isinstance(ev, Cycle):
                self.store.add_cycle(ev)
                if live:
                    log.info("[%s] cycle %s %s %.0fs (%s)", self.id, ev.program, ev.result, ev.duration_s, ev.end_reason)
        if live and (s.ts - self._last_live >= 2.0 or self.engine.state_since >= self._last_live):
            snap = self.engine.snapshot()
            snap["error"] = self.last_error
            self.store.update_live(self.id, snap, self.engine.has_completion_signal)
            self._last_live = s.ts

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

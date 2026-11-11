"""SQLite storage: state timeline, closed cycles and live machine status."""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id TEXT PRIMARY KEY,
    name TEXT,
    control TEXT,
    live_json TEXT,
    has_completion_signal INTEGER DEFAULT 0,
    updated_ts REAL
);
CREATE TABLE IF NOT EXISTS state_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    state TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL,
    program TEXT
);
CREATE INDEX IF NOT EXISTS ix_seg ON state_segments(machine_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_seg_end ON state_segments(machine_id, end_ts);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    program TEXT,
    program_path TEXT,
    program_source TEXT,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    duration_s REAL,
    run_s REAL,
    pause_s REAL,
    alarm_s REAL,
    result TEXT,
    end_reason TEXT,
    parts INTEGER,
    part_count_end INTEGER,
    subprograms TEXT,
    mode TEXT
);
CREATE INDEX IF NOT EXISTS ix_cyc ON cycles(machine_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_cyc_prog ON cycles(program, start_ts);
CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    code TEXT,
    text TEXT,
    start_ts REAL NOT NULL,
    end_ts REAL
);
CREATE INDEX IF NOT EXISTS ix_alm ON alarms(machine_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_alm_end ON alarms(machine_id, end_ts);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id TEXT NOT NULL,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,      -- 'send' (to the control) or 'fetch' (from the control)
    path TEXT NOT NULL,
    size INTEGER,
    result TEXT NOT NULL,         -- 'ok' / 'error' / 'refused'
    detail TEXT,
    backup TEXT,                  -- file kept on the monitor before a program was replaced
    client TEXT                   -- address of the browser that asked
);
CREATE INDEX IF NOT EXISTS ix_trf ON transfers(machine_id, ts);
-- daily summary (local calendar days) for month/year views - derived data, can always be rebuilt
CREATE TABLE IF NOT EXISTS daily_state (
    machine_id TEXT NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL, secs REAL NOT NULL,
    PRIMARY KEY (machine_id, day, state)
);
CREATE TABLE IF NOT EXISTS daily_prog (
    machine_id TEXT NOT NULL, day TEXT NOT NULL, program TEXT NOT NULL,
    cycles INTEGER, completed INTEGER, aborted INTEGER, parts INTEGER, last_end REAL,
    PRIMARY KEY (machine_id, day, program)
);
CREATE TABLE IF NOT EXISTS daily_done (day TEXT PRIMARY KEY, built_ts REAL);
CREATE INDEX IF NOT EXISTS ix_cyc_end ON cycles(end_ts);
"""

STATES = ["RUNNING", "SETUP", "PAUSED", "WAITING", "WAIT_ROBOT", "IDLE", "ALARM", "OFFLINE"]


class Store:
    def __init__(self, path: str):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self._live: dict = {}          # machine id -> (live dict, has_completion_signal, ts)
        self._live_written: dict = {}
        self._alarm_supported: dict = {}   # machine id -> True once the driver has read alarms
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            # columns added later - upgrade existing databases in place
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(cycles)")}
            for col, typ in (("wait_s", "REAL DEFAULT 0"), ("sub_times", "TEXT")):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE cycles ADD COLUMN {col} {typ}")
            # v1: the first PLC-message version read unverified marker addresses - remove what it recorded
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
                self.db.execute("DELETE FROM alarms WHERE code LIKE 'PLC %'")
                self.db.execute("PRAGMA user_version = 1")
            # v2: Siemens alarms were stored as code 'SystemCondition' + '700209 | text' - split the number out
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
                from .drivers.siemens_opcua import split_alarm_text
                for rid, text in self.db.execute(
                        "SELECT id, text FROM alarms WHERE code='SystemCondition'").fetchall():
                    code, rest = split_alarm_text(text)
                    if code:
                        self.db.execute("UPDATE alarms SET code=?, text=? WHERE id=?", (code, rest, rid))
                self.db.execute("PRAGMA user_version = 2")
            # v3: Heidenhain programs that ended STARTED -> IDLE (FINISHED too short to see) were closed by the
            # RESET fallback and classed ABORTED. A real abort shows CANCELLED there, so these were normal ends.
            # Only cycles without pause time (a stopped-then-deselected program stays ABORTED).
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
                self.db.execute(
                    "UPDATE cycles SET result='COMPLETED', parts=MAX(parts, 1) "
                    "WHERE result='ABORTED' AND end_reason='RESET' AND COALESCE(pause_s, 0) < 1 "
                    "AND machine_id IN (SELECT id FROM machines WHERE control='heidenhain')")
                self.db.execute("PRAGMA user_version = 3")
            self.db.commit()

    # ------------------------------------------------------------------ writes
    def register_machine(self, mid: str, name: str, control: str) -> bool:
        """Returns the stored has_completion_signal flag."""
        with self.lock:
            self.db.execute(
                "INSERT INTO machines(id,name,control,updated_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, control=excluded.control",
                (mid, name, control, time.time()))
            # a service restart leaves open segments behind - close them at the last update
            row = self.db.execute("SELECT updated_ts, has_completion_signal FROM machines WHERE id=?", (mid,)).fetchone()
            self.db.execute("UPDATE state_segments SET end_ts=? WHERE machine_id=? AND end_ts IS NULL",
                            (row["updated_ts"], mid))
            self.db.execute("UPDATE alarms SET end_ts=MAX(start_ts, ?) WHERE machine_id=? AND end_ts IS NULL",
                            (row["updated_ts"], mid))
            self.db.commit()
            return bool(row["has_completion_signal"])

    # ------------------------------------------------------------------ alarms (display only)
    def alarm_open(self, mid: str, code: str, text: str, ts: float) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO alarms(machine_id,code,text,start_ts) VALUES(?,?,?,?)",
                                  (mid, code, text, ts))
            self.db.commit()
            return cur.lastrowid

    def alarm_close(self, alarm_id: int, ts: float) -> None:
        with self.lock:
            self.db.execute("UPDATE alarms SET end_ts=? WHERE id=? AND end_ts IS NULL", (ts, alarm_id))
            self.db.commit()

    def set_alarm_supported(self, mid: str, supported: bool) -> None:
        self._alarm_supported[mid] = supported

    def add_transfer(self, mid: str, direction: str, path: str, size, result: str, detail: str = "",
                     backup: str = "", client: str = "") -> None:
        with self.lock:
            self.db.execute("INSERT INTO transfers(machine_id,ts,direction,path,size,result,detail,backup,client) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (mid, time.time(), direction, path, size, result, detail, backup, client))
            self.db.commit()

    def transfers(self, mid: str, limit: int = 50) -> list:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT ts,direction,path,size,result,detail,backup FROM transfers WHERE machine_id=? "
                "ORDER BY ts DESC LIMIT ?", (mid, limit))]

    def alarms(self, mid: str, t0: float, t1: float, limit: int = 50) -> dict:
        """Active alarms, a per-alarm summary for the period and the most recent occurrences."""
        now = time.time()
        with self.lock:
            active = [dict(r) for r in self.db.execute(
                "SELECT code,text,start_ts FROM alarms WHERE machine_id=? AND end_ts IS NULL ORDER BY start_ts DESC",
                (mid,))]
            rows = [dict(r) for r in self.db.execute(
                "SELECT code,text,start_ts,end_ts FROM alarms WHERE machine_id=? AND start_ts<? "
                "AND (end_ts IS NULL OR end_ts>?) ORDER BY start_ts DESC", (mid, t1, t0))]
        summary = {}
        for r in rows:
            a, b = max(r["start_ts"], t0), min(r["end_ts"] if r["end_ts"] is not None else now, t1)
            k = (r["code"] or "", r["text"] or "")
            s = summary.setdefault(k, {"code": k[0], "text": k[1], "count": 0, "total_s": 0.0, "last_ts": 0})
            s["count"] += 1
            s["total_s"] += max(0.0, b - a)
            s["last_ts"] = max(s["last_ts"], r["start_ts"])
        for r in rows:
            r["duration_s"] = (r["end_ts"] if r["end_ts"] is not None else now) - r["start_ts"]
        return {"supported": self._alarm_supported.get(mid),
                "active": active,
                "summary": sorted(summary.values(), key=lambda s: (-s["count"], -s["total_s"])),
                "recent": rows[:limit],
                "total": len(rows)}

    def add_state(self, mid: str, state: str, start_ts: float, program: Optional[str]):
        with self.lock:
            self.db.execute("UPDATE state_segments SET end_ts=? WHERE machine_id=? AND end_ts IS NULL",
                            (start_ts, mid))
            self.db.execute("INSERT INTO state_segments(machine_id,state,start_ts,program) VALUES(?,?,?,?)",
                            (mid, state, start_ts, program))
            self.db.commit()

    def add_cycle(self, c) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO cycles(machine_id,program,program_path,program_source,start_ts,end_ts,duration_s,"
                "run_s,pause_s,alarm_s,result,end_reason,parts,part_count_end,subprograms,mode,wait_s,sub_times) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c.machine_id, c.program, c.program_path, c.program_source, c.start_ts, c.end_ts,
                 c.duration_s, c.run_s, c.pause_s, c.alarm_s, c.result, c.end_reason, c.parts,
                 c.part_count_end, json.dumps(c.subprograms), c.mode, c.wait_s,
                 json.dumps({k: round(v, 1) for k, v in c.sub_times.items()})))
            self.db.commit()

    LIVE_PERSIST_S = 60

    def update_live(self, mid: str, live: dict, has_completion_signal: bool):
        """Live status is kept in memory (dashboard reads it from here). It is written to the database
        only once a minute, or when the learned completion flag changes - this saves the SD card/drive
        ~40 000 writes per machine per day. The stored copy is only used after a restart."""
        now = time.time()
        with self.lock:
            prev = self._live.get(mid)
            self._live[mid] = (live, has_completion_signal, now)
            last = self._live_written.get(mid, 0.0)
            if now - last >= self.LIVE_PERSIST_S or (prev and prev[1] != has_completion_signal):
                self.db.execute("UPDATE machines SET live_json=?, has_completion_signal=?, updated_ts=? WHERE id=?",
                                (json.dumps(live, default=str), int(has_completion_signal), now, mid))
                self.db.commit()
                self._live_written[mid] = now

    # ------------------------------------------------------------------- reads
    def typical(self, mid: str, program: str) -> dict:
        """Usual cycle time of a program on a machine (median of the last 10 completed cycles) and the end
        of the last completed part. Cached for a minute - used for the progress line on the machine card."""
        if not program:
            return {}
        key, now = (mid, program), time.time()
        cache = self.__dict__.setdefault("_typical", {})
        hit = cache.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
        with self.lock:
            durs = [r[0] for r in self.db.execute(
                "SELECT duration_s FROM cycles WHERE machine_id=? AND program=? AND result='COMPLETED' "
                "ORDER BY start_ts DESC LIMIT 10", (mid, program)).fetchall()]
            last = self.db.execute("SELECT MAX(end_ts) FROM cycles WHERE machine_id=? AND result IN "
                                   "('COMPLETED','PARTIAL')", (mid,)).fetchone()[0]
        res = {"typical_s": statistics.median(durs) if durs else None, "last_part_ts": last}
        cache[key] = (now, res)
        return res

    def machines(self) -> list:
        with self.lock:
            rows = self.db.execute("SELECT * FROM machines ORDER BY id").fetchall()
            mem = dict(self._live)
        out = []
        for r in rows:
            if r["id"] in mem:
                live, _, ts = mem[r["id"]]
            else:
                live, ts = (json.loads(r["live_json"]) if r["live_json"] else {}), r["updated_ts"]
            out.append({"id": r["id"], "name": r["name"], "control": r["control"],
                        "updated_ts": ts, "live": live})
        return out

    def segments(self, t0: float, t1: float, mid: Optional[str] = None) -> list:
        now = time.time()
        q = ("SELECT machine_id,state,start_ts,end_ts,program FROM state_segments "
             "WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?)")
        args = [t1, t0]
        if mid:
            q += " AND machine_id=?"
            args.append(mid)
        q += " ORDER BY machine_id, start_ts"
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            s = max(r["start_ts"], t0)
            e = min(r["end_ts"] if r["end_ts"] is not None else now, t1, now)
            if e > s:
                out.append({"machine_id": r["machine_id"], "state": r["state"], "start": s, "end": e,
                            "program": r["program"], "open": r["end_ts"] is None})
        return out

    def utilization(self, t0: float, t1: float) -> dict:
        res: dict = {}
        for seg in self.segments(t0, t1):
            m = res.setdefault(seg["machine_id"], {s: 0.0 for s in STATES})
            m[seg["state"]] = m.get(seg["state"], 0.0) + seg["end"] - seg["start"]
        return res

    def cycles(self, t0: float, t1: float, mid: Optional[str] = None, program: Optional[str] = None,
               limit: int = 500) -> list:
        q = "SELECT * FROM cycles WHERE end_ts > ? AND start_ts < ?"
        args: list = [t0, t1]
        if mid:
            q += " AND machine_id=?"
            args.append(mid)
        if program:
            q += " AND program=?"
            args.append(program)
        q += " ORDER BY start_ts DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["subprograms"] = json.loads(d["subprograms"] or "[]")
            d["sub_times"] = json.loads(d.get("sub_times") or "{}")
            d["wait_s"] = d.get("wait_s") or 0.0
            out.append(d)
        return out

    def search_programs(self, text: str, mid: Optional[str] = None, limit: int = 200) -> list:
        """Search program names over the WHOLE history (no date limit), case-insensitive,
        partial match on name or full path. '*' works as wildcard."""
        text = (text or "").strip()
        if not text:
            return []
        pat = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
        like = f"%{pat}%"
        q = ("SELECT machine_id, program, COUNT(*) AS cycles, "
             "SUM(result='COMPLETED') AS completed, SUM(result='ABORTED') AS aborted, "
             "SUM(CASE WHEN result IN ('COMPLETED','PARTIAL') THEN parts ELSE 0 END) AS parts, "
             "MIN(start_ts) AS first_ts, MAX(end_ts) AS last_ts, "
             "MIN(CASE WHEN result='COMPLETED' THEN duration_s END) AS min_s, "
             "MAX(CASE WHEN result='COMPLETED' THEN duration_s END) AS max_s "
             "FROM cycles WHERE (program LIKE ? ESCAPE '\\' OR program_path LIKE ? ESCAPE '\\')")
        args: list = [like, like]
        if mid:
            q += " AND machine_id=?"
            args.append(mid)
        q += " GROUP BY machine_id, program ORDER BY last_ts DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = [dict(r) for r in self.db.execute(q, args).fetchall()]
            for r in rows:
                durs = [x[0] for x in self.db.execute(
                    "SELECT duration_s FROM cycles WHERE machine_id=? AND program=? AND result='COMPLETED' "
                    "ORDER BY start_ts DESC", (r["machine_id"], r["program"])).fetchall()]
                r["median_s"] = statistics.median(durs) if durs else None
                # median of the last 10 runs = what the program does now (after optimising)
                r["recent_median_s"] = statistics.median(durs[:10]) if durs else None
        return rows

    def merge_split_cycles(self, max_fragment_s: float = 15.0, dry_run: bool = False) -> list:
        """Repair history written before the 'counter before program end' fix: a part that was split
        into [cycle ended by PART_COUNTER] + [short fragment starting right after it] becomes one cycle."""
        merged = []
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM cycles ORDER BY machine_id, start_ts")]
            for a, b in zip(rows, rows[1:]):
                if (a["machine_id"] == b["machine_id"] and a["program"] == b["program"]
                        and a["end_reason"] == "PART_COUNTER" and b["end_reason"] in ("PROGRAM_END", "RESET", "FINISHED")
                        and 0 <= b["start_ts"] - a["end_ts"] < 1.5
                        and (b["end_ts"] - b["start_ts"]) <= max_fragment_s and not a.get("_gone")):
                    merged.append((a["machine_id"], a["program"], a["start_ts"], b["end_ts"] - b["start_ts"]))
                    b["_gone"] = True
                    if dry_run:
                        continue
                    self.db.execute(
                        "UPDATE cycles SET end_ts=?, duration_s=duration_s+?, run_s=run_s+?, pause_s=pause_s+?, "
                        "alarm_s=alarm_s+? WHERE id=?",
                        (b["end_ts"], b["end_ts"] - a["end_ts"] - (b.get("wait_s") or 0), b["run_s"], b["pause_s"],
                         b["alarm_s"], a["id"]))
                    self.db.execute("DELETE FROM cycles WHERE id=?", (b["id"],))
            if not dry_run:
                self.db.commit()
        if merged and not dry_run:
            self.invalidate_daily(min(m[2] for m in merged))
        return merged

    def mark_partial_cycles(self, dry_run: bool = False) -> list:
        """Repair history from before 'partial' existed: a completed cycle whose start directly follows a
        gap in the timeline (monitor not running) or an OFFLINE period only saw the tail of the part."""
        marked = []
        with self.lock:
            for c in [dict(r) for r in self.db.execute("SELECT id, machine_id, program, start_ts FROM cycles "
                                                       "WHERE result='COMPLETED'")]:
                before = self.db.execute(
                    "SELECT state FROM state_segments WHERE machine_id=? AND start_ts < ? AND "
                    "(end_ts IS NULL OR end_ts > ?) ORDER BY start_ts DESC LIMIT 1",
                    (c["machine_id"], c["start_ts"] - 2, c["start_ts"] - 3)).fetchone()
                prev = self.db.execute(
                    "SELECT end_reason, end_ts FROM cycles WHERE machine_id=? AND end_ts <= ? "
                    "ORDER BY end_ts DESC LIMIT 1", (c["machine_id"], c["start_ts"] + 1)).fetchone()
                after_lost = bool(prev and prev["end_reason"] == "CONNECTION_LOST")
                if before is None or before["state"] == "OFFLINE" or after_lost:
                    marked.append((c["machine_id"], c["program"], c["start_ts"]))
                    if not dry_run:
                        self.db.execute("UPDATE cycles SET result='PARTIAL' WHERE id=?", (c["id"],))
            if not dry_run:
                self.db.commit()
        if marked and not dry_run:
            self.invalidate_daily(min(m[2] for m in marked))
        return marked

    # ------------------------------------------------------------------ daily summary
    # One row per machine/local day/state (seconds) and per machine/day/program (counts). Built once a day
    # is complete; month/year views add the partial first/last day live. A cycle belongs to the day it ENDS.
    DAY_MARGIN_S = 900          # a day is summarised 15 min after midnight (late writes have landed)

    @staticmethod
    def day_start(t: float) -> float:
        lt = time.localtime(t)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    @staticmethod
    def next_day(t: float) -> float:
        """Local midnight after the day that starts at t (DST-safe)."""
        lt = time.localtime(t + 36 * 3600)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    @staticmethod
    def day_key(t: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(t))

    def _lean_cycles(self, t0: float, t1: float, mid: Optional[str] = None) -> list:
        q = ("SELECT machine_id,program,result,duration_s,run_s,parts,end_ts FROM cycles "
             "WHERE end_ts >= ? AND end_ts < ?")
        args: list = [t0, t1]
        if mid:
            q += " AND machine_id=?"
            args.append(mid)
        with self.lock:
            return self.db.execute(q, args).fetchall()

    def _counts(self, rows) -> dict:
        out: dict = {}
        for r in rows:
            g = out.setdefault((r["machine_id"], r["program"] or "?"),
                               {"cycles": 0, "completed": 0, "aborted": 0, "parts": 0, "last_end": 0.0})
            g["cycles"] += 1
            g["last_end"] = max(g["last_end"], r["end_ts"])
            if r["result"] == "COMPLETED":
                g["completed"] += 1
                g["parts"] += r["parts"] or 0
            elif r["result"] == "PARTIAL":
                g["parts"] += r["parts"] or 0
            elif r["result"] == "ABORTED":
                g["aborted"] += 1
        return out

    def build_day(self, start: float) -> None:
        end = self.next_day(start)
        key = self.day_key(start)
        util = self.utilization(start, end)
        counts = self._counts(self._lean_cycles(start, end))
        with self.lock:
            self.db.execute("DELETE FROM daily_state WHERE day=?", (key,))
            self.db.execute("DELETE FROM daily_prog WHERE day=?", (key,))
            self.db.executemany("INSERT INTO daily_state VALUES (?,?,?,?)",
                                [(m, key, st, sec) for m, u in util.items() for st, sec in u.items() if sec > 0])
            self.db.executemany("INSERT INTO daily_prog VALUES (?,?,?,?,?,?,?,?)",
                                [(m, key, p, g["cycles"], g["completed"], g["aborted"], g["parts"], g["last_end"])
                                 for (m, p), g in counts.items()])
            self.db.execute("INSERT OR REPLACE INTO daily_done VALUES (?,?)", (key, time.time()))
            self.db.commit()

    def refresh_daily(self, max_days: int = 100000) -> int:
        """Summarise every complete day that isn't summarised yet. Returns the number of days built."""
        with self.lock:
            first = self.db.execute("SELECT MIN(start_ts) FROM state_segments").fetchone()[0]
            done = {r[0] for r in self.db.execute("SELECT day FROM daily_done")}
        if first is None:
            return 0
        n, d, limit = 0, self.day_start(first), time.time() - self.DAY_MARGIN_S
        while n < max_days:
            e = self.next_day(d)
            if e > limit:
                break
            if self.day_key(d) not in done:
                self.build_day(d)
                n += 1
            d = e
        return n

    def invalidate_daily(self, since: Optional[float] = None) -> None:
        """History changed (repair, migration): drop the summary (from `since`) so it is rebuilt."""
        with self.lock:
            if since is None:
                for t in ("daily_state", "daily_prog", "daily_done"):
                    self.db.execute(f"DELETE FROM {t}")
            else:
                k = self.day_key(since)
                for t in ("daily_state", "daily_prog", "daily_done"):
                    self.db.execute(f"DELETE FROM {t} WHERE day >= ?", (k,))
            self.db.commit()

    def first_ts(self) -> Optional[float]:
        """Start of the recorded history (cached - it only moves when the database is replaced)."""
        if getattr(self, "_first_ts", None) is None:
            with self.lock:
                self._first_ts = self.db.execute("SELECT MIN(start_ts) FROM state_segments").fetchone()[0]
        return self._first_ts

    def _pieces(self, t0: float, t1: float):
        """Split [t0, t1) into local days: (start, end, key, whole_day_summarised). Days before the first
        recorded data are skipped (nothing to show, and nothing to calculate)."""
        first = self.first_ts()
        if first is None:
            return
        t0 = max(t0, self.day_start(first))
        with self.lock:
            done = {r[0] for r in self.db.execute("SELECT day FROM daily_done WHERE day >= ? AND day <= ?",
                                                  (self.day_key(t0), self.day_key(t1)))}
        d = self.day_start(t0)
        while d < t1:
            e = self.next_day(d)
            a, b = max(d, t0), min(e, t1)
            key = self.day_key(d)
            yield a, b, key, (a == d and b == e and key in done)
            d = e

    def daily(self, t0: float, t1: float, mid: Optional[str] = None) -> dict:
        """{machine: {day: {"states": {state: s}, "parts": n, "completed": n, "aborted": n}}} - summary rows
        for whole summarised days, live calculation for the rest (today, the cut-off first day)."""
        out: dict = {}

        def slot(m, k):
            return out.setdefault(m, {}).setdefault(k, {"states": {}, "parts": 0, "completed": 0, "aborted": 0})
        live = []
        keys = []
        for a, b, k, whole in self._pieces(t0, t1):
            (keys if whole else live).append((a, b, k))
        if keys:
            lo, hi = keys[0][2], keys[-1][2]
            mq, ma = ("", []) if not mid else (" AND machine_id=?", [mid])
            ks = {k for _, _, k in keys}
            with self.lock:
                srows = self.db.execute("SELECT machine_id,day,state,secs FROM daily_state WHERE day>=? AND day<=?" + mq,
                                        [lo, hi] + ma).fetchall()
                prows = self.db.execute("SELECT machine_id,day,SUM(parts),SUM(completed),SUM(aborted) FROM daily_prog "
                                        "WHERE day>=? AND day<=?" + mq + " GROUP BY machine_id,day", [lo, hi] + ma).fetchall()
            for m, k, st, sec in srows:
                if k in ks:
                    slot(m, k)["states"][st] = sec
            for m, k, p, c, ab in prows:
                if k in ks:
                    x = slot(m, k)
                    x["parts"], x["completed"], x["aborted"] = p or 0, c or 0, ab or 0
        for a, b, k in live:
            for seg in self.segments(a, b, mid):
                st = slot(seg["machine_id"], k)["states"]
                st[seg["state"]] = st.get(seg["state"], 0.0) + seg["end"] - seg["start"]
            for (m, _), g in self._counts(self._lean_cycles(a, b, mid)).items():
                x = slot(m, k)
                x["parts"] += g["parts"]
                x["completed"] += g["completed"]
                x["aborted"] += g["aborted"]
        return out

    def summary(self, t0: float, t1: float) -> dict:
        """Per machine: time per state + part counts over [t0, t1), using the daily summary."""
        res: dict = {}
        for m, days in self.daily(t0, t1).items():
            r = res.setdefault(m, {"utilization": {s: 0.0 for s in STATES}, "parts": 0, "completed": 0, "aborted": 0})
            for d in days.values():
                for st, sec in d["states"].items():
                    r["utilization"][st] = r["utilization"].get(st, 0.0) + sec
                r["parts"] += d["parts"]
                r["completed"] += d["completed"]
                r["aborted"] += d["aborted"]
        return res

    def program_stats(self, t0: float, t1: float, mid: Optional[str] = None) -> list:
        groups: dict = {}
        for c in self._lean_cycles(t0, t1, mid):
            g = groups.setdefault((c["machine_id"], c["program"]), {"all": 0, "aborted": 0, "durs": [], "runs": [],
                                                                    "parts": 0, "last": 0})
            g["all"] += 1
            g["last"] = max(g["last"], c["end_ts"])
            if c["result"] == "COMPLETED":
                g["durs"].append(c["duration_s"])
                g["runs"].append(c["run_s"])
                g["parts"] += c["parts"] or 0
            elif c["result"] == "PARTIAL":          # a real part, but its cycle time is incomplete
                g["parts"] += c["parts"] or 0
            elif c["result"] == "ABORTED":
                g["aborted"] += 1
        out = []
        for (m, p), g in groups.items():
            d = g["durs"]
            out.append({
                "machine_id": m, "program": p, "cycles": g["all"], "completed": len(d),
                "aborted": g["aborted"], "parts": g["parts"], "last_end": g["last"],
                "median_s": statistics.median(d) if d else None,
                "min_s": min(d) if d else None, "max_s": max(d) if d else None,
                "avg_run_s": (sum(g["runs"]) / len(g["runs"])) if g["runs"] else None,
            })
        out.sort(key=lambda x: -x["last_end"])
        return out

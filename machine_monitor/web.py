"""Dashboard web server (standard library only - no internet/CDN needed on the shop floor)."""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import Store

STATIC = os.path.join(os.path.dirname(__file__), "static")


_CACHE: dict = {}


def cached(key, fn, span: float):
    """Periods longer than 2 days: reuse a result for up to 60 s (the end of the period is rounded to the
    minute so the 3-second refreshes hit the same entry). Short periods are always calculated fresh."""
    if span <= 2 * 86400:
        return fn()
    key = tuple(round(k / 60) if isinstance(k, float) else k for k in key)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < 60:
        return hit[1]
    val = fn()
    if len(_CACHE) > 50:
        _CACHE.clear()
    _CACHE[key] = (now, val)
    return val


def make_handler(store: Store, plc_jobs=None, transfers=None, about=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(200, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            now = time.time()
            t1 = float(q.get("to", now))
            t0 = float(q.get("from", t1 - 24 * 3600))
            mid = q.get("machine") or None
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/about":
                    return self._json(about)
                if u.path == "/api/overview":
                    # long periods come from the daily summary; cached briefly (the page asks every 3 s)
                    summ = cached(("summary", t0, t1), lambda: store.summary(t0, t1), t1 - t0)
                    out = []
                    for m in store.machines():
                        sm = summ.get(m["id"], {})
                        m["utilization"] = sm.get("utilization", {})
                        m["parts"] = sm.get("parts", 0)
                        m["completed"] = sm.get("completed", 0)
                        m["aborted"] = sm.get("aborted", 0)
                        m["stale"] = (now - (m["updated_ts"] or 0)) > 30
                        m.update(store.typical(m["id"], (m.get("live") or {}).get("program")))
                        live = m.get("live") or {}
                        m.update(cached(("toolstat", m["id"], live.get("program"), live.get("tool_group"), live.get("tool")),
                                        lambda: store.tool_status(m["id"], live.get("program"), live), 1e9))
                        out.append(m)
                    return self._json({"now": now, "from": t0, "to": t1, "machines": out})
                if u.path == "/api/program-tools":
                    if not mid or not q.get("program"):
                        return self._send(400, b'{"error": "machine and program required"}', "application/json")
                    return self._json({"tools": store.program_tools(mid, q["program"]),
                                       "groups": list(store.tool_life_now(mid).values())})
                if u.path == "/api/daily":
                    return self._json(cached(("daily", t0, t1, mid), lambda: store.daily(t0, t1, mid), t1 - t0))
                if u.path == "/api/timeline":
                    return self._json(store.segments(t0, t1, mid))
                if u.path == "/api/cycles":
                    return self._json(store.cycles(t0, t1, mid, q.get("program"), int(q.get("limit", 300))))
                if u.path == "/api/search":
                    return self._json(store.search_programs(q.get("q", ""), mid))
                if u.path == "/api/plc-fetch":
                    if not plc_jobs or not mid:
                        return self._json({"supported": False})
                    return self._json(plc_jobs.status(mid))
                if u.path.startswith("/api/transfer"):
                    return self._transfer_get(u.path, q, mid)
                if u.path == "/api/alarms":
                    if not mid:
                        return self._send(400, b'{"error": "machine required"}', "application/json")
                    return self._json(store.alarms(mid, t0, t1, int(q.get("limit", 50))))
                if u.path == "/api/programs":
                    return self._json(cached(("programs", t0, t1, mid), lambda: store.program_stats(t0, t1, mid), t1 - t0))
                if u.path == "/api/cycles.csv":
                    rows = store.cycles(t0, t1, mid, q.get("program"), 100000)
                    buf = io.StringIO()
                    w = csv.writer(buf, delimiter=";")
                    cols = ["machine_id", "program", "start", "end", "cycle_time_s", "run_s", "pause_s",
                            "alarm_s", "wait_s", "result", "end_reason", "parts", "program_source", "subprograms"]
                    w.writerow(cols)
                    for r in rows:
                        w.writerow([r["machine_id"], r["program"],
                                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["start_ts"])),
                                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["end_ts"])),
                                    round(r["duration_s"], 1), round(r["run_s"], 1), round(r["pause_s"], 1),
                                    round(r["alarm_s"], 1), round(r["wait_s"] or 0, 1), r["result"], r["end_reason"],
                                    r["parts"], r["program_source"],
                                    " ".join(f"{k}({round(v)}s)" for k, v in (r["sub_times"] or {}).items())
                                    or " ".join(r["subprograms"])])
                    return self._send(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                                      {"Content-Disposition": "attachment; filename=cycles.csv"})
                self._send(404, b"not found", "text/plain")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

        # ---------------------------------------------------------------- program transfer
        def _transfer_get(self, path, q, mid):
            if not transfers:
                return self._json({"supported": False})
            if path == "/api/transfer":
                return self._json(transfers.info(mid))
            if path == "/api/transfer/roots":
                return self._json(transfers.roots(mid))
            if path == "/api/transfer/list":
                return self._json(transfers.list(mid, q.get("path", "")))
            if path == "/api/transfer/history":
                return self._json(transfers.history(mid, int(q.get("limit", 30))))
            if path == "/api/transfer/job":
                return self._json(transfers.job(q.get("id", "")))
            if path == "/api/transfer/download":
                got = transfers.job_file(q.get("id", ""))
                if not got:
                    return self._send(404, b"not ready", "text/plain")
                fp, name = got
                size = os.path.getsize(fp)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name) or "program"
                self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
                self.end_headers()
                with open(fp, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 64 * 1024)
                return None
            return self._send(404, b"not found", "text/plain")

        def _transfer_send(self, q):
            """Raw file body (application/octet-stream), streamed to a temp file on the monitor."""
            if not transfers:
                return self._send(400, b'{"error": "not available"}', "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                raise ValueError("empty file")
            if n > transfers.max_bytes:
                raise ValueError(f"file is larger than {transfers.max_bytes // 1048576} MB")
            tmp = transfers.new_upload_path()
            left = n
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 256 * 1024))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                os.remove(tmp)
                raise ValueError("upload was interrupted")
            return self._json(transfers.start_send(
                q.get("machine", ""), q.get("folder", ""), q.get("name", ""), tmp,
                q.get("overwrite") in ("1", "true"), self.headers.get("X-Transfer-Pin", ""),
                self.client_address[0]))

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/transfer/send":
                    return self._transfer_send(q)
                n = min(int(self.headers.get("Content-Length") or 0), 10000)
                body = json.loads(self.rfile.read(n) or b"{}")
                if u.path == "/api/plc-fetch":
                    if not plc_jobs:
                        return self._send(400, b'{"error": "not available"}', "application/json")
                    # the PLC code number is only used for this one login; it is not stored or logged
                    return self._json(plc_jobs.start(str(body.get("machine", "")), str(body.get("password", ""))))
                if u.path == "/api/transfer/fetch":
                    if not transfers:
                        return self._send(400, b'{"error": "not available"}', "application/json")
                    return self._json(transfers.start_fetch(str(body.get("machine", "")), str(body.get("path", "")),
                                                            self.client_address[0]))
                if u.path == "/api/transfer/replace":
                    if not transfers:
                        return self._send(400, b'{"error": "not available"}', "application/json")
                    return self._json(transfers.replace(str(body.get("id", "")),
                                                        self.headers.get("X-Transfer-Pin", ""), self.client_address[0]))
                self._send(404, b"not found", "text/plain")
            except PermissionError as e:
                self._send(403, json.dumps({"error": str(e)}).encode(), "application/json")
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    return H


def serve(store: Store, host: str, port: int, plc_jobs=None, transfers=None, source_url=None) -> ThreadingHTTPServer:
    from . import __version__, LICENSE, SOURCE_URL
    about = {"version": __version__, "license": LICENSE, "source_url": source_url or SOURCE_URL}
    srv = ThreadingHTTPServer((host, port), make_handler(store, plc_jobs, transfers, about))
    srv.daemon_threads = True
    return srv

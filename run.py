"""Start the CNC monitor.

    python run.py                       uses config.toml
    python run.py --config other.toml
    python run.py --demo                simulated machines + 2 days of history (no CNC needed)
"""
import argparse
import logging
import logging.handlers
import os
import sys
import threading
import time
import tomllib

from machine_monitor.collector import MachineWorker
from machine_monitor.store import Store
from machine_monitor.web import serve

HERE = os.path.dirname(os.path.abspath(__file__))


def _card_list(m):
    """Memory card file names via cnc_rdmcdfinfo (verified read-only on the 2600LY)."""
    import ctypes as C
    from machine_monitor.drivers import fanuc_focas as F

    class ODBFILESTATUS(C.Structure):
        _fields_ = [("size", C.c_uint32), ("min", C.c_ubyte), ("hour", C.c_ubyte), ("day", C.c_ubyte),
                    ("month", C.c_ubyte), ("year", C.c_ushort), ("reserve", C.c_ubyte * 2),
                    ("filename", C.c_char * 20)]
    drv = F.FanucFocasDriver(dict(m))
    drv.connect()
    L, h, out = drv.lib, drv.h.value, {}
    try:
        L.cnc_rdmcdfinfo.argtypes = [C.c_ushort, C.c_int32, C.POINTER(ODBFILESTATUS)]
        L.cnc_rdmcdfinfo.restype = C.c_short
        for i in range(200):                       # like FANUC's tool: entry 1, then -1 = next, until empty
            st = ODBFILESTATUS()
            if L.cnc_rdmcdfinfo(h, 1 if i == 0 else -1, C.byref(st)) != 0:
                break
            name = st.filename.split(b"\0")[0].decode(errors="replace")
            if not name:
                break
            out[name] = st.size
    finally:
        L.cnc_canmcdfinfo.argtypes = [C.c_ushort]
        L.cnc_canmcdfinfo(h)
        drv.close()
    return out


def fanuc_card_send(cfg, cfg_path, argv):
    """Send a tiny test program to the FANUC memory card (CF_TEXT), read it back and compare.
    Refuses if the name already exists on the card, so nothing is overwritten."""
    from machine_monitor.fanuc_card import CardStream
    mid, name = argv[0], argv[1].upper()
    overwrite = len(argv) > 2 and argv[2].lower() == "overwrite"
    m = next((m for m in cfg.get("machine", []) if m["id"] == mid), None)
    if not m or m.get("control") != "fanuc":
        sys.exit(f"'{mid}' is not a FANUC machine in {cfg_path}")
    m = {**cfg.get("defaults", {}), **m}
    num = "".join(ch for ch in name.split(".")[0] if ch.isdigit()) or "7778"
    prog = f"%\r\nO{num} (TEST MONITOR {'OVERWRITE ' + time.strftime('%H%M%S') if overwrite else 'SEND'})\r\nM30\r\n%\r\n".encode()
    print(f"Send test program {name} to the memory card of {m.get('name', mid)} ({m.get('host')}).")
    print("Machine NOT in a cycle, someone at the control.")
    before = _card_list(m)
    print("card now:", ", ".join(f"{k} ({v} B)" for k, v in before.items()) or "(empty)")
    exists = any(k.upper() == name for k in before)
    if exists and not overwrite:
        sys.exit(f"{name} is already on the card - pick another name (or add 'overwrite' for a test file), nothing was sent")
    if overwrite and not exists:
        sys.exit(f"{name} is not on the card - nothing to overwrite")
    if overwrite:
        print(f"OVERWRITE TEST: {name} is replaced with new content ('10000:' as FANUC's tool does, no delete).")
    if input("Type YES to send: ").strip() != "YES":
        sys.exit("cancelled")
    with CardStream(m["host"], int(m.get("port", 8193))) as cs:
        cs.send(name, prog, overwrite=overwrite)
        print("sent", len(CardStream.prepare(prog)), "bytes")
        back = cs.fetch(name)
    print("read back:", back.decode("ascii", "replace").strip().replace("\r\n", " | "))
    after = _card_list(m)
    print("card now:", ", ".join(f"{k} ({v} B)" for k, v in after.items()))
    ok = back.strip(b"\r\n") == CardStream.prepare(prog).strip(b"\r\n")
    print("MATCH" if ok else "DIFFERENT - send me this output")
    print("done - check the control screen for alarms. Delete the test file at the control if you like.")


def fanuc_card_fetch(cfg, cfg_path, argv):
    """Fetch ONE file from the FANUC memory card (CF_TEXT) with the exact messages FANUC's Program Transfer
    Tool used in the Wireshark recording (machine_monitor/fanuc_card.py). Read only: nothing is written to
    the control. The file is saved in data/card_test/."""
    from pathlib import Path
    from machine_monitor.fanuc_card import CardStream
    mid, name = argv[0], argv[1]
    m = next((m for m in cfg.get("machine", []) if m["id"] == mid), None)
    if not m or m.get("control") != "fanuc":
        sys.exit(f"'{mid}' is not a FANUC machine in {cfg_path}")
    m = {**cfg.get("defaults", {}), **m}
    print(f"Fetch {name} from the memory card of {m.get('name', mid)} ({m.get('host')}), read only.")
    print("Only run this with the machine NOT in a cycle and someone at the control.")
    if input("Type YES to continue: ").strip() != "YES":
        sys.exit("cancelled")
    with CardStream(m["host"], int(m.get("port", 8193))) as cs:
        data = cs.fetch(name)
    out = Path("data/card_test"); out.mkdir(parents=True, exist_ok=True)
    (out / name).write_bytes(data)
    print(f"received {len(data)} bytes -> {out / name}")
    print("first lines:")
    for line in data.decode("ascii", "replace").splitlines()[:6]:
        print("   ", line)
    print("done - check the control screen for alarms.")


def fanuc_card_test(cfg, cfg_path, argv):
    """Read-only test of the FANUC memory card file list (cnc_rdmcdfinfo), entry by entry.
    Background: on 2026-11-11 opening an unreported drive crashed a 0i-D (SYS_ALM198). This test only calls
    cnc_rdmcdfinfo + cnc_canmcdfinfo, stops at the first non-zero answer and prints every raw answer."""
    import ctypes as C
    from machine_monitor.drivers import fanuc_focas as F
    mid, n = argv[0], int(argv[1]) if len(argv) > 1 else 1
    n = max(1, min(n, 200))
    m = next((m for m in cfg.get("machine", []) if m["id"] == mid), None)
    if not m or m.get("control") != "fanuc":
        sys.exit(f"'{mid}' is not a FANUC machine in {cfg_path}")
    m = {**cfg.get("defaults", {}), **m}
    print(f"FANUC memory card test on {m.get('name', mid)} ({m.get('host')}): reads {n} entr{'y' if n == 1 else 'ies'} "
          "of the card's file list with cnc_rdmcdfinfo, nothing else.")
    print("Only run this with the machine NOT in a cycle and someone at the control.")
    if input("Type YES to continue: ").strip() != "YES":
        sys.exit("cancelled")

    class ODBFILESTATUS(C.Structure):          # fwlib32.h: memory card access
        _fields_ = [("size", C.c_uint32), ("min", C.c_ubyte), ("hour", C.c_ubyte), ("day", C.c_ubyte),
                    ("month", C.c_ubyte), ("year", C.c_ushort), ("reserve", C.c_ubyte * 2),
                    ("filename", C.c_char * 20)]
    drv = F.FanucFocasDriver(dict(m))
    drv.connect()
    L, h = drv.lib, drv.h.value
    try:
        if not hasattr(L, "cnc_rdmcdfinfo"):
            sys.exit("cnc_rdmcdfinfo is not in this FOCAS library")
        L.cnc_rdmcdfinfo.argtypes = [C.c_ushort, C.c_int32, C.POINTER(ODBFILESTATUS)]
        L.cnc_rdmcdfinfo.restype = C.c_short
        for i in range(1, n + 1):                  # entry 1, then -1 = next entry (as FANUC's own tool does)
            st = ODBFILESTATUS()
            ret = L.cnc_rdmcdfinfo(h, 1 if i == 1 else -1, C.byref(st))
            name = st.filename.split(b"\0")[0].decode(errors="replace")
            print(f"  entry {i}: ret={ret}  name={name!r}  size={st.size}  "
                  f"date={st.year:04d}-{st.month:02d}-{st.day:02d} {st.hour:02d}:{st.min:02d}")
            if ret != 0 or not name:
                break
    finally:
        if hasattr(L, "cnc_canmcdfinfo"):
            L.cnc_canmcdfinfo.argtypes = [C.c_ushort]
            L.cnc_canmcdfinfo.restype = C.c_short
            print("  cnc_canmcdfinfo ->", L.cnc_canmcdfinfo(h))
        drv.close()
    print("done - check the control screen for alarms.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    ap.add_argument("--demo", action="store_true", help="run config.demo.toml with simulated history")
    ap.add_argument("--backfill-hours", type=float, default=48)
    ap.add_argument("--probe", metavar="MACHINE_ID", help="test the connection to one machine and exit")
    ap.add_argument("--fetch-plc", metavar="MACHINE_ID",
                    help="Heidenhain: copy the PLC text files (messages, symbols) once, read-only, to data/plc_<id>/")
    ap.add_argument("--probe-files", metavar="MACHINE_ID",
                    help="program transfer check, READ-ONLY: list the drives/folders the control offers and exit")
    ap.add_argument("--opcua-rights", metavar="MACHINE_ID",
                    help="SINUMERIK: show the OPC UA user's access rights and exit")
    ap.add_argument("--opcua-grant", nargs=2, metavar=("MACHINE_ID", "RIGHT"),
                    help="SINUMERIK: give the configured OPC UA user a right, e.g. FsWrite (the user must be the "
                         "OPC UA admin or allowed to manage rights)")
    ap.add_argument("--fanuc-card-send", nargs="+", metavar=("MACHINE_ID", "NAME"),
                    help="test: send a tiny test program to a FANUC memory card, read it back and compare")
    ap.add_argument("--fanuc-card-fetch", nargs=2, metavar=("MACHINE_ID", "FILE"),
                    help="read-only test: fetch one file from a FANUC memory card (CF_TEXT)")
    ap.add_argument("--probe-tools", metavar="MACHINE_ID",
                    help="read-only: which tool data (current tool, tool life) the control offers")
    ap.add_argument("--fanuc-card-test", nargs="+", metavar=("MACHINE_ID", "N"),
                    help="FANUC test, READ-ONLY: read the first N entries (default 1) of the memory card file list "
                         "with cnc_rdmcdfinfo. Asks for confirmation; not used by the monitor or the dashboard.")
    ap.add_argument("--merge-split-cycles", action="store_true",
                    help="repair history: merge 'counted before program end' fragments, mark partially seen cycles")
    args = ap.parse_args()

    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(os.path.join(HERE, "data", "monitor.log"),
                                              maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    # protocol libraries are very chatty - only show their real problems
    for noisy in ("asyncua", "LSV2 Client", "pyLSV2"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    cfg_path = os.path.join(HERE, "config.demo.toml") if args.demo else args.config
    if not os.path.exists(cfg_path):
        sys.exit(f"config not found: {cfg_path}  (copy config.example.toml to config.toml)")
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    # control type in any capitals ("FANUC", "Siemens"): the code compares lowercase; the dashboard shows it
    # in capitals anyway
    for m in cfg.get("machine", []):
        if isinstance(m.get("control"), str):
            m["control"] = m["control"].strip().lower()

    if args.probe:
        from machine_monitor.drivers import make_driver
        m = next((m for m in cfg.get("machine", []) if m["id"] == args.probe), None)
        if not m:
            sys.exit(f"no machine with id '{args.probe}' in {cfg_path}")
        logging.info("probing %s (%s, %s) ...", m["id"], m.get("name"), m.get("control"))
        for noisy in ("LSV2 Client", "pyLSV2"):      # their warnings help when probing
            logging.getLogger(noisy).setLevel(logging.WARNING)
        d = make_driver(m)

        class Live(list):          # print every probe line the moment it is produced
            def append(self, x):
                logging.info("probe %s | %s", m["id"], x)
                super().append(x)

            def extend(self, xs):
                for x in xs:
                    self.append(x)

            def __iadd__(self, xs):
                self.extend(xs)
                return self

        if hasattr(d, "probe"):
            d.probe(Live())
            return
        out = Live()
        try:
            d.connect()
            out.append("CONNECT OK")
            out.append(repr(d.read()))
            # alarms/messages: 30 s live, so a message can be triggered (e.g. open the door) while it runs
            out.append("alarms/messages for 30 s (None = could not be read):")
            last = object()
            for _ in range(15):
                try:
                    a = d.read_alarms()
                except Exception as e:
                    a = f"ERROR {type(e).__name__}: {e}"
                raw = getattr(d, "last_alarm_raw", None)
                praw = getattr(d, "last_plc_raw", None)
                if a != last:
                    out.append(f"  alarms: {a}")
                    if raw is not None:
                        out.append(f"  raw:    {raw}")
                    if praw is not None:
                        out.append(f"  plc:    {praw}")
                    last = a
                time.sleep(2)
            if hasattr(d, "probe_extra"):
                out.append("PLC marker check (read-only):")
                d.probe_extra(out)
        except Exception as e:
            out.append(f"FAILED: {type(e).__name__}: {e}")
        d.close()
        return

    if args.probe_tools:
        from machine_monitor.tool_probe import probe
        m = next((m for m in cfg.get("machine", []) if m["id"] == args.probe_tools), None)
        if not m:
            sys.exit(f"no machine with id '{args.probe_tools}' in {cfg_path}")
        m = {**cfg.get("defaults", {}), **m}
        print(f"Tool probe on {m.get('name', m['id'])} ({m.get('control')}, {m.get('host')}) - read only.")
        if m.get("control") == "fanuc":
            print("FANUC: reads the current T code and the tool life groups. Run it with the machine idle.")
            if input("Type YES to continue: ").strip() != "YES":
                sys.exit("cancelled")
        try:
            probe(m)
        except Exception as e:
            sys.exit(f"probe failed: {e}")
        print("\ndone - nothing was written to the control.")
        return

    if args.fanuc_card_send:
        return fanuc_card_send(cfg, cfg_path, args.fanuc_card_send)
    if args.fanuc_card_fetch:
        return fanuc_card_fetch(cfg, cfg_path, args.fanuc_card_fetch)
    if args.fanuc_card_test:
        return fanuc_card_test(cfg, cfg_path, args.fanuc_card_test)

    if args.opcua_rights or args.opcua_grant:
        from machine_monitor.transfer import SiemensFiles
        mid = args.opcua_rights or args.opcua_grant[0]
        m = next((m for m in cfg.get("machine", []) if m["id"] == mid), None)
        if not m or m.get("control") != "siemens":
            sys.exit(f"'{mid}' is not a SINUMERIK machine in {cfg_path}")
        m = {**cfg.get("defaults", {}), **m}
        try:
            with SiemensFiles(m) as s:
                if args.opcua_grant:
                    right = args.opcua_grant[1]
                    print(f"GiveUserAccess({m.get('username')!r}, {right!r}) ->", s.grant(m.get("username", ""), right))
                print("rights of", repr(m.get("username")), "->", s.my_rights())
        except Exception as e:
            print(f"failed: {e}")
            try:
                with SiemensFiles(m) as s:
                    print("\nWhat this control offers under Objects (send this to whoever helps you):")
                    print("\n".join(s.tree()))
            except Exception as e2:
                print("cannot list the control's objects:", e2)
            sys.exit(1)
        return

    if args.probe_files:
        from machine_monitor.transfer import TransferService
        m = next((m for m in cfg.get("machine", []) if m["id"] == args.probe_files), None)
        if not m:
            sys.exit(f"no machine with id '{args.probe_files}' in {cfg_path}")
        m = {**cfg.get("defaults", {}), **m}
        svc = TransferService([m], None, os.path.join(HERE, "data"), cfg.get("transfer", {}))
        if not svc.supported(m):
            sys.exit("file transfer is not switched on for this machine (FANUC: needs transfer = \"read\" or \"send\")")
        print("sending:", "ON" if svc.can_send(m) else 'off (transfer = "send" turns it on)')
        try:
            with svc.session(m) as s:
                if hasattr(s, "drives"):
                    try:
                        print("drives reported by the control:", ", ".join(s.drives()) or "(none)")
                    except Exception as e:
                        print("drives reported by the control: cannot read -", e)
                    print("   ", getattr(s, "last_drive_raw", ""))
                roots = s.roots()
                for r in roots:
                    print(f"\n== {r['label']}  {r['path']}  {r['note']}")
                    try:
                        items = s.list(r["path"])
                        for e in items[:25]:
                            print(f"   {'[dir] ' if e['dir'] else '      '}{e['name']:<40} {e.get('size') or '':>10}"
                                  f"  {e.get('comment') or ''}")
                        print(f"   ({len(items)} entries)")
                    except Exception as e:
                        print(f"   cannot list: {e}")
        except Exception as e:
            sys.exit(f"cannot open a file session: {e}")
        return

    if args.fetch_plc:
        m = next((m for m in cfg.get("machine", []) if m["id"] == args.fetch_plc), None)
        if not m or m.get("control") != "heidenhain":
            sys.exit(f"'{args.fetch_plc}' is not a heidenhain machine in {cfg_path}")
        # the PLC code number comes from stdin, so it is not visible in the command line / shell history
        if sys.stdin.isatty():
            import getpass
            pw = getpass.getpass("PLC code number: ")
        else:
            pw = sys.stdin.readline().strip()
        from machine_monitor.plc_fetch import fetch_plc_files
        try:
            fetch_plc_files(m, pw, os.path.join(HERE, "data"))
        except Exception as e:
            logging.error("fetch-plc %s failed: %s: %s", m["id"], type(e).__name__, e)
            sys.exit(1)
        return

    if args.merge_split_cycles:
        db = cfg.get("server", {}).get("database", "data/monitor.db")
        db = db if os.path.isabs(db) else os.path.join(HERE, db)
        done = Store(db).merge_split_cycles()
        per = {}
        for mid, prog, *_ in done:
            per[(mid, prog)] = per.get((mid, prog), 0) + 1
        for (mid, prog), n in sorted(per.items()):
            logging.info("merged %d split cycles: %s %s", n, mid, prog)
        logging.info("done - %d fragments merged into their part", len(done))
        part = Store(db).mark_partial_cycles()
        for mid, prog, ts in part:
            logging.info("marked partial (monitor started/reconnected mid-part): %s %s %s", mid, prog,
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)))
        logging.info("done - %d cycles marked partial", len(part))
        return

    server = cfg.get("server", {})
    db = server.get("database", "data/monitor.db")
    if not os.path.isabs(db):
        db = os.path.join(HERE, db)
    fresh_demo = args.demo and not os.path.exists(db)
    store = Store(db)
    defaults = cfg.get("defaults", {})

    workers = []
    for m in cfg.get("machine", []):
        if not m.get("enabled", True):
            continue
        w = MachineWorker(m, defaults, store)
        if fresh_demo and m.get("control") == "simulator":
            logging.info("generating %.0f h of simulated history for %s ...", args.backfill_hours, m["id"])
            w.backfill(args.backfill_hours)
        workers.append(w)
    for w in workers:
        w.start()

    def resource_watch():
        while True:
            try:
                fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1
            except OSError:
                fds = -1
            logging.info("resources: %s open files, %d threads", fds if fds >= 0 else "?", threading.active_count())
            time.sleep(1800)
    threading.Thread(target=resource_watch, daemon=True, name="resource-watch").start()

    def daily_summary():
        """Month/year views read a per-day summary. Fill it in the background: the first run after an
        update summarises the whole history (can take a few minutes on a Pi), then just the new day."""
        while True:
            try:
                t = time.time()
                n = store.refresh_daily()
                if n:
                    logging.info("daily summary: %d day(s) summarised in %.1f s", n, time.time() - t)
            except Exception as e:
                logging.warning("daily summary failed: %s", e)
            time.sleep(600)
    threading.Thread(target=daily_summary, daemon=True, name="daily-summary").start()

    host, port = server.get("host", "0.0.0.0"), int(server.get("port", 8080))
    from machine_monitor.plc_fetch import PlcJobs
    from machine_monitor.transfer import TransferService
    active = [m for m in cfg.get("machine", []) if m.get("enabled", True)]
    srv = serve(store, host, port, PlcJobs(active, os.path.join(HERE, "data")),
                TransferService([{**defaults, **m} for m in active], store, os.path.join(HERE, "data"),
                                cfg.get("transfer", {})), server.get("source_url"))
    logging.info("dashboard on http://%s:%d  (%d machines)", "localhost" if host == "0.0.0.0" else host, port,
                 len(workers))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for w in workers:
            w.stop_evt.set()
        srv.shutdown()


if __name__ == "__main__":
    main()

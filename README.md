# CNC Machine Monitoring

> Working with an AI assistant? Give it **AI_HANDOVER.md** first - architecture, decisions, setup checklist per control and lessons learned.
> New site / colleague? Start with **GETTING_STARTED.md**.

Monitors Siemens (OPC UA), Heidenhain (LSV2 / DNC option) and Fanuc (FOCAS2) machines and shows:

- **Running / stationary timeline** per machine: Running, Setup/MDI, Paused (feed hold, M0/M1), Stationary, Alarm, Offline
- **Part cycles**: program name, start, end, cycle time, cutting time vs. paused time, completed or aborted
- **Cycle times per program**: median / fastest / slowest, plus a trend chart per program
- **Live cards**: current program, running cycle time, sub-program executing right now, running % and parts
- **Program search** over the entire history (not limited to the date range): partial names, `*` wildcard, first/last made, median cycle (all time and last 10 runs), full cycle list + trend + CSV per program. Bookmarkable: `http://<pc>:8080/?q=PIN`
- CSV export of the cycle log (opens directly in Excel, `;`-separated)

Plain Python + SQLite. The web dashboard needs no internet connection.

> **Safety first.** This software only *reads* from the controls, except for the optional program transfer,
> which writes NC programs to the control and is **off by default**. A careless file probe once crashed a
> FANUC 0i-D (system alarm, power cycle needed) - see `AI_HANDOVER.md` 5b. Test every new control with the
> machine idle and someone at it. You use this software at your own risk (see the license: no warranty).

> **Network.** Viewing the dashboard needs no login and nothing is encrypted (no HTTPS); only changing machines
> needs the admin password. Run it only on a closed shop network, never exposed to the internet. See the
> known limits in `SECURITY.md`.

## Quick start (no machines needed)

```
python run.py --demo
```
Open http://localhost:8080. Four simulated machines (Fanuc, Siemens, Heidenhain and one control that reports only the program name) with 2 days of history.

## Real machines

1. **Windows: double-click `install.bat`** (or `install.bat --yes` from a script / AI agent - no questions).
   It installs Python 3.11+ if missing (winget), creates `.venv` with the packages, runs `--doctor`, and can
   add a start at logon (`--autostart`; remove with `--remove-autostart`). Other systems: Python **3.11+**,
   `python -m venv .venv`, `.venv/bin/pip install -r requirements.txt`.
2. `start.bat` (or `python run.py`). The first start creates an empty `config.toml`.
3. Add machines in the dashboard (**⚙ Settings**, see below), with
   `python run.py --add-machine m1 --control fanuc --host 192.168.1.21`, or by copying `config.example.toml`
   to `config.toml`. FANUC on Windows: FANUC's FOCAS2 DLLs in `FOCAS\` (found there automatically).
4. **Check**: `doctor.bat` / `python run.py --doctor` (`--json` for scripts; exit code 0 = no problems):
   Python, packages, config, FANUC library, data folder, port, admin password and whether every machine
   answers on its port. Read-only.

AI agents: see **`AGENTS.md`** (install, checks, and the safety rules for real machines).

### Raspberry Pi (permanent installation)

Raspberry Pi 4 with **Raspberry Pi OS Lite (32-bit)** - 32-bit is required for the FANUC library
(`FOCAS/libfwlib32-linux-armv7.so.1.0.5`, included - FANUC allows the Linux library to be redistributed; the
Pi 4's 64-bit kernel is fine). The Windows folder stays the
master copy; the Pi never needs access to the PC. Address of the Pi: `pi\pi_settings.local.bat` (not in Git; `pi\pi_settings.bat` holds only a placeholder).

| on the PC | what it does |
|---|---|
| `setup_pi.bat` | once: SSH key (no more passwords), upload, optionally copy the history database, install (Python, service that starts on boot, nightly backup at 02:15 keeping 14 days) |
| `deploy.bat` | send the current version incl. `config.toml` to the Pi, restart, show the log. The Pi keeps its `data\` |
| `rollback_pi.bat` | back to the version before the last deploy |
| `logs_pi.bat` | status + log of the Pi (copied to `data\monitor_pi.log`) |
| `probe_pi.bat m9` | `--probe` for one machine, run on the Pi |
| `probe_ip_pi.bat 192.168.1.34 siemens` | `--probe` for an IP **not in config** (control + optional port); OPC UA tries 4840 and 4848 |
| `backup_pi.bat` | copy the newest nightly database backup to `backups\` on the PC |

One `config.toml` serves both: on Linux a Windows `fwlib_path` is ignored and the right `.so` is picked
automatically. Only ONE monitor may run at a time (Siemens allows few OPC UA sessions) - stop `start.bat`
on the PC once the Pi runs. Dashboard: `http://<pi-address>:8080`.

### Per control

| Control | What it needs | Main program from | Part finished signal |
|---|---|---|---|
| Siemens 840D sl / ONE / 828D | OPC UA option enabled, user with read rights, port 4840 | `/Channel/ProgramInfo/selectedWorkPProg` | `actParts` / `totalParts` (whichever is enabled), `oldProgNetTimeCounter` |
| Heidenhain TNC 530/620/640/TNC7 | LSV2 port 19000, DNC option (#18) | program stack *main* | status **FINISHED** / **CANCELLED** |
| Fanuc 0i/16i/18i/30i/31i | FOCAS2 Ethernet (port 8193), FANUC FOCAS2 library (part no. A02B-0207-K737) | `cnc_pdf_rdmain` or `cnc_rdprgnum.mdata` | part counter macro #3901 |

**Testing an IP not yet in config**: `python run.py --probe <ip> --control <siemens|mtconnect|fanuc|heidenhain> [--port N]` (or `probe_ip_pi.bat <ip> <control> [port]`). For Siemens OPC UA, leaving the port off tries 4840 then 4848.

**Listing a whole OPC UA server** (when the node layout is unknown, e.g. a DMG MORI MDC / IoTconnector that doesn't use the native SINUMERIK paths): `python run.py --browse <ip>` (or `browse_opcua_pi.bat <ip>`) prints every NodeId, name and value. DMG MDC exposes its own address space (`Channelinformation/ExecutionState`, `OverallTimerAndCounter/PartCounter`, `MessagesAndAlarms/...`) rather than `/Channel/State/...`.

**Siemens**: `python run.py --probe <id>` (or `probe.bat <id>`) tests login, security and every value and writes the result to `data\monitor.log`. Verified on DN Solutions DNM6700/NHP5000: `chanStatus` 1 = active, `workPNameLong` is only the workpiece folder, umati not offered. Otherwise check the node paths with UaExpert (free from Unified Automation). They can differ slightly by software version. Override them in `[machine.nodes]`.
**Fanuc**: the part counter must count on M30 (parameter 6700/6710). If counting is switched off, the system falls back to reset detection.
**MTConnect** (`control = "mtconnect"`): any machine with an MTConnect agent (DMG MORI CELOS/MDC, Okuma, Mazak, or an adapter + agent). Read-only HTTP to `/current` once per second: Execution, ControllerMode, Program (MAIN/ACTIVE), PartCount, ToolNumber, Block, EmergencyStop and Conditions (shown as alarms). `--probe <id>` finds the agent's port and lists every data item it offers. Experimental - not yet verified on a real machine.

## How start/end is detected (sub-programs and M-code macros)

The program name the control is executing right now is **not** used to split cycles. During `M6 → O9001`, `M98 P8000`, `CALL PGM`, `L6`, `CYCLE800`… it changes, but that is still the same part. Instead:

- **Start**: the channel goes to *running* in AUTO mode while no cycle is open.
- **Stays in the cycle**: sub-programs, macros, feed hold, M0/M1, alarms, switching to JOG to clear chips, short network drops (< 2 min). Sub-programs are only recorded as "sub-programs seen".
- **End**, using the strongest signal available:
  1. part counter goes up (Fanuc #3901, Siemens actParts)
  2. program-end counter goes up (Siemens, M30/M2)
  3. program status FINISHED (Heidenhain)
  4. CANCELLED (Heidenhain) → *aborted*
  5. control stays in RESET for 5 s → *completed* or, if the machine normally gives signal 1–3 but didn't this time, *aborted*
- **Program name**: the control's own main program where available. Otherwise the first program at cycle start, skipping names matching `subprogram_patterns` (O9xxx, L…, CYCLE…, TOOL_CHANGE), is kept for the whole cycle and marked *(latched)* in the log.
- **Counter before the program end** (e.g. Lynx: counted, then the door opens, then M30): the cycle stays open until the program really ends, so the part is one cycle. If the program keeps running for `counter_end_window_s` (30 s) after the count - an **M99 loop / bar feeder** - the cycle is split exactly at the counter moment and a new one continues. History from before this fix: `python run.py --merge-split-cycles` (`fix_history_pi.bat`).
- **Warm-up programs**: per machine (Settings page, or `warmup_programs = ['^O?9000$']` in `config.toml`).
  While one runs in automatic mode the timeline shows *Warm-up*: no cycle, no part, not counted as running time.
- **Alarms that mean "waiting for the operator"**: some machines keep reporting the program running while they wait
  for an answer (e.g. a HEIDENHAIN PLC message `PLC 215 Correct tool in spindle ?`). List those per machine
  (Settings page, or `operator_alarms = ["PLC 215"]`): whole words, any case, matched against the alarm's number and
  text. While one is active the machine shows *Waiting for operator* with the message; inside a part that time is
  waiting, not machining time. Other alarms stay display only.
- **Waiting for robot / operator**: per machine (Settings page, or `[[machine.wait]]` in `config.toml`), rules mark program names (e.g. a robot handshake macro `O9022` or a robot load program `LOAD_PRODUCT_0.H`) as waiting. The timeline shows *Waiting for robot* and *Waiting for operator* separately; that time is never counted in the cycle time (`action = "end"`: the part is done when the wait starts, `"pause"`: wait inside the cycle). The cycle log shows the waiting time and the time spent per sub-program/macro.
- **Pallet machines (Siemens)**: `[machine.cycle_signal] block_regex = '\bM0?60\b'` ends the cycle at the M60 block and shows the clearance wait as *Waiting for operator*.
- **Partial cycles**: a part that was already running when the monitor started or reconnected is logged as **Partial**: it counts as a part, but not for cycle-time statistics. `--merge-split-cycles` (`fix_history_pi.bat`) also marks such cycles in older history.
- **Restarts (`deploy.bat`, reboot, a machine's settings saved)**: on a clean stop the monitor saves the part each machine has open (`data/handover.json`) and continues it after the restart - when the gap is under 3 minutes, the same program is running, no part counter rose and the next part could not have started yet (from the usual start-to-start time). The gap counts as the state it was in. Otherwise the saved part is stored as Partial (a part, unless a part counter shows it was reset). After a power cut parts are still Partial.
- **Noise**: cycles with less than 3 s actual running time are ignored.

## Settings page (machines in the browser)

**⚙ Settings** (top right) adds, changes and removes machines: name, control, IP address/port, login, program
transfer (off / browse + fetch / + send), the control-specific options, and the *waiting for robot/operator*
rules (type a program name such as `9021` or `LOAD_PRODUCT_0.H`; a regular expression also works).
*Test connection* tries the values before saving. After *Save* only that machine reconnects; the others keep
running.

- **Admin password**: chosen on the first visit to the page, stored hashed in `data/admin.json`. Forgotten?
  `reset_admin_pi.bat` (or `python run.py --set-admin-password` on the monitor) sets a new one and logs out
  every browser.
- Changes are written to **`config.toml`**, line by line: comments and everything the page doesn't show
  (OPC UA node overrides, cycle signals, transfer folders, `[defaults]`) stay as they are. The previous
  version is kept in `data/config_backup/` (last 20). Passwords are never sent back to the browser.
- **Raspberry Pi**: `deploy.bat` keeps a `config.toml` that was changed in the dashboard and copies it back to
  the PC afterwards (your PC's version is kept as `config.toml.before_deploy`). If it was changed on both, the
  dashboard version wins and the deploy says so.
- `[defaults]`, `[server]` and `[transfer]` (PIN, backups) are still edited in `config.toml` (restart needed).

### Programs: default folder and the server's program folder
Per machine, in Settings (machines with program transfer):
- **Default folder on the control** - opened first in *Programs*, as its folder line shows it, e.g.
  `TNC:\nc_prog\BMO\` or `//CNC_MEM/USER/PATH1/`. Empty = where the selected program is.
- **Program folder on the server** - where this machine's programs wait to be sent, e.g.
  `N:\Programma's DVF5000-BMO`. *Programs* then lists that folder (newest first, filter, sub-folders) with a
  **Send →** button per file: it goes to the folder open on the left, with the same PIN, protected/in-use checks
  and backup of a replaced program as an uploaded file. The server's files are only read.
  - Monitor on a **Windows PC**: `N:\...` works as it is (the monitor runs as the logged-in user).
  - Monitor on a **Raspberry Pi**: run `mount_share_pi.bat` once per drive letter. It asks the share path
    (`\\SERVER\share`, what `net use` shows for N:), a user and the password, mounts it **read-only** at
    `/mnt/n` and keeps it mounted after a reboot. The password is stored only on the Pi, in a root-only file.
    `N:\...` then maps to `/mnt/n/...` by itself; another place can be set with
    `[transfer] drives = { "N:" = "/media/programs" }`.

## Alarms & messages (machine detail view)

Click a machine: below the status and parts charts, **Alarms & messages** shows what is active on the control right now, how often each alarm occurred in the selected period and for how long, and a list of every occurrence. This is **for information only** - alarms never change the machine state or the cycles.

- **Fanuc**: CNC alarms (`SV0401`, `PS0011`, `EX1001` …) via `cnc_rdalmmsg2` (older series: `cnc_rdalmmsg`) and operator messages (`MSG 2010`, #3006 / PMC messages) via `cnc_rdopmsg`.
- **Heidenhain**: the error window via LSV2 (pyLSV2 `get_error_messages`) - **iTNC 530 only**. TNC 640/620/TNC7 don't support this request over LSV2; there the section says alarms can't be read (would need the Heidenhain OPC UA option).
- **Siemens**: OPC UA Alarms & Conditions (best effort: if the control doesn't send them, the view says so).

**Heidenhain PLC messages (TNC 640/620/TNC7)**: in the machine detail view, *PLC message files* → enter the control's PLC code number → *Copy from control*. The monitor copies the PLC text files (message table, texts, symbols) once, read-only, to `data/plc_<id>/` on the monitor. They tell which PLC marker belongs to which message (e.g. "door open"), so the monitor can read just those markers. `get_plc_files_pi.bat m1` copies them from the Pi to the PC if you want to look at them.

Read every 5 s (`alarm_poll_s`); switch off per machine with `alarms = false`.

**Limitation**: a control that gives *only* a program name (no counter, no status) can't tell an aborted program from a finished one. Those cycles are logged as completed.

Thresholds are in `[defaults]` in the config. Scenario tests: `python -m unittest discover tests`.

## Tools and tool life

The card of a selected machine shows the tool in the spindle; the detail view lists **Tools in <program>**
(median running time per tool per part, from the last 10 finished parts). Tool life is shown only where the
**control** manages it - there is no manual counting.

**HEIDENHAIN**: a part can start while the tool's CUR_TIME is below TIME2 (the control checks TIME2 only at TOOL
CALL - a tool in the spindle keeps cutting) and must not take it past TIME1; *parts left* is counted that way.

**Tool life used up**: when the machine stands still in automatic and the program's tool life is used up (e.g.
FANUC message *TOOL LIFE COUNT END ALARM*), it shows as *Waiting for operator: tool change* instead of
stationary.

**When the tool life runs out**: next to *parts left* the card shows *out ≈ 14:35* while the machine is producing:
the rest of the current part + the remaining parts x the usual time from one part start to the next (median of the
last part starts of that program, so robot loading and other time between parts is included; being a median, an
occasional break does not count). When the machine is not producing it shows how much production time is left instead.

- **Fanuc**: the tool in the spindle from macro `#4120`. With tool life management (e.g. `T0199` = group 1) the
  monitor reads the groups (life, counter, sister tools) at the end of a part and shows *parts left* for the program.
- **Siemens**: tool number and identifier (`actTNumber`, `actToolIdent`) with the normal values. Tools with
  **tool monitoring** on (piece count or time) are read at connect, at the end of every part for the tools it used
  (and once more 15 s later, as the control lowers the count at the end of the program). Piece count: *parts left*
  = the remaining count; a tool used up or locked on the control counts as 0. Switch off with `tool_life = false`.
- **Heidenhain**: the monitor asks the control once at connect whether it answers *tool in spindle*:
  - **yes** (e.g. iTNC 530): tool number from that request, name and life (`TIME1`/`TIME2`, `CUR.TIME`) read as
    single tool table fields - once per tool, and at the end of a part for the tools it used;
  - **no** (TNC 640): the PLC keeps the tool number in `DG_TOOL_IN_SPINDLE`; its address is taken from the
    machine's own PLC compiler output (`MainPgm.map` or `MainPgm.lst`) after *Copy from control* (see below), or
    set per machine with `tool_in_spindle = "D17032"`. Names and life come from `TNC:\table\tool.t`, read when
    connecting, at the start of a part after a setup, at the end of every part and for an unknown tool number.
    Tools swapped or a life reset in manual mode show their new life from the start of the next part.
- Life in minutes is turned into parts with the tool's usual time per part.

`probe_tools_pi.bat <id>` shows, read-only, what a control offers.

## Periods (weeks, months, years)

Next to the quick buttons (*Today*, *Last 8 h*, *Last 24 h*, *Last 7 days*) is one **📅 Period** button:
rolling periods (last 7/30/90 days, last 12 months), calendar periods (this/last week, month, quarter, year) or
any custom from–to range. **◀ ▶** step to the previous/next period of the same size (October → September,
week 46 → week 45, a day → the day before). The period is part of the address (e.g. `?period=2026-10`), so a
report can be bookmarked or shown on a shop-floor screen.

Periods longer than about a week show one bar per day in the timeline. These come from a **daily summary**
that the monitor builds in the background (the first start after an update summarises the existing history
once), so a whole year opens just as fast as a single day.

## Storage, and when to add a time series database

All history is stored in one SQLite file (`data/monitor.db`): roughly 300–400 kB per day for 13 machines,
about 150 MB per year. For this kind of data (states, cycles, alarms) SQLite is the right tool, and nothing has to
be installed to try the system.

If you later want to log **continuous signals** - feed rate, spindle speed, spindle load, overrides, axis loads,
power - several times per second, add a **time series database** next to it for those signals (SQLite would
need 30–50 bytes per value; a time series database 1–3 bytes, plus automatic averaging and clean-up):
**TimescaleDB** (PostgreSQL extension, one SQL database for everything) or **VictoriaMetrics** (light, signals
only). Note: the Raspberry Pi runs 32-bit because the FANUC FOCAS library only exists as 32-bit for ARM; most
time series databases need a 64-bit system (e.g. a small x86 PC). See `AI_HANDOVER.md` section 5d.

## Files

```
run.py                     start (python run.py / --demo)
config.example.toml        template for your machines
machine_monitor/engine.py  cycle detection (control-independent)
machine_monitor/drivers/   siemens_opcua.py, heidenhain_lsv2.py, fanuc_focas.py, simulator.py
machine_monitor/store.py   SQLite (data/monitor.db)
machine_monitor/web.py     API + dashboard (static/index.html)
tests/test_engine.py       scenarios: macros, feed hold, M99 loops, aborts, network drops
```

API (JSON, for Excel/Power BI): `/api/search?q=<text>` (all history), `/api/overview`, `/api/timeline`, `/api/cycles`, `/api/programs`, `/api/cycles.csv`, all with `?from=<unix>&to=<unix>&machine=<id>`.

## License

Copyright (C) 2026 Laurens Wijnschenk

Licensed under the **Apache License, Version 2.0** (see `LICENSE`). You may use, change and share it, also
commercially and in closed products, as long as you keep the copyright and license notices and state which
files you changed. It is distributed on an "AS IS" BASIS, **WITHOUT WARRANTIES** OR CONDITIONS OF ANY KIND.

FANUC's FOCAS2 library is not covered by this license. Only FANUC's Linux library for the Raspberry Pi is
included (unchanged, FANUC allows it to be redistributed); the Windows libraries are not - see `NOTICE.md`,
which also lists the third-party packages and trademarks.

This project is not affiliated with, endorsed by or supported by FANUC, Siemens, HEIDENHAIN, DN Solutions or
any other machine or control builder. Their names are only used to say which controls it works with.

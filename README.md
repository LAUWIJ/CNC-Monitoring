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

> **Network.** The dashboard has no login. Run it only on a closed shop network, never exposed to the internet.
> See `SECURITY.md`.

## Quick start (no machines needed)

```
python run.py --demo
```
Open http://localhost:8080. Four simulated machines (Fanuc, Siemens, Heidenhain and one control that reports only the program name) with 2 days of history.

## Real machines

1. Python **3.11 or newer** (`tomllib`). On a 64-bit Python, use `Fwlib64.dll` for Fanuc.
2. `pip install -r requirements.txt`
3. Copy `config.example.toml` to `config.toml` and fill in the IP addresses.
4. `python run.py` (or double-click `start.bat`)
5. To run permanently: Windows Task Scheduler "At startup" running `start.bat`, or NSSM as a service.

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
- **Waiting for robot / operator**: per machine, `[[machine.wait]]` rules mark program names (e.g. a robot handshake macro `O9022` or a robot load program `LOAD_PRODUCT_0.H`) as waiting. The timeline shows *Waiting for robot* and *Waiting for operator* separately; that time is never counted in the cycle time (`action = "end"`: the part is done when the wait starts, `"pause"`: wait inside the cycle). The cycle log shows the waiting time and the time spent per sub-program/macro.
- **Pallet machines (Siemens)**: `[machine.cycle_signal] block_regex = '\bM0?60\b'` ends the cycle at the M60 block and shows the clearance wait as *Waiting for operator*.
- **Partial cycles**: a part that was already running when the monitor started, restarted (e.g. `deploy.bat`) or reconnected is logged as **Partial**: it counts as a part, but not for cycle-time statistics. `--merge-split-cycles` (`fix_history_pi.bat`) also marks such cycles in older history.
- **Noise**: cycles with less than 3 s actual running time are ignored.

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

- **Fanuc**: the tool in the spindle from macro `#4120`. With tool life management (e.g. `T0199` = group 1) the
  monitor reads the groups (life, counter, sister tools) at the end of a part and shows *parts left* for the program.
- **Siemens**: tool number and identifier (`actTNumber`, `actToolIdent`) with the normal values.
- **Heidenhain**: the monitor asks the control once at connect whether it answers *tool in spindle*:
  - **yes** (e.g. iTNC 530): tool number from that request, name and life (`TIME1`/`TIME2`, `CUR.TIME`) read as
    single tool table fields - once per tool, and at the end of a part for the tools it used;
  - **no** (TNC 640): the PLC keeps the tool number in `DG_TOOL_IN_SPINDLE`; its address is taken from the
    machine's own PLC compiler output (`MainPgm.map` or `MainPgm.lst`) after *Copy from control* (see below), or
    set per machine with `tool_in_spindle = "D17032"`. Names and life come from `TNC:\table\tool.t`, read when
    connecting, at the start of a part after a setup, at the end of every part and for an unknown tool number.
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

This program is free software: you can redistribute it and/or modify it under the terms of the
**GNU Affero General Public License** as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version (see `LICENSE`). It is distributed in the hope that it will be
useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.

In short: you may use it, change it and share it, also commercially. If you share a changed version - or let
people use a changed version over a network, e.g. the dashboard on your shop network - you must offer them the
source code of your version under the same license. The dashboard footer links to the source; set
`[server] source_url` in `config.toml` if you run a changed version.

An additional permission allows use together with FANUC's FOCAS2 library, which is not free software. Only
FANUC's Linux library for the Raspberry Pi is included (unchanged, FANUC allows it to be redistributed); the
Windows libraries are not - see `NOTICE.md`, which also lists the third-party packages and trademarks.

This project is not affiliated with, endorsed by or supported by FANUC, Siemens, HEIDENHAIN, DN Solutions or
any other machine or control builder. Their names are only used to say which controls it works with.

# Instructions for AI agents

This repository is a CNC machine monitor: it reads FANUC, SIEMENS SINUMERIK, HEIDENHAIN and MTConnect controls
on a shop network and shows a dashboard on port 8080. It runs on a Windows PC or a Raspberry Pi.

You may be connected to **real production machines**. Read the safety rules before you run anything.

## Safety rules - never break these

1. **Never write to a control unless the person explicitly asks for that exact action.** That covers sending
   programs (dashboard or API), `--fanuc-card-send`, `--opcua-grant`, and setting `transfer = "send"`.
2. **Never run the FANUC test commands** (`--fanuc-card-send`, `--fanuc-card-fetch`, `--fanuc-card-test`) or
   `--probe-files` on a FANUC unless the person asks and someone is standing at the machine. A file probe once
   crashed a FANUC 0i-D (system alarm, power cycle needed). Never call FOCAS file, mount or drive functions
   yourself.
3. **Read-only is the default.** Safe to run at any time: `--doctor`, `--probe <id>`, `--db-stats`, `--demo`,
   the tests.
4. **Never commit or upload** `config.toml`, `config.toml.*`, `pi/pi_settings.local.bat`, `data/`, `certs/`,
   `backups/`, `.venv/`, `*.pcap*`, or FANUC Windows DLLs. They hold IP addresses, passwords, production data
   and licensed files.
5. **Don't choose the admin password yourself.** Ask the person, or tell them to set it in the dashboard
   (⚙ Settings) right after the first start.
6. **Don't change history** (`--merge-split-cycles`, edits to `data/monitor.db`) without asking.

## Install

Windows (installs Python with winget if missing, creates `.venv`, installs packages, runs a check):

```
install.bat --yes
```

`--yes` never asks and never waits for a key. Add `--autostart` only if the person wants the monitor to start
at logon. Then start it with `start.bat` (or `.venv\Scripts\python.exe run.py`).

Linux / macOS / any machine with Python 3.11+:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

Raspberry Pi (from the Windows PC, needs SSH to the Pi): fill in `pi/pi_settings.local.bat`, then
`setup_pi.bat` once and `deploy.bat` for updates. The Pi must run 32-bit Raspberry Pi OS for FANUC.

## Check your work

```
python run.py --doctor --json          (Windows: .venv\Scripts\python.exe run.py --doctor --json)
```

Read-only. Exit code 0 = no FAIL. Each check has `status` OK / WARN / FAIL, a `detail` and, if something is
wrong, a `fix`. The machine check only opens a TCP connection; add `--no-connect` to skip it.
`python run.py --demo` starts four simulated machines with history - use it to see the dashboard without
real machines (it uses `config.demo.toml` and `data/demo.db`, never the real ones).

## Add machines

Ask the person for each machine: control type, IP address, and a short permanent ID (`m1`, `m2`, ...; the
history is stored under it). Then either let them use the dashboard (⚙ Settings → + Add machine, with a
connection test), or:

```
python run.py --add-machine m14 --control fanuc --host 192.168.1.30 --name "Puma 2600"
python run.py --probe m14              (read-only: connects and shows what the control reports)
```

`--control` is `fanuc`, `siemens`, `heidenhain` or `mtconnect`. SINUMERIK also needs an OPC UA user and
password (`username`, `password` in `config.toml`); FANUC on Windows needs FANUC's FOCAS2 DLLs in `FOCAS\`
(licensed, not in the repository - see `FOCAS/README.txt`). A running monitor picks up machines added on the
command line after a restart; machines added in the dashboard start at once. All options per control:
`config.example.toml`.

## Tests

```
python -m pytest -q
```

No machines or network needed. Run them after every code change.

## Where to read more

- `README.md` - features, how cycles are detected, per-control notes.
- `AI_HANDOVER.md` - the full technical handover: design rules that must not break, site findings per
  control, file map, decisions. Read it before changing code.
- `SECURITY.md` - what is protected and the known limits.
- `GETTING_STARTED.md` - the same install for people, step by step.

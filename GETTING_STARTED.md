# Getting started (for a new site / colleague)

This repository contains the complete machine monitoring system. Everything that is specific to one
site - machines, IP addresses, passwords, the database, the FANUC libraries - is **not** in the repository.
You create your own copy of those, and they stay on your own PC.

Working with an AI assistant? Point it to **AGENTS.md** (install, checks, safety rules); for code changes
also **AI_HANDOVER.md**.

## 1. Get the code

1. Install **GitHub Desktop** (https://desktop.github.com) and sign in.
2. *File > Clone repository* -> choose this repository and a local folder, e.g. `C:\Machine Monitoring`.

## 2. Install and try the demo

1. Double-click **`install.bat`** in the project folder. It installs Python if it is missing (asks first),
   sets up the monitor's own Python environment (`.venv`) with the packages it needs, checks the
   installation and can make the monitor start when you log on to Windows. Running it again is safe.
2. Try it without machines: `start.bat --demo`, then open http://localhost:8080 - four simulated machines with
   two days of history. Close the window to stop.
3. Something wrong? **`doctor.bat`** checks the installation and whether your machines answer, and says what to
   fix (read-only).

## 3. Your own machines

1. Add your machines. Either in the dashboard: start the monitor (step 4), click **⚙ Settings** (top right),
   choose an admin password and use **+ Add machine** - with a *Test connection* button. Or on the command
   line: `python run.py --add-machine m1 --control fanuc --host 192.168.1.21 --name "Puma"`. Or by hand: copy
   `config.example.toml` to **`config.toml`** and fill in one `[[machine]]` block per machine.
   - `id`: short and permanent (it keys the history), `name`: free text, can change any time.
   - `enabled = false` (or untick *Monitor this machine*) skips a machine for now.
2. **FANUC machines**: on the Raspberry Pi nothing to do - FANUC's Linux library for the Pi is included in
   `FOCAS\`. To run the monitor on Windows, get the FOCAS2 library (FANUC part number **A02B-0207-K737**)
   from FANUC / your machine builder and put it in `FOCAS\` (see `FOCAS\README.txt`).
3. Test each machine: `python run.py --probe <id>` (or `probe.bat <id>`). The result is also in
   `data\monitor.log`. This shows whether the login works and which values come through - the variable
   paths can differ per control and software version.
4. Start: `python run.py` or double-click `start.bat`. Dashboard: http://localhost:8080.

Checklist per control (OPC UA licence for Siemens, DNC option for Heidenhain, FOCAS for Fanuc, ...):
`README.md` and `AI_HANDOVER.md` section 4.

## 4. Optional: permanent on a Raspberry Pi

1. Raspberry Pi 4 with **Raspberry Pi OS Lite (32-bit)** (Imager: "Raspberry Pi OS (other)"), SSH on,
   network cable. 32-bit is needed for FANUC.
2. Create **`pi\pi_settings.local.bat`** with your own Pi (this file is not in the repository):
   ```
   @echo off
   set PI=your-user@192.168.x.x
   ```
3. Run `setup_pi.bat` once, afterwards `deploy.bat` after every change. Details: `README.md`.

## 5. Updates and your own changes

- **Get updates**: GitHub Desktop -> *Fetch origin* -> *Pull*. Your `config.toml`, `data\` and `FOCAS\`
  are never touched by this. Then restart the monitor (or `deploy.bat` for the Pi).
- **Changing something yourself** (a fix, a new feature): make a *branch* in GitHub Desktop, commit, and
  open a *pull request*. Then the changes can be reviewed and shared with the other site.
- Keep site-specific things out of shared files: machines go in `config.toml`, your Pi in
  `pi\pi_settings.local.bat`.

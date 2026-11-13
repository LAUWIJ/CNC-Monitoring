# Getting started (for a new site / colleague)

This repository contains the complete machine monitoring system. Everything that is specific to one
site - machines, IP addresses, passwords, the database, the FANUC libraries - is **not** in the repository.
You create your own copy of those, and they stay on your own PC.

Working with an AI assistant? Give it **AI_HANDOVER.md** first.

## 1. Get the code

1. Install **GitHub Desktop** (https://desktop.github.com) and sign in.
2. *File > Clone repository* -> choose this repository and a local folder, e.g. `C:\Machine Monitoring`.

## 2. Install Python and try the demo

1. Install Python 3.11 or newer from https://www.python.org - tick **"Add python.exe to PATH"**.
2. Open a command window in the project folder (type `cmd` in the Explorer address bar) and run:
   ```
   pip install -r requirements.txt
   python run.py --demo
   ```
3. Open http://localhost:8080 - four simulated machines with two days of history. Close the window to stop.

## 3. Your own machines

1. Copy `config.example.toml` to **`config.toml`** and fill in your machines (one `[[machine]]` block each).
   - `id`: short and permanent (it keys the history), `name`: free text, can change any time.
   - `enabled = false` skips a machine for now.
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

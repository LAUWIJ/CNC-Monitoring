@echo off
rem READ-ONLY: how large the monitor's database on the Pi is, per table, and how fast it grows.
cd /d "%~dp0"
call pi\pi_settings.bat
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --db-stats; echo; df -h ~ | tail -1"
pause

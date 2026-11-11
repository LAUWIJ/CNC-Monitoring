@echo off
rem One-time repair on the Pi: merge "part counted before program end" fragments into their part.
rem Run deploy.bat FIRST (so the Pi has the new version). Safe to run more than once.
cd /d "%~dp0"
call pi\pi_settings.bat
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --merge-split-cycles"
pause

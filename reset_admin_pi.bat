@echo off
rem Sets a new admin password for the dashboard's Settings page (e.g. when it was forgotten).
rem Browsers that were logged in with the old password have to log in again.
cd /d "%~dp0"
call pi\pi_settings.bat
ssh -t %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --set-admin-password"
pause

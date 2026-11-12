@echo off
rem READ-ONLY: which tool data (current tool, tool life) a control offers.
rem   probe_tools_pi.bat m6
rem FANUC asks YES first (run it with the machine idle). Nothing is written to the control.
cd /d "%~dp0"
call pi\pi_settings.bat
if "%1"=="" (echo usage: probe_tools_pi.bat ^<machine id^> & pause & exit /b 1)
ssh -t %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --probe-tools %1"
pause

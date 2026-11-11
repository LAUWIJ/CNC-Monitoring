@echo off
rem Usage: probe_files_pi.bat m6   - READ-ONLY check of program transfer from the Pi:
rem lists the drives/folders the control offers (CNC memory, memory card, ...). Nothing is sent or changed.
cd /d "%~dp0"
call pi\pi_settings.bat
if "%1"=="" (echo usage: probe_files_pi.bat ^<machine id^> & pause & exit /b 1)
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --probe-files %1"
pause

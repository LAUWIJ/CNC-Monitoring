@echo off
rem Usage: probe_pi.bat m9   - tests one machine from the Pi; result also in data\monitor_pi.log
cd /d "%~dp0"
call pi\pi_settings.bat
if "%1"=="" (echo usage: probe_pi.bat ^<machine id^> & pause & exit /b 1)
if not exist data mkdir data
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --probe %1"
scp -q %PI%:%PI_DIR%/data/monitor.log "data\monitor_pi.log"
pause

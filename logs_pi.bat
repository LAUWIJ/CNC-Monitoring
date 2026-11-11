@echo off
cd /d "%~dp0"
call pi\pi_settings.bat
if not exist data mkdir data
ssh %PI% "systemctl --user --no-pager --lines=0 status machine-monitor | head -3; echo; tail -n 60 ~/%PI_DIR%/data/monitor.log"
scp -q %PI%:%PI_DIR%/data/monitor.log "data\monitor_pi.log" && echo. && echo (full log copied to data\monitor_pi.log)
pause

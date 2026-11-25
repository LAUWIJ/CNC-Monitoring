@echo off
rem Usage: probe_ip_pi.bat <ip> [control] [port]
rem   Tests an IP that is NOT in config.toml, from the Pi. Read-only.
rem   control: mtconnect (default) | siemens | fanuc | heidenhain
rem   OPC UA (siemens) without a port tries 4840 and 4848.
rem   Result also in data\monitor_pi.log
cd /d "%~dp0"
call pi\pi_settings.bat
if "%~1"=="" (echo usage: probe_ip_pi.bat ^<ip^> [control] [port]   e.g.  probe_ip_pi.bat 192.168.1.34 siemens & pause & exit /b 1)
set CTRL=%~2
if "%CTRL%"=="" set CTRL=mtconnect
set PORTARG=
if not "%~3"=="" set PORTARG=--port %~3
if not exist data mkdir data
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --probe %~1 --control %CTRL% %PORTARG%"
scp -q %PI%:%PI_DIR%/data/monitor.log "data\monitor_pi.log"
pause

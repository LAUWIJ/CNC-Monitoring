@echo off
rem Usage: browse_opcua_pi.bat <ip-or-id> [port]
rem   Lists the WHOLE OPC UA address space (NodeId, name, value) of a server, from the Pi. Read-only.
rem   For a server whose layout we don't know yet, e.g. DMG MORI MDC. No port -> tries 4840 then 4848.
rem   Result also in data\monitor_pi.log
cd /d "%~dp0"
call pi\pi_settings.bat
if "%~1"=="" (echo usage: browse_opcua_pi.bat ^<ip-or-id^> [port]   e.g.  browse_opcua_pi.bat 192.168.1.34 & pause & exit /b 1)
set PORTARG=
if not "%~2"=="" set PORTARG=--port %~2
if not exist data mkdir data
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --browse %~1 %PORTARG%"
scp -q %PI%:%PI_DIR%/data/monitor.log "data\monitor_pi.log"
pause

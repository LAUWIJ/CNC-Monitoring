@echo off
rem Usage: probe_ip.bat <ip> [control] [port]  - tests an IP not in config.toml, on this PC. Read-only.
rem   control: mtconnect (default) | siemens | fanuc | heidenhain   OPC UA tries 4840 and 4848.
cd /d "%~dp0"
set PY=python
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if "%~1"=="" (echo usage: probe_ip.bat ^<ip^> [control] [port] & pause & exit /b 1)
set CTRL=%~2
if "%CTRL%"=="" set CTRL=mtconnect
set PORTARG=
if not "%~3"=="" set PORTARG=--port %~3
%PY% run.py --probe %~1 --control %CTRL% %PORTARG%
pause

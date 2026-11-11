@echo off
rem SINUMERIK OPC UA user rights, run on the Pi.
rem   opcua_rights_pi.bat m10            show the rights of the OPC UA user configured for m10
rem   opcua_rights_pi.bat m10 FsWrite    give that user the right FsWrite (write files), then show the rights
cd /d "%~dp0"
call pi\pi_settings.bat
if "%1"=="" (echo usage: opcua_rights_pi.bat ^<machine id^> [right] & pause & exit /b 1)
if "%2"=="" (
  ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --opcua-rights %1"
) else (
  ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --opcua-grant %1 %2"
)
pause

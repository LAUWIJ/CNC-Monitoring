@echo off
rem Usage: get_plc_files_pi.bat m1
rem Copies the PLC files that the dashboard button stored on the Pi (data/plc_<id>) to this PC, e.g. to check them.
cd /d "%~dp0"
call pi\pi_settings.bat
if "%1"=="" (echo usage: get_plc_files_pi.bat ^<machine id^> & pause & exit /b 1)
if not exist data mkdir data
scp -r -q %PI%:%PI_DIR%/data/plc_%1 data\ && echo Files copied to data\plc_%1
pause

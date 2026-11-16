@echo off
rem SINUMERIK alarms via OPC UA: which object sends the alarm events, and what arrives.
rem   probe_alarms_pi.bat m9     step 1 is read-only; step 2 (listen 60 s) only after you confirm
cd /d "%~dp0"
call pi\pi_settings.bat
if "%1"=="" (echo usage: probe_alarms_pi.bat ^<machine id^> & pause & exit /b 1)
echo == step 1: read-only check
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --probe-alarms %1"
echo.
echo == step 2: listen for alarm events for 60 seconds
echo    This subscribes to the control's alarm events. Siemens' OPC UA server is normally fine with
echo    that, but with one older version a user reported that its OPC UA server shut down and had to
echo    be restarted. Best done when the machine is not in the middle of a part.
echo    Tip: trigger a harmless message during the 60 s (e.g. open the door) so there is something to see.
choice /M "   Listen now"
if errorlevel 2 goto :end
ssh %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --probe-alarms %1 --listen 60"
:end
pause

@echo off
rem READ-ONLY test: fetch one file from a FANUC memory card (CF_TEXT), the way the Program Transfer Tool does.
rem   fanuc_card_fetch_pi.bat m5 0001.NC
rem Machine NOT in a cycle, someone at the control. Asks YES first. Saves to data/card_test/ on the Pi.
cd /d "%~dp0"
call pi\pi_settings.bat
if "%2"=="" (echo usage: fanuc_card_fetch_pi.bat ^<machine id^> ^<file^> & pause & exit /b 1)
ssh -t %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --fanuc-card-fetch %1 %2"
pause

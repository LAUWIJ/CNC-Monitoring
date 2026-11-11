@echo off
rem READ-ONLY test of a FANUC memory card file list (cnc_rdmcdfinfo).
rem   fanuc_card_test_pi.bat m5       read entry 1 only
rem   fanuc_card_test_pi.bat m5 20    read up to 20 entries (stops at the first error)
rem Machine NOT in a cycle, someone at the control. Asks YES before it touches the control.
cd /d "%~dp0"
call pi\pi_settings.bat
if "%1"=="" (echo usage: fanuc_card_test_pi.bat ^<machine id^> [entries] & pause & exit /b 1)
ssh -t %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --fanuc-card-test %1 %2"
pause

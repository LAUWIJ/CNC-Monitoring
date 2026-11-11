@echo off
rem TEST: send a tiny test program to a FANUC memory card (CF_TEXT), read it back and compare.
rem   fanuc_card_send_pi.bat m5 O7778.NC
rem   fanuc_card_send_pi.bat m5 O7778.NC overwrite    send a test file that is already on the card again
rem Refuses if the name already exists on the card. Machine NOT in a cycle, someone at the control. Asks YES first.
cd /d "%~dp0"
call pi\pi_settings.bat
if "%2"=="" (echo usage: fanuc_card_send_pi.bat ^<machine id^> ^<name^> & pause & exit /b 1)
ssh -t %PI% "cd ~/%PI_DIR% && .venv/bin/python run.py --fanuc-card-send %1 %2 %3"
pause

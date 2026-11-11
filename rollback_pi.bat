@echo off
cd /d "%~dp0"
call pi\pi_settings.bat
ssh %PI% "bash ~/%PI_DIR%/pi/rollback.sh"
pause

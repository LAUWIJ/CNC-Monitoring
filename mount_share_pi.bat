@echo off
rem Makes a network drive (e.g. N: with the NC programs) readable for the monitor on the Raspberry Pi,
rem for "Program folder on the server" in Settings. Read-only. Asks the share path, user and password.
cd /d "%~dp0"
call pi\pi_settings.bat
ssh -t %PI% "bash ~/%PI_DIR%/pi/mount_share.sh"
pause

@echo off
rem Copies the newest nightly backup from the Pi to backups\ on this PC
cd /d "%~dp0"
call pi\pi_settings.bat
if not exist backups mkdir backups
for /f "delims=" %%f in ('ssh %PI% "ls -1t ~/backups/monitor-*.db 2>/dev/null | head -1"') do (
  scp -q %PI%:%%f backups\ && echo copied %%~nxf to backups\
)
pause

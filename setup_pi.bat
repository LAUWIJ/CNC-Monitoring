@echo off
setlocal
cd /d "%~dp0"
call pi\pi_settings.bat
echo ============================================================
echo  One-time installation of the monitor on the Raspberry Pi
echo  Pi: %PI%
echo ============================================================
echo.
echo == 1/4 SSH key, so deploy.bat never asks for a password
if not exist "%USERPROFILE%\.ssh\id_ed25519.pub" ssh-keygen -t ed25519 -N "" -f "%USERPROFILE%\.ssh\id_ed25519"
echo    (enter the Pi password once more if asked)
type "%USERPROFILE%\.ssh\id_ed25519.pub" | ssh %PI% "k=$(cat); mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; grep -qxF \"$k\" ~/.ssh/authorized_keys || echo \"$k\" >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys"
if errorlevel 1 goto :fail

echo == 2/4 upload program
call pi\make_package.bat || goto :fail
scp -q "%PKG%" %PI%:/tmp/mm_deploy.tgz || goto :fail
ssh %PI% "mkdir -p ~/%PI_DIR%/data && tar -xzf /tmp/mm_deploy.tgz -C ~/%PI_DIR% && rm /tmp/mm_deploy.tgz" || goto :fail

echo == 3/4 history (database) from this PC
ssh %PI% "test -f ~/%PI_DIR%/data/monitor.db" && (echo    Pi already has a database - not copied & goto :setup)
if not exist "data\monitor.db" (echo    no database on this PC - starting empty & goto :setup)
choice /M "   Copy the history from this PC to the Pi"
if errorlevel 2 goto :setup
py -c "import sqlite3,os; s=sqlite3.connect('data/monitor.db'); d=sqlite3.connect(os.path.join(os.environ['TEMP'],'mm_monitor.db')); s.backup(d); d.close(); s.close()" || goto :fail
scp -q "%TEMP%\mm_monitor.db" %PI%:%PI_DIR%/data/monitor.db || goto :fail
del "%TEMP%\mm_monitor.db"
echo    history copied

:setup
echo == 4/4 install on the Pi (asks for the Pi password for sudo)
ssh -t %PI% "bash ~/%PI_DIR%/pi/setup_pi.sh" || goto :fail
echo.
echo Done. IMPORTANT: stop the monitor on this PC (close start.bat / remove it from Task Scheduler),
echo otherwise both connect to the machines at the same time.
pause
exit /b 0
:fail
echo.
echo !! setup stopped - see the message above
pause
exit /b 1

@echo off
setlocal
cd /d "%~dp0"
rem Installs the monitor on this Windows PC: Python (if needed), its own .venv with the packages, a check,
rem and optionally a start when you log on to Windows. Safe to run again (e.g. after an update).
rem   install.bat                       asks before installing Python, the autostart and starting
rem   install.bat --yes                 no questions (scripts / AI agents): installs Python if missing,
rem                                     no autostart, does not start the monitor, never waits for a key
rem   install.bat --yes --autostart     the same, plus start at logon
rem   install.bat --remove-autostart    only removes the start at logon
set YES=
set AUTO=
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Machine Monitoring.cmd
for %%A in (%*) do (
  if /i "%%~A"=="--yes" set YES=1
  if /i "%%~A"=="--autostart" set AUTO=1
  if /i "%%~A"=="--remove-autostart" goto :unauto
)

echo == 1/4 Python 3.11 or newer
call :findpy
if defined PY goto :havepy
where winget >nul 2>nul
if errorlevel 1 goto :nopy
if defined YES goto :getpy
choice /M "   Python 3.11 or newer was not found. Install Python 3.12 now with winget"
if errorlevel 2 goto :nopy
:getpy
echo    installing Python 3.12 for this user ...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if not defined PY goto :nopy
:havepy
echo    Python: %PY%

echo == 2/4 own Python environment (.venv) and packages
if exist ".venv\Scripts\python.exe" goto :haveenv
echo    creating .venv (about half a minute) ...
%PY% -m venv .venv
if errorlevel 1 goto :fail
:haveenv
echo    installing packages (the first time this takes a few minutes) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --progress-bar on -r requirements.txt
if errorlevel 1 goto :fail
echo    packages installed

echo == 3/4 check (read-only)
".venv\Scripts\python.exe" run.py --doctor --no-connect
echo    FANUC on Windows: put FANUC's FOCAS2 DLLs in the FOCAS folder - see FOCAS\README.txt

echo == 4/4 start when you log on to Windows
if defined AUTO goto :auto
if defined YES goto :noauto
choice /M "   Start the monitor automatically when you log on"
if errorlevel 2 goto :noauto
:auto
> "%STARTUP%" echo @start "Machine Monitoring" /min "%CD%\.venv\Scripts\pythonw.exe" "%CD%\run.py"
if exist "%STARTUP%" (echo    added to the Startup folder - remove with: install.bat --remove-autostart) else echo !! could not add the autostart
goto :done
:noauto
echo    not added - later: install.bat --autostart, or start the monitor with start.bat

:done
echo.
echo Installed. Start: start.bat - dashboard: http://localhost:8080 - then Settings (top right): choose the
echo admin password and add your machines. Windows may ask whether Python may use the network: allow it for
echo private networks, so other PCs in the shop can open the dashboard.
if defined YES exit /b 0
choice /M "Start the monitor now and open the dashboard"
if errorlevel 2 goto :end
start "Machine Monitoring" "%CD%\start.bat"
timeout /t 5 >nul
start "" http://localhost:8080
:end
pause
exit /b 0

:unauto
if exist "%STARTUP%" del "%STARTUP%"
echo autostart removed
if not defined YES pause
exit /b 0

:nopy
echo !! No Python 3.11 or newer. Install it from https://www.python.org - tick "Add python.exe to PATH" -
echo !! then run install.bat again. (winget, the Windows package manager, can do it if it is available.)
:fail
echo !! installation stopped - see the messages above
if not defined YES pause
exit /b 1

:findpy
set PY=
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set PY=py -3
if defined PY exit /b 0
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set PY=python
if defined PY exit /b 0
for %%V in (313 312 311) do if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
exit /b 0

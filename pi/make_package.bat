@echo off
rem Packs the program files (NOT data\, NOT the Windows FOCAS DLLs) into %PKG%
set PKG=%TEMP%\mm_deploy.tgz
if exist "%PKG%" del "%PKG%"
set EXTRA=
if exist "FOCAS\libfwlib32-linux-armv7.so.1.0.5" set EXTRA=FOCAS/libfwlib32-linux-armv7.so.1.0.5
tar -czf "%PKG%" --exclude=__pycache__ run.py requirements.txt config.toml config.example.toml README.md machine_monitor tests pi %EXTRA%
if errorlevel 1 (echo !! packing failed & exit /b 1)
exit /b 0

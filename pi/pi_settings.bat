@echo off
rem === Raspberry Pi connection ===
rem Do NOT put your own Pi here (this file is shared in the repository).
rem Create pi\pi_settings.local.bat with your own values, e.g.:
rem     set PI=pi-user@192.168.1.100
set PI=pi-user@192.168.1.100
set PI_DIR=machine-monitoring
if exist "%~dp0pi_settings.local.bat" call "%~dp0pi_settings.local.bat"

@echo off
rem Records the network traffic between this PC and ONE machine straight into a file in this folder
rem (no save window). Uses dumpcap, which comes with Wireshark.
rem   capture_machine.bat 192.168.1.20 cf_text
rem Stop the recording with Ctrl+C.
cd /d "%~dp0"
set DUMPCAP="C:\Program Files\Wireshark\dumpcap.exe"
if not exist %DUMPCAP% (echo Wireshark / dumpcap not found in C:\Program Files\Wireshark & pause & exit /b 1)
if "%1"=="" (echo usage: capture_machine.bat ^<machine ip^> [name] & pause & exit /b 1)
set NAME=%2
if "%NAME%"=="" set NAME=capture
echo Network cards on this PC:
%DUMPCAP% -D
echo.
set /p IF=Number of the network card that connects to the machines: 
echo.
echo Recording traffic with %1 into "%~dp0%NAME%.pcapng"
echo Do the actions in the Program Transfer Tool now, then press Ctrl+C here to stop.
%DUMPCAP% -i %IF% -f "host %1" -w "%~dp0%NAME%.pcapng"
echo.
echo Saved: %~dp0%NAME%.pcapng
pause

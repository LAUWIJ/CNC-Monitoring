@echo off
rem Copies the PLC file list and the PLC compiler map / listing file of one HEIDENHAIN machine from the Pi to data\plc_<id>_MainPgm.map
rem (for working out where the PLC keeps a symbol, e.g. the tool in the spindle). Read-only, the Pi keeps its copy.
rem Usage: get_plc_map_pi.bat m1
cd /d "%~dp0"
call pi\pi_settings.bat
if "%~1"=="" (echo Usage: get_plc_map_pi.bat ^<machine id^> & pause & exit /b 1)
if not exist data mkdir data
scp -q %PI%:%PI_DIR%/data/plc_%~1/filelist.txt "data\plc_%~1_filelist.txt" && echo copied to data\plc_%~1_filelist.txt
scp -q %PI%:%PI_DIR%/data/plc_%~1/plc/program/MainPgm.map "data\plc_%~1_MainPgm.map" && echo copied to data\plc_%~1_MainPgm.map
scp -q %PI%:%PI_DIR%/data/plc_%~1/plc/program/MainPgm.lst "data\plc_%~1_MainPgm.lst" && echo copied to data\plc_%~1_MainPgm.lst
pause

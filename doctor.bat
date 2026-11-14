@echo off
rem Read-only check of this installation and whether the machines answer (run.py --doctor).
cd /d "%~dp0"
set PY=python
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
%PY% run.py --doctor %*
pause

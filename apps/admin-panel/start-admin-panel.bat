@echo off
cd /d "%~dp0..\.."
rem the job bot's own Python environment (.venv) when it exists
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" -m jobbot serve --site admin --port 8766 >> logs\admin-panel.log 2>&1

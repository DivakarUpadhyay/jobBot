@echo off
rem Started by the Windows scheduled tasks "JobBot Daily 0830" (every day at 08:30) and
rem "JobBot Loop" (when you sign in). If the loop is already running, the second start does nothing.
rem Runs the whole pipeline again and again; everything it prints goes to logs\loop.log.
cd /d "%~dp0"
rem the job bot's own Python environment (.venv) when it exists
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not exist logs mkdir logs
set PYTHONIOENCODING=utf-8
set COLUMNS=160
echo. >> logs\loop.log
echo ===== loop started %date% %time% ===== >> logs\loop.log
rem The two sites start together with the loop: the customer portal (http://127.0.0.1:8765/) and the admin
rem panel with the scheduler (http://127.0.0.1:8766/). A second copy cannot start: each claims its port
rem exclusively, so it ends at once.
start "JobPilot customer portal" /min cmd /c "apps\customer-portal\start-customer-portal.bat"
start "JobPilot admin panel" /min cmd /c "apps\admin-panel\start-admin-panel.bat"
"%PY%" -m jobbot cycle --loop >> logs\loop.log 2>&1

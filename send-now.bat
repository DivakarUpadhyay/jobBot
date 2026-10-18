@echo off
rem Send everything that is approved right now, at any hour (job applications first, then company emails).
rem The daily limits per account, the one-email-per-address rule and the bounce stop still apply.
rem Double-click this file, or run it from a terminal. What it sends is also written to logs\send-now.log.
pushd "%~dp0"
rem the job bot's own Python environment (.venv) when it exists
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set PYTHONIOENCODING=utf-8
set COLUMNS=160
echo ===== send-now %date% %time% ===== >> logs\send-now.log
"%PY%" -m jobbot send --now --per-run 150 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath logs\send-now.log -Append"
popd
echo.
pause

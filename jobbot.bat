@echo off
pushd "%~dp0"
rem the job bot's own Python environment (.venv) when it exists
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set PYTHONIOENCODING=utf-8
"%PY%" -m jobbot %*
popd

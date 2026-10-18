@echo off
pushd "%~dp0"
set PYTHONIOENCODING=utf-8
python -m jobbot %*
popd

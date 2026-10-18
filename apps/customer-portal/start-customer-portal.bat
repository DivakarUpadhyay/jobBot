@echo off
cd /d "%~dp0..\.."
python -m jobbot serve --site customer --port 8765 >> logs\customer-portal.log 2>&1

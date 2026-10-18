@echo off
cd /d "%~dp0..\.."
python -m jobbot serve --site admin --port 8766 >> logs\admin-panel.log 2>&1

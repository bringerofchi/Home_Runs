@echo off
cd /d "%~dp0"
echo Installing/checking dependencies...
python -m pip install -r requirements.txt -q
echo Starting MLB HR Edge Lab dashboard...
python webapp\app.py
pause

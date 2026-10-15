@echo off
rem Downloads new Kalshi 15-minute BTC windows (and BTC prices). Safe to close and re-run any time.
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
python fetch_data.py %*
pause

@echo off
rem Runs every edge test on the recordings so far and opens the report.
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
"%LOCALAPPDATA%\kalshi-btc15\.venv\Scripts\python.exe" morning_report.py
notepad reports\latest.md

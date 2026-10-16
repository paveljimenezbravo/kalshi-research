@echo off
rem Paper trading for the daily BTC "sell the tails" rule (#23). Run it around 11am ET each day.
rem It settles earlier paper trades, logs today's from the live order book, and prints the record.
rem Nothing is sent to Kalshi.
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
"%LOCALAPPDATA%\kalshi-btc15\.venv\Scripts\python.exe" paper_tails.py %*
pause

@echo off
rem Records Kalshi's live 15-minute BTC order book and trades plus live BTC prices.
rem Leave this window open; the edge tests get better with every hour it records.
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set "PY=%LOCALAPPDATA%\kalshi-btc15\.venv\Scripts\python.exe"
if not exist "%PY%" (
    call "%~dp0setup.bat" || (pause & exit /b 1)
)
"%PY%" -c "import aiohttp" 2>nul || "%PY%" -m pip install --quiet -r requirements.txt
"%PY%" recorder.py
pause

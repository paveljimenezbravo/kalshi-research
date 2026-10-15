@echo off
rem Auto-sell: once a position is up 100%, sells it if it then drops 20% from its best.
rem It only works while this window is open. Close it to stop.
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set "PY=%LOCALAPPDATA%\kalshi-btc15\.venv\Scripts\python.exe"
if not exist "%PY%" (
    call "%~dp0setup.bat" || (pause & exit /b 1)
)
"%PY%" -c "import cryptography" 2>nul || "%PY%" -m pip install --quiet -r requirements.txt
"%PY%" autosell.py %*
pause

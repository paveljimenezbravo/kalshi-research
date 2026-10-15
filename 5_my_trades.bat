@echo off
rem Opens your Kalshi trades in the browser. Only this PC can reach it. Close this window to stop it.
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set "PY=%LOCALAPPDATA%\kalshi-btc15\.venv\Scripts\python.exe"
if not exist "%PY%" (
    call "%~dp0setup.bat" || (pause & exit /b 1)
)
"%PY%" -c "import cryptography" 2>nul || "%PY%" -m pip install --quiet -r requirements.txt
echo Starting at http://localhost:8502 (close this window to stop it)...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 4; Start-Process 'http://localhost:8502'"
"%PY%" -m streamlit run my_trades.py --server.address localhost --server.port 8502
pause

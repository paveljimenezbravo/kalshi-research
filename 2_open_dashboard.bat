@echo off
rem Opens the dashboard in your browser. Close this window to stop it.
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set "PY=%LOCALAPPDATA%\kalshi-btc15\.venv\Scripts\python.exe"
if not exist "%PY%" (
    call "%~dp0setup.bat" || (pause & exit /b 1)
)
echo Starting the dashboard at http://localhost:8501 (close this window to stop it)...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 4; Start-Process 'http://localhost:8501'"
"%PY%" -m streamlit run app.py
pause

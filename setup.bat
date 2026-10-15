@echo off
rem One-time setup: a private Python environment, kept outside OneDrive, with the dashboard's libraries.
set "VENV=%LOCALAPPDATA%\kalshi-btc15\.venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo Creating the Python environment...
    python -m venv "%VENV%" || goto :fail
)
echo Installing libraries (first time takes a minute or two)...
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install --quiet -r "%~dp0requirements.txt" || goto :fail
echo Setup done.
exit /b 0

:fail
echo Setup failed. See the error above.
exit /b 1

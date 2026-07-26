@echo off
REM Growth Engine: double-click launcher (Windows).
REM Creates the virtual environment and installs packages on first run,
REM then starts the engine with the RIGHT Python every time.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment...
    python -m venv .venv || goto :nopython
    echo Installing packages, this takes a few minutes...
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :pipfail
)

if not exist ".env" (
    echo Creating .env from .env.example - open it and set DASHBOARD_PASSWORD.
    copy /y ".env.example" ".env" >nul
)

echo.
".venv\Scripts\python.exe" run.py
echo.
echo Engine stopped. Press any key to close.
pause >nul
exit /b 0

:nopython
echo.
echo ERROR: Python was not found. Install Python 3.11+ from python.org
echo and tick "Add python.exe to PATH" during setup.
pause >nul
exit /b 1

:pipfail
echo.
echo ERROR: package installation failed. Check your internet connection
echo and run start.bat again.
pause >nul
exit /b 1

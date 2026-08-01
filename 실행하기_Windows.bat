@echo off
cd /d "%~dp0"
echo ============================================
echo   ETF Dashboard Starting...
echo ============================================
echo.

REM check python
python --version >nul 2>&1
if errorlevel 1 (
  echo [!] Python is not installed.
  echo     Install from https://www.python.org/downloads/
  echo     During install, CHECK "Add Python to PATH".
  echo.
  pause
  exit /b
)

REM install libraries (first run only)
echo Installing required libraries (first time only)...
pip install -q flask finance-datareader

REM run
echo.
echo Opening in your browser at http://127.0.0.1:5000
python app.py

pause

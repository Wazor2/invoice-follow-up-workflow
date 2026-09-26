@echo off
setlocal

:: Ensure the script always runs from its own directory
cd /d "%~dp0"

title AR Copilot - Server
echo ===============================================================
echo                   AR Copilot - Invoice Follow-Up
echo ===============================================================
echo.

:: 1. Check if virtual environment exists
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment folder .venv not found.
    echo Please run setup.bat first to set up the environment and dependencies.
    echo.
    pause
    exit /b 1
)

:: 2. Check if port 8000 is already in use
netstat -ano | findstr /R /C:":8000 " >nul 2>nul
if %errorlevel% equ 0 (
    echo [WARNING] Port 8000 appears to be in use already by another process.
    echo If another AR Copilot server is running, please close it first.
    echo.
)

:: 3. Display current settings
echo Starting AR Copilot Server...
echo Web Dashboard:        http://localhost:8000/
echo Interactive API Docs: http://localhost:8000/docs
echo.
echo Press CTRL+C in this window anytime to stop the server.
echo ===============================================================
echo.

:: 4. Open default browser
start "" "http://localhost:8000/"

:: 5. Run Uvicorn using the virtual environment python directly
".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

:: If uvicorn exits or crashes, keep window open so user can read the error
echo.
echo ===============================================================
echo Server has stopped.
echo ===============================================================
pause

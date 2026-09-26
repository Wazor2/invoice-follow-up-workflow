@echo off
setlocal enabledelayedexpansion

:: Ensure the script always runs from its own directory
cd /d "%~dp0"

title AR Copilot - Setup
echo ===============================================================
echo             AR Copilot - Automated Environment Setup
echo ===============================================================
echo.

:: 1. Detect Python
echo [1/5] Checking Python installation...
set "PYTHON_CMD="
where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_CMD=python"
) else (
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON_CMD=py"
    ) else if exist "C:\Python314\python.exe" (
        set "PYTHON_CMD=C:\Python314\python.exe"
    ) else if exist "C:\Python313\python.exe" (
        set "PYTHON_CMD=C:\Python313\python.exe"
    ) else if exist "C:\Python312\python.exe" (
        set "PYTHON_CMD=C:\Python312\python.exe"
    )
)

if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python was not found in your PATH or standard directories.
    echo Please install Python 3.10+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('%PYTHON_CMD% --version 2^>^&1') do set PYTHON_VER=%%v
echo Found: %PYTHON_VER% using %PYTHON_CMD%

:: 2. Create virtual environment
echo.
echo [2/5] Checking virtual environment folder...
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv folder...
    %PYTHON_CMD% -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
) else (
    echo Virtual environment .venv already exists.
)

:: 3. Install packages
echo.
echo [3/5] Installing dependencies from requirements.txt...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Package installation failed. Please check errors above.
    pause
    exit /b 1
)
echo Dependencies installed successfully.

:: 4. Setup .env configuration
echo.
echo [4/5] Checking configuration .env...
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo Created .env from .env.example.
    ) else (
        echo COMPANY_NAME=Demo Company Pvt Ltd > .env
        echo HIGH_VALUE_THRESHOLD_INR=100000 >> .env
        echo MAX_FOLLOW_UPS=2 >> .env
        echo TEST_MODE=true >> .env
        echo MAIL_MODE=dry-run >> .env
        echo Created default .env file.
    )
) else (
    echo Configuration file .env already exists.
)

:: 5. Initialize database
echo.
echo [5/5] Initializing SQLite database...
".venv\Scripts\python.exe" -c "import db; db.init_db(); print('Database schema and tables initialized successfully.')"
if %errorlevel% neq 0 (
    echo [WARNING] Database initialization had an issue.
)

:: Google OAuth check
echo.
echo ---------------------------------------------------------------
if exist "credentials.json" (
    if exist "token.json" (
        echo [OK] Google credentials.json and token.json are ready.
    ) else (
        echo [INFO] credentials.json detected.
        echo To authorize Gmail and Google Sheets access, run: test_google_apis.bat
    )
) else (
    echo [INFO] credentials.json not found - normal for dry-run testing.
    echo To use real Gmail or Sheets, place credentials.json in this folder.
)
echo ---------------------------------------------------------------

echo.
echo ===============================================================
echo              Setup Completed Successfully!
echo ===============================================================
echo Double-click launch.bat to start the server and open the dashboard.
echo.
pause

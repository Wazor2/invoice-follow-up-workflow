@echo off
setlocal enabledelayedexpansion

:: Ensure the script always runs from its own directory
cd /d "%~dp0"

title AR Copilot - Google API Verification Menu
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment folder .venv not found.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

:menu
cls
echo ===============================================================
echo             AR Copilot - Google API and Testing Utility
echo ===============================================================
echo.
echo  1. First-time OAuth Setup - auth_setup.py
echo  2. Test Gmail API Read - test_gmail_read.py
echo  3. Test Gmail Search for INV-1001 - test_gmail_search.py
echo  4. Test Gmail Safe Send - test_gmail_send.py
echo  5. Test Google Sheets Read - test_sheets.py
echo  6. Update Sheet Row Sample - update_invoice_status.py
echo  7. Run Full Automated Test Suite - pytest
echo  8. Exit
echo.
echo ===============================================================
set /p choice="Select an option [1-8]: "

if "%choice%"=="1" goto opt1
if "%choice%"=="2" goto opt2
if "%choice%"=="3" goto opt3
if "%choice%"=="4" goto opt4
if "%choice%"=="5" goto opt5
if "%choice%"=="6" goto opt6
if "%choice%"=="7" goto opt7
if "%choice%"=="8" goto opt8

echo Invalid choice. Please try again.
ping 127.0.0.1 -n 2 >nul
goto menu

:opt1
echo.
echo Running OAuth Setup...
".venv\Scripts\python.exe" auth_setup.py
echo.
pause
goto menu

:opt2
echo.
echo Reading Gmail Profile and Labels...
".venv\Scripts\python.exe" test_gmail_read.py
echo.
pause
goto menu

:opt3
echo.
set "inv_id=INV-1001"
set /p inv_id="Enter invoice ID to search [default INV-1001]: "
".venv\Scripts\python.exe" test_gmail_search.py !inv_id!
echo.
pause
goto menu

:opt4
echo.
set "to_email="
set /p to_email="Enter destination email address for test send: "
if "!to_email!"=="" (
    echo [ERROR] Email address is required.
) else (
    ".venv\Scripts\python.exe" test_gmail_send.py !to_email!
)
echo.
pause
goto menu

:opt5
echo.
echo Testing Google Sheets read access...
".venv\Scripts\python.exe" test_sheets.py
echo.
pause
goto menu

:opt6
echo.
".venv\Scripts\python.exe" update_invoice_status.py
echo.
pause
goto menu

:opt7
echo.
echo Running pytest suite...
".venv\Scripts\python.exe" -m pytest --basetemp=.pytest_tmp
echo.
pause
goto menu

:opt8
exit /b 0

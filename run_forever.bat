@echo off
title Jegubot Reflexivity

set BASE_DIR=%~dp0
set PYTHON=%BASE_DIR%venv\Scripts\python.exe
set SCRIPT=%BASE_DIR%orchestrator.py
set ENV_FILE=%BASE_DIR%.env

cd /d "%BASE_DIR%"

echo.
echo ============================================
echo   Jegubot Reflexivity - Startup Checklist
echo ============================================
echo.

set CHECK_OK=1

if exist "%ENV_FILE%" (
    echo [OK] .env found
) else (
    echo [FAIL] .env not found
    set CHECK_OK=0
)

if exist "%PYTHON%" (
    echo [OK] venv found: %PYTHON%
) else (
    echo [FAIL] venv not found at: %PYTHON%
    echo        Run: python -m venv venv
    set CHECK_OK=0
)

if not exist "%BASE_DIR%data\" (
    mkdir "%BASE_DIR%data"
)
echo [OK] data/ folder ready

if exist "%SCRIPT%" (
    echo [OK] orchestrator.py found
) else (
    echo [FAIL] orchestrator.py not found at: %SCRIPT%
    set CHECK_OK=0
)

for /f "tokens=1,* delims==" %%a in ('findstr /i "DRY_RUN" "%ENV_FILE%" 2^>nul') do (
    echo [INFO] DRY_RUN=%%b
)

echo.

if "%CHECK_OK%"=="0" (
    echo [ERROR] Checklist failed. See above.
    pause
    exit /b 1
)

echo [OK] All checks passed. Starting in 5s... ^(Ctrl+C to cancel^)
echo.
timeout /t 5 /nobreak >nul

:LOOP
echo.
echo [%date% %time%] Launching orchestrator.py --interval 600
echo --------------------------------------------

"%PYTHON%" "%SCRIPT%" --interval 600

echo.
echo [%date% %time%] Process exited ^(code: %ERRORLEVEL%^). Restarting in 30s...
echo      Close this window to stop completely.
timeout /t 30 /nobreak >nul

goto LOOP

@echo off
title Jegubot Reflexivity

set BASE_DIR=%~dp0
set PYTHON=%BASE_DIR%venv311\Scripts\python.exe
set SCRIPT=%BASE_DIR%orchestrator.py
set ENV_FILE=%BASE_DIR%.env

cd /d "%BASE_DIR%"

echo.
echo ============================================
echo   Jegubot Reflexivity - Startup Checklist
echo ============================================
echo.

set CHECK_OK=1

set EXISTING_ORCH_COUNT=0
for /f %%i in ('powershell -NoProfile -Command "$count = @(Get-CimInstance Win32_Process ^| Where-Object { $_.CommandLine -like '''*orchestrator.py*''' }).Count; Write-Output $count"') do set EXISTING_ORCH_COUNT=%%i
if not "%EXISTING_ORCH_COUNT%"=="0" (
    echo [ERROR] Existing orchestrator process detected: %EXISTING_ORCH_COUNT%
    echo        Close the existing worker first.
    pause
    exit /b 1
)

if exist "%ENV_FILE%" (
    echo [OK] .env found
) else (
    echo [FAIL] .env not found
    set CHECK_OK=0
)

if exist "%PYTHON%" (
    echo [OK] venv311 found: %PYTHON%
) else (
    echo [FAIL] venv311 not found at: %PYTHON%
    echo        Run: C:\Users\82109\AppData\Local\Programs\Python\Python311\python.exe -m venv venv311
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

set LOCK_FILE=%BASE_DIR%data\orchestrator.lock
if exist "%LOCK_FILE%" (
    set /p LOCK_PID=<"%LOCK_FILE%"
    if not "%LOCK_PID%"=="" (
        tasklist /FI "PID eq %LOCK_PID%" | findstr /I "python.exe" >nul 2>&1
        if not errorlevel 1 (
            echo [ERROR] Existing orchestrator instance detected ^(PID %LOCK_PID%^).
            echo        Close the existing worker first.
            pause
            exit /b 1
        ) else (
            echo [WARNING] Stale orchestrator lock found. Removing it.
            del "%LOCK_FILE%" >nul 2>&1
        )
    )
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

set RUN_INTERVAL=60
for /f "tokens=1,* delims==" %%a in ('findstr /i "RUN_INTERVAL_SEC" "%ENV_FILE%" 2^>nul') do (
    set RUN_INTERVAL=%%b
)

:LOOP
echo.
echo [%date% %time%] Launching orchestrator.py --interval %RUN_INTERVAL%
echo --------------------------------------------

"%PYTHON%" "%SCRIPT%" --interval %RUN_INTERVAL%
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" if exist "%LOCK_FILE%" (
    echo.
    echo [%date% %time%] Existing orchestrator instance blocked startup ^(code: %EXIT_CODE%^). Exiting.
    exit /b %EXIT_CODE%
)

echo.
echo [%date% %time%] Process exited ^(code: %EXIT_CODE%^). Restarting in 30s...
echo      Close this window to stop completely.
timeout /t 30 /nobreak >nul

goto LOOP

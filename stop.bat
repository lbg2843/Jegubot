@echo off
title Jegubot - Stop

echo.
echo ============================================
echo   Jegubot Reflexivity - Stop
echo ============================================
echo.

echo [INFO] Searching for orchestrator process...

set FOUND=0
for /f "tokens=1" %%p in ('wmic process where "name='python.exe' and commandline like '%%orchestrator%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    echo [KILL] Terminating PID %%p
    taskkill /PID %%p /F >nul 2>&1
    set FOUND=1
)

taskkill /FI "WINDOWTITLE eq Jegubot Reflexivity" /F >nul 2>&1

if "%FOUND%"=="1" (
    echo [OK] Orchestrator process terminated.
) else (
    echo [INFO] No running orchestrator process found.
)

echo.
echo Closing in 3s...
timeout /t 3 /nobreak >nul

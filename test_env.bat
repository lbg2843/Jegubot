@echo off
set BASE_DIR=%~dp0
set PYTHON=%BASE_DIR%venv\Scripts\python.exe

echo BASE_DIR = %BASE_DIR%
echo PYTHON   = %PYTHON%
echo.

if exist "%PYTHON%" (
    echo [OK] python.exe found
    "%PYTHON%" --version
) else (
    echo [FAIL] python.exe NOT found at above path
)

echo.
if exist "%BASE_DIR%.env" (
    echo [OK] .env found
    type "%BASE_DIR%.env"
) else (
    echo [FAIL] .env NOT found
)

echo.
pause

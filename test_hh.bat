@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
echo [hrbot3] hh.ru API test
echo.

if "%~1"=="" (
    python test_hh.py
) else (
    python test_hh.py %*
)

echo.
pause

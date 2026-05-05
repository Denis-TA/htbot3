@echo off
echo [hrbot3] Stopping all python processes...
powershell -Command "Stop-Process -Name python -Force -ErrorAction SilentlyContinue"
echo [hrbot3] Done.
timeout /t 2 >nul

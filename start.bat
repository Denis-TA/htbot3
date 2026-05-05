@echo off
cd /d "%~dp0"

call stop.bat

echo [hrbot3] Starting...
echo.
python bot.py
echo.
echo [hrbot3] Bot stopped.
pause

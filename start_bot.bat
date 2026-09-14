@echo off
REM Double-click to run bot in background (no PowerShell window stays open)
cd /d "%~dp0"
start "" /min pythonw run.py
echo Bot started in background. Check Task Manager ^> pythonw.exe to confirm.
pause

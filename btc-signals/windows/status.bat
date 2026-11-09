@echo off
rem Mode, risk state and open positions.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.trader status
pause

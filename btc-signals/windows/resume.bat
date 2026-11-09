@echo off
rem Clear a kill switch / pause so the bot trades again.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.trader resume
pause

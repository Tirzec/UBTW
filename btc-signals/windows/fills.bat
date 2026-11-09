@echo off
rem Your latest real fills on Polymarket US (read-only).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.trader fills
pause

@echo off
rem Read-only live-readiness checks. Optional: preflight.bat --expect-balance 50
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.preflight %*
pause

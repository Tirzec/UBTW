@echo off
rem Exit diagnosis: live positions that reached a 90c+ bid. Use --days N to look further back.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.trader exits --live %*
pause

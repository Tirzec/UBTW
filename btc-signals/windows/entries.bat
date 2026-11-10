@echo off
rem Entry diagnosis: every live trade bought below 30c (why it passed) and results below 30c vs above 50c.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.trader entries --live %*
pause

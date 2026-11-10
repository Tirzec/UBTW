@echo off
rem Execution diagnosis: entry speed and fills, profit left on the table by early sells, old vs new rules.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.trader execution --live %*
pause

@echo off
rem Copy trading: score top international traders, backtest copying them 10-30 s late, list the best.
rem Takes 20-40 minutes. Read-only: places no orders.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.copytrade candidates %*
pause

@echo off
rem Today's summary: trades, P&L after fees, win rate, avg win/loss, sports CLV. Add --live for live trades, --send to push it to your phone.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.trader summary %*
pause

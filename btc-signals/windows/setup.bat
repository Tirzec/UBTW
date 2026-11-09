@echo off
rem Double-click or run from a terminal. Runs the PowerShell script of the same name.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause

@echo off
rem Step 4. test-order.bat = rest + cancel only.  test-order.bat --fill --qty 1 = also a real buy + sell.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m app.test_order %*
pause

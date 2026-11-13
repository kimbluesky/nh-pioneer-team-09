@echo off
cd /d "%~dp0"
if not exist "runtime\.venv\Scripts\python.exe" (
 echo Run setup.cmd first.
 pause
 exit /b 1
)
cd runtime
echo Open http://127.0.0.1:47104/?view=rotation^&present=1 in a browser.
echo A new session requires the Start button.
".venv\Scripts\python.exe" -m uvicorn backend.strategy.blend_app:app --host 127.0.0.1 --port 47104 --log-level warning
pause

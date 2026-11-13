@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
 python code\prepare_runtime.py
) else (
 py -3 code\prepare_runtime.py
)
if errorlevel 1 (
 pause
 exit /b 1
)
pause

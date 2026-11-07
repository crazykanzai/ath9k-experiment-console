@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0start_console.py" --open %*
) else (
  py -3 "%~dp0start_console.py" --open %*
)
if errorlevel 1 pause

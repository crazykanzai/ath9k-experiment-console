@echo off
setlocal
cd /d "%~dp0"
echo Preparing a local Python environment. No node connections or traffic.
if not exist "%~dp0.venv\Scripts\python.exe" (
  py -3 -m venv "%~dp0.venv"
  if errorlevel 1 goto failed
)
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-observer.txt"
if errorlevel 1 goto failed
echo Ready. Double-click start_console.bat to open the console.
pause
exit /b 0
:failed
echo Setup failed. Install Python 3.10 or later and check the error above.
pause
exit /b 1

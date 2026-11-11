@echo off
setlocal
cd /d "%~dp0"
if exist "runtime\python\python.exe" (
    "runtime\python\python.exe" tools\install_dependencies.py --profile tui --check
    if errorlevel 1 exit /b 1
    "runtime\python\python.exe" -m solbreaker tui %*
    exit /b
)
echo Use the self-contained Windows release or launch from a prepared Python environment.
exit /b 1

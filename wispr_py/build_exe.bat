@echo off
setlocal
cd /d "%~dp0"

set PYTHON=E:\Apps\scoop\apps\miniconda3\current\python.exe
if exist ".venv\Scripts\python.exe" set PYTHON=%~dp0.venv\Scripts\python.exe

echo Ensuring PyInstaller is installed...
"%PYTHON%" -m pip install pyinstaller -q

echo Building Wispr\Wispr.exe (thin launcher, uses existing packages)...
:: onedir (not onefile) = single process = one clean PID for process management
"%PYTHON%" -m PyInstaller launcher.py ^
    --name Wispr ^
    --onedir ^
    --noconsole ^
    --icon "%~dp0wispr.ico" ^
    --distpath . ^
    --workpath tmp\build ^
    --specpath tmp ^
    --noconfirm

echo.
if exist "Wispr\Wispr.exe" (
    echo Done: %~dp0Wispr\Wispr.exe
) else (
    echo Build FAILED - see output above.
)
pause

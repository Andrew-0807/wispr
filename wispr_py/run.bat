@echo off
setlocal
cd /d "%~dp0"

:: Launch the managed launcher exe -> single named process "Wispr.exe" that
:: external tools can detect/start/stop by name. Do NOT launch wispr.py directly;
:: that produces an unmanaged bare pythonw.exe. Rebuild the exe with build_exe.bat.
:: (LM Studio server is started by wispr.py itself, so no need to do it here.)
if not exist "Wispr\Wispr.exe" (
    echo Wispr\Wispr.exe not found. Build it first: build_exe.bat
    pause
    exit /b 1
)
start "" "Wispr\Wispr.exe"

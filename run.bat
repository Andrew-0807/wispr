@echo off
setlocal
cd /d "%~dp0"

:: ── Set this to the full path of the Python that has all packages installed ──
:: To find it, open a terminal where "pip show faster-whisper" works and run:
::   python -c "import sys; print(sys.executable)"
:: Then paste the result below.
set PYTHON=E:\Apps\scoop\apps\miniconda3\current\python.exe

:: ── Auto-detect venv in this folder (overrides the setting above if present) ─
if exist ".venv\Scripts\python.exe" set PYTHON=%~dp0.venv\Scripts\python.exe

echo Using: %PYTHON%
echo.

:: ── Start LM Studio server if lms CLI is available ────────────────────────
where lms >nul 2>&1
if %errorlevel%==0 (
    echo Starting LM Studio server...
    lms server start >nul 2>&1
)

"%PYTHON%" wispr.py
pause

@echo off
setlocal

:: Change to the script's directory so .env is found correctly
cd /d "%~dp0"

:: Use the venv if it exists, otherwise fall back to system Python
if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    set PYTHON=python
)

echo Starting Wispr...
%PYTHON% wispr.py
pause

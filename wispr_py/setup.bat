@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Wispr – first-time setup
echo ============================================================

:: Create virtualenv
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

:: Activate
call .venv\Scripts\activate.bat

:: Upgrade pip quietly
python -m pip install --upgrade pip -q

:: Install PyTorch with CUDA 12.8 support first (RTX 3060)
echo Installing PyTorch (CUDA 12.8)...
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 -q

:: Install the rest
echo Installing dependencies...
pip install -r requirements.txt -q

echo.
echo ============================================================
echo  Setup complete!
echo  Edit .env and add your GEMINI_API_KEY, then run run.bat
echo ============================================================
pause

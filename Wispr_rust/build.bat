@echo off
setlocal
cd /d "%~dp0"
set LIBCLANG_PATH=E:\Apps\scoop\apps\llvm\current\bin
rem Build into a SHORT path: whisper.cpp's Vulkan shader-gen nests dirs so deep
rem that MSBuild's FileTracker hits the 260-char MAX_PATH limit under target\.
set CARGO_TARGET_DIR=E:\w

echo ========================================================
echo   Building Wispr Rust (Release Mode with LTO)
echo ========================================================
cargo build --release

if %errorlevel% equ 0 (
    if not exist "target\release" mkdir "target\release"
    copy /y "E:\w\release\wispr.exe" "target\release\wispr.exe" >nul
    copy /y "E:\w\release\*.dll" "target\release\" >nul 2>&1
    echo.
    echo Build successful: target\release\wispr.exe
) else (
    echo.
    echo Build failed with error code %errorlevel%
)
pause

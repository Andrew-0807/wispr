@echo off
setlocal
cd /d "%~dp0"
set LIBCLANG_PATH=E:\Apps\scoop\apps\llvm\current\bin
set CARGO_TARGET_DIR=E:\w

if not exist "target\release\wispr.exe" (
    echo Building Wispr in release mode...
    cargo build --release
    if %errorlevel% neq 0 (
        echo Build failed.
        pause
        exit /b 1
    )
    if not exist "target\release" mkdir "target\release"
    copy /y "E:\w\release\wispr.exe" "target\release\wispr.exe" >nul
)

start "" "target\release\wispr.exe"
exit

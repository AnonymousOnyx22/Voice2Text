@echo off
echo ==========================================
echo   Voice2Text - One-Click Builder
echo ==========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python from https://python.org
    pause
    exit /b 1
)

echo [1/4] Installing dependencies...
python -m pip install pyinstaller pillow speechrecognition pyaudio --quiet
if errorlevel 1 (
    echo [WARN] pyaudio may need pipwin. Trying fallback...
    python -m pip install pipwin --quiet
    pipwin install pyaudio --quiet
)

echo [2/4] Building executable...
if exist make_icon.py python make_icon.py
python -m PyInstaller --onefile --windowed --name "Voice2Text" --icon icon.ico --clean voice2text.py

echo [3/4] Cleaning up...
if exist build rmdir /s /q build

echo [4/4] Done!
echo.
echo Your executable is here:
echo   dist\Voice2Text.exe
echo.
pause

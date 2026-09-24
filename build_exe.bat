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

echo [1/4] Checking Python version...
for /f "tokens=2" %%v in ('python -c "import sys; print(sys.version.split()[0])"') do set PYVER=%%v
echo   Found Python %PYVER%
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] in [(3,10),(3,11),(3,12)] else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python %PYVER% is not supported by PyAudio.
    echo PyAudio 0.2.14 only ships prebuilt wheels for Python 3.10-3.12.
    echo On 3.13/3.14 pip tries to compile from source and fails with
    echo "Microsoft Visual C++ 14.0 or greater is required".
    echo.
    echo Fix: install Python 3.11 64-bit from https://python.org,
    echo tick "Add python.exe to PATH", then re-run this script with 3.11.
    echo.
    pause
    exit /b 1
)

echo [2/4] Installing dependencies...
echo   - PyAudio ^(needs the matching wheel - this is the step that fails on 3.13/3.14^)...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Could not upgrade pip.
    pause
    exit /b 1
)
python -m pip install PyAudio==0.2.14
if errorlevel 1 (
    echo.
    echo [ERROR] PyAudio failed to install. Common causes:
    echo   - Wrong Python version ^(use 3.10-3.12 64-bit^)
    echo   - 32-bit Python instead of 64-bit
    echo   - Old pip: run "python -m pip install --upgrade pip" and retry
    echo NOTE: pipwin is dead ^(2021, max Python 3.9^) - do not use it as fallback.
    echo.
    pause
    exit /b 1
)
python -m pip install SpeechRecognition==3.10.4 pyinstaller pillow
if errorlevel 1 (
    echo [ERROR] Could not install remaining dependencies. See error above.
    pause
    exit /b 1
)

echo [3/4] Building executable...
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

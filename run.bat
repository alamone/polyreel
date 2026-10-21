@echo off
rem Starts Polyreel. The first run installs the Python packages it needs.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)

python -m pip show PySide6 python-mpv >nul 2>nul
if errorlevel 1 (
    echo Installing the Python packages Polyreel needs ^(first run only^)...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo The packages could not be installed; see the messages above.
        pause
        exit /b 1
    )
)

if not exist "libmpv-2.dll" if not exist "lib\libmpv-2.dll" (
    echo.
    echo libmpv-2.dll is missing. Polyreel uses it to play video.
    echo   1. Download the latest mpv-dev-x86_64-v3-*.7z ^(or mpv-dev-x86_64-*.7z for older CPUs^) from
    echo      https://github.com/shinchiro/mpv-winbuild-cmake/releases
    echo   2. Open it with 7-Zip and extract libmpv-2.dll into this folder:
    echo      %~dp0
    echo.
    pause
    exit /b 1
)

python -m polyreel %*
if errorlevel 1 pause

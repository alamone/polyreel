@echo off
rem Starts Polyreel. Put libmpv-2.dll next to this file (see README).
cd /d "%~dp0"
python -m polyreel %*
if errorlevel 1 pause

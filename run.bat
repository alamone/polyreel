@echo off
rem Starts Masonry Player. Put libmpv-2.dll next to this file (see README).
cd /d "%~dp0"
python -m masonry_player %*
if errorlevel 1 pause

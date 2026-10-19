
@echo off
title Python Launcher

REM Automatically change to this BAT file's folder
cd /d "%~dp0"

REM Run the Python script
py "%~dp0app.py"

pause

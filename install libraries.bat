
@echo off
title Chief's Court - Install Dependencies

echo ==========================================
echo   Chief's Court - Python Dependencies
echo ==========================================
echo.

echo Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 goto error

echo.
echo Installing Flask and Werkzeug...
python -m pip install Flask Werkzeug
if errorlevel 1 goto error

echo.
echo Verifying installation...
python -c "import flask, werkzeug, json, os, uuid, datetime, functools; print('All required imports are available.')"
if errorlevel 1 goto error

echo.
echo ==========================================
echo Installation completed successfully!
echo ==========================================
echo.
echo You can now start your application using:
echo python app.py
echo.
pause
exit /b 0

:error
echo.
echo Installation failed. Check the error messages above.
pause
exit /b 1

@echo off
title Auto Song Downloader Web Server
color 0b

echo ====================================================
echo   Launching Auto Song Downloader Web App
echo ====================================================
echo.

echo Checking Python requirements...
python -c "import flask" 2>NUL
if errorlevel 1 (
    echo Installing Flask...
    pip install flask --quiet
)

echo Opening Web App in your default browser...
start http://127.0.0.1:5000

echo Starting Web Server on http://127.0.0.1:5000 ...
python app.py

@echo off
title Auto Downloader UI & API
color 0b

echo ===========================================
echo   Starting Auto Downloader Web UI / API
echo ===========================================
echo.

echo Checking requirements (Flask)...
pip install flask --quiet

echo.
echo Launching your default web browser...
:: Wait a second for the python server to initialize before opening browser
start "" http://127.0.0.1:5000

echo.
echo ----------------------------------------------------
echo   Server is running! Keep this window open.
echo   - Web UI is available at: http://127.0.0.1:5000
echo   - Local API is available at: POST http://127.0.0.1:5000/api/download
echo   
echo   Close this window to stop the server.
echo ----------------------------------------------------
echo.

python app.py
pause

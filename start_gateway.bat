@echo off
title NEXORA WhatsApp Gateway
echo ===================================================
echo Starting NEXORA WhatsApp Gateway...
echo ===================================================

set "PATH=C:\Program Files\nodejs;%PATH%"
cd /d "%~dp0"

echo.
echo Launching gateway on port 3000...
node server.js
pause

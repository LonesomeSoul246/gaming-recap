@echo off
chcp 65001 >nul
title Gaming Recap (mit Fenster)
cd /d "%~dp0"
rem Nur zur Fehlersuche: startet Gaming Recap mit sichtbarem Fenster.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py %*
) else (
  python server.py %*
)
pause

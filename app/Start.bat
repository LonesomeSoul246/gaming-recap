@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Gaming Recap laeuft unsichtbar im Hintergrund (kein Fenster in der Taskleiste).
rem Beenden oder Neustarten: in der App unter Einstellungen > System.
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 server.py %*
  exit /b
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw server.py %*
  exit /b
)
echo.
echo  Python ist noch nicht installiert.
echo  Die Download-Seite wird geoeffnet. Beim Installieren unbedingt
echo  "Add python.exe to PATH" anhaken. Danach Start.bat erneut starten.
echo.
start https://www.python.org/downloads/
pause

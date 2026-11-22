@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Gaming Recap zuruecksetzen
echo  ===========================
echo  Spielt die letzte Sicherung zurueck (Programm + Spielverlauf + Begleiter).
echo  Deine Keys und Einstellungen bleiben, wie sie sind.
echo  Der aktuelle Stand wird vorher selbst noch gesichert.
echo.
set /p OK=  Wirklich zuruecksetzen? (j/n) 
if /i not "%OK%"=="j" exit /b
echo  Beende Gaming Recap ...
powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -ContentType application/json -Body '{}' http://127.0.0.1:8765/api/quit | Out-Null } catch {}"
timeout /t 4 /nobreak >nul
where py >nul 2>nul
if %errorlevel%==0 (py -3 server.py --restore latest) else (python server.py --restore latest)
echo.
echo  Starte Gaming Recap wieder ...
call Start.bat
timeout /t 3 >nul

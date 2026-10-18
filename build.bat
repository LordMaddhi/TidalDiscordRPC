@echo off
REM Builds dist\TidalDiscordRPC.exe
REM Detects a working Python launcher (py or python) and creates .venv if needed
cd /d "%~dp0"

if exist .venv\Scripts\python.exe goto :build

set "PY="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :makevenv

python --version >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :makevenv

echo No working Python found. Install it from python.org or create the venv in PyCharm.
goto :err

:makevenv
%PY% -m venv .venv
if errorlevel 1 goto :err

:build
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :err
.venv\Scripts\python.exe -m PyInstaller --onefile --noconsole --icon icon.ico --name TidalDiscordRPC tidal_discord_rpc.py
if errorlevel 1 goto :err

echo.
echo Done: dist\TidalDiscordRPC.exe
pause
exit /b 0

:err
echo Build failed.
pause
exit /b 1
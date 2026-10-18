@echo off
REM Starts the app without a console window (uses the venv if it exists)
cd /d "%~dp0"
if exist .venv\Scripts\pythonw.exe (
    start "" .venv\Scripts\pythonw.exe tidal_discord_rpc.py
) else (
    start "" pythonw tidal_discord_rpc.py
)

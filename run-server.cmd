@echo off
rem Launched by the "soundcloud-dl server" scheduled task at logon.
cd /d "%~dp0"
set LOGDIR=%LOCALAPPDATA%\soundcloud-dl
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ---- %DATE% %TIME% starting server ---- >> "%LOGDIR%\server.log"
python server.py >> "%LOGDIR%\server.log" 2>&1

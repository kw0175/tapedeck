@echo off
rem Launched by the "soundcloud-dl tunnel" scheduled task at logon.
rem --wait rides out the race with the server task; boot order is not guaranteed.
cd /d "%~dp0"
set LOGDIR=%LOCALAPPDATA%\soundcloud-dl
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ---- %DATE% %TIME% starting tunnel ---- >> "%LOGDIR%\tunnel.log"
python tunnel.py --wait 300 >> "%LOGDIR%\tunnel.log" 2>&1

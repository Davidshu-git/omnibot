@echo off
setlocal

set "INSTALL_DIR=C:\Users\sdw\ws-scrcpy-web"
set "CURRENT_DIR=%INSTALL_DIR%\current"
set "CONFIG_PATH=C:\ProgramData\WsScrcpyWeb\config.json"
set "PORT=8000"
set "WS_SCRCPY_CONFIG=%CONFIG_PATH%"

cd /d "%CURRENT_DIR%"
call start.cmd >> "%INSTALL_DIR%\out.log" 2>>&1

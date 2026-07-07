@echo off
setlocal EnableDelayedExpansion

set "BOT_DIR=D:\LocalGIT\Delta.Exchange"
set "LOG_DIR=%BOT_DIR%\logs"
set "PYTHON=D:\LocalGIT\Delta.Exchange\.venv\Scripts\python.exe"

cd /d "%BOT_DIR%"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [%DATE% %TIME%] Scheduler triggered >> "%LOG_DIR%\scheduler.log"
%PYTHON% "%BOT_DIR%\Delta_Main.py"
echo [%DATE% %TIME%] Bot exited with code %ERRORLEVEL% >> "%LOG_DIR%\scheduler.log"

endlocal

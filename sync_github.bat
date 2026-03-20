@echo off
setlocal
chcp 65001 >nul

title Sync chatGPT_Simulator
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Scripts\sync_github.ps1" %*
set "EXIT_CODE=%errorlevel%"
if not "%~1"=="--scheduled" if not "%~1"=="install-task" if not "%~1"=="uninstall-task" (
    echo.
    pause
)
exit /b %EXIT_CODE%

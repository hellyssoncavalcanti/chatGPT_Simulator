@echo off
rem Wrapper versionado para disparar o sync automatico do Windows.
setlocal
chcp 65001 >nul

title Sync chatGPT_Simulator

rem Verifica se o script foi aberto sem argumentos (duplo clique)
rem Se for o caso, assume o modo recorrente (--scheduled) automaticamente.
set "ARGS=%*"
if "%~1"=="" set "ARGS=--scheduled"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Scripts\sync_github.ps1" %ARGS%
set "EXIT_CODE=%errorlevel%"

rem Pausa a janela apenas se nao estiver em modo recorrente ou de instalacao
if not "%ARGS%"=="--scheduled" if not "%~1"=="install-task" if not "%~1"=="uninstall-task" (
    echo.
    pause
)
exit /b %EXIT_CODE%

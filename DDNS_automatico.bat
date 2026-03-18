@echo off
:: Garante que o diretório de trabalho seja o local onde este arquivo .bat está
cd /d "%~dp0"

:: Define o caminho do script PowerShell de forma dinâmica
:: %~dp0 traz o caminho com a barra final (ex: C:\OpenClaw\)
set "TARGET_SCRIPT=%~dp0scripts\ddns-client.ps1"

title Atualizando IP dinamico para o deste PC

:: Verifica se o arquivo existe antes de tentar rodar (bom para debug)
if not exist "%TARGET_SCRIPT%" (
    echo ERRO: O arquivo nao foi encontrado em:
    echo "%TARGET_SCRIPT%"
    pause
    exit /b
)

:: Executa o PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -File "%TARGET_SCRIPT%"
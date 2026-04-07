@echo off
chcp 65001 >nul
title WhatsApp Follow-up Server (Web)

cd /d "C:\chatgpt_simulator"
if not exist "logs" mkdir logs
if not exist "db" mkdir db

set "PYTHON_BOOTSTRAP="
if exist ".venv\pyvenv.cfg" if exist ".venv\Scripts\python.exe" set "PYTHON_BOOTSTRAP=.venv\Scripts\python.exe"
if not defined PYTHON_BOOTSTRAP (
    where py >nul 2>&1
    if %errorLevel%==0 (
        set "PYTHON_BOOTSTRAP=py -3"
    ) else (
        set "PYTHON_BOOTSTRAP=python"
    )
)

echo.
echo +------------------------------------------------------------------+
echo   WHATSAPP FOLLOW-UP SERVER (WEB WHATSAPP)
echo   Iniciando servico isolado via https://web.whatsapp.com/
echo +------------------------------------------------------------------+
echo.

echo [INFO] Verificando dependencias minimas...
%PYTHON_BOOTSTRAP% -m pip install -q requests flask playwright
%PYTHON_BOOTSTRAP% -m playwright install chromium

echo [INFO] Iniciando servidor PyWa...
echo [INFO] Arquivo: Scripts\acompanhamento_whatsapp.py
%PYTHON_BOOTSTRAP% Scripts\acompanhamento_whatsapp.py
if %errorLevel% neq 0 (
    echo.
    echo [ERRO] Falha ao iniciar o servidor WhatsApp Web.
    echo [ERRO] Verifique acesso a https://web.whatsapp.com/ e se o login QR foi realizado.
    echo [ERRO] Endpoints utilizados por padrao:
    echo [ERRO] - SIMULATOR_URL: http://127.0.0.1:3003/v1/chat/completions
    echo [ERRO] - PHP_URL: https://conexaovida.org/scripts/js/chatgpt_integracao_criado_pelo_gemini.js.php
    echo.
    echo [AJUDA] Guia de configuracao:
    echo [AJUDA] 1^) Acesso WhatsApp Web: https://web.whatsapp.com/
    echo [AJUDA] 2^) Playwright docs: https://playwright.dev/python/
    echo [AJUDA] 3^) Se necessario, use tunel HTTPS local ^(ngrok^): https://ngrok.com/docs/getting-started/
)

pause

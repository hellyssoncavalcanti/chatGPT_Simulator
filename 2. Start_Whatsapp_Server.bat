@echo off
chcp 65001 >nul
title WhatsApp Follow-up Server (PyWa)

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
echo   WHATSAPP FOLLOW-UP SERVER (PYWA)
echo   Iniciando servico de envio de acompanhamento e resposta automatica
echo +------------------------------------------------------------------+
echo.

echo [INFO] Verificando dependencias minimas...
%PYTHON_BOOTSTRAP% -m pip install -q requests "pywa[flask]"

echo [INFO] Iniciando servidor PyWa...
echo [INFO] Arquivo: Scripts\pywa_acompanhamento_server.py
%PYTHON_BOOTSTRAP% Scripts\pywa_acompanhamento_server.py
if %errorLevel% neq 0 (
    echo.
    echo [ERRO] Falha ao iniciar o servidor PyWa.
    echo [ERRO] Verifique variaveis: PYWA_PHONE_ID e PYWA_TOKEN.
    echo [ERRO] PYWA_VERIFY_TOKEN e opcional ^(se ausente, um token local padrao sera usado^).
    echo [ERRO] Recomendado configurar tambem PYWA_APP_SECRET para validar assinatura do webhook.
    echo [ERRO] Tambem confirme conectividade com PHP_URL e SIMULATOR_URL.
    echo.
    echo [AJUDA] Guia de configuracao:
    echo [AJUDA] 1^) Meta Apps: https://developers.facebook.com/apps/
    echo [AJUDA] 2^) Cloud API: https://developers.facebook.com/docs/whatsapp/cloud-api/get-started
    echo [AJUDA] 3^) Webhooks: https://developers.facebook.com/docs/graph-api/webhooks/getting-started
    echo [AJUDA] 4^) Docs PyWa: https://pywa.readthedocs.io/
    echo [AJUDA] 5^) Túnel HTTPS local ^(ngrok^): https://ngrok.com/docs/getting-started/
)

pause

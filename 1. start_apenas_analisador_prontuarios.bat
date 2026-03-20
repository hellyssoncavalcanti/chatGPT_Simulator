@echo off
chcp 65001 >nul
title Analisador de Prontuarios

cd /d "C:\chatgpt_simulator"
if not exist "logs" mkdir logs

echo [INFO] Ativando venv...
call .venv\Scripts\activate.bat

echo [INFO] Verificando dependencias...
python -m pip install requests --quiet

echo [INFO] Iniciando Analisador de Prontuarios...
echo.
.venv\Scripts\python.exe scripts\analisador_prontuarios.py

pause
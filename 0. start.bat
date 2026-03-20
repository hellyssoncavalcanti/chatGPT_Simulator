@echo off
chcp 65001 >nul
title ChatGPT Simulator

cd /d "C:\chatgpt_simulator"
if not exist "logs" mkdir logs
if not exist "db" mkdir db

:: Limpeza de processos
echo [INFO] Limpando processos...
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM ms-playwright.exe >nul 2>&1

echo [INFO] Ativando venv...
call .venv\Scripts\activate.bat

echo [INFO] Verificando dependencias...
python -m pip install flask flask-cors playwright pystray pillow cryptography --quiet
playwright install chromium --quiet >nul 2>&1

:: Verifica se a regra de firewall ja existe — eleva SOMENTE se necessario
netsh advfirewall firewall show rule name="ChatGPT-3002" >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO] Regra de firewall ausente. Adicionando ^(requer elevacao pontual^)...
    powershell -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command \"netsh advfirewall firewall add rule name=''ChatGPT-3002'' dir=in action=allow protocol=TCP localport=3002\"' -Verb RunAs -Wait"
    echo [INFO] Regra de firewall adicionada.
) else (
    echo [INFO] Regra de firewall ja existe. Nenhuma elevacao necessaria.
)

echo.
echo +-------------------------------------------------------+
echo   CHATGPT SIMULATOR - INICIANDO
echo   Acesse: https://localhost:3002
echo   (Aceite o risco de certificado no navegador)
echo +-------------------------------------------------------+
echo.

start /B cmd /c "timeout /t 6 >nul && start https://localhost:3002"

echo [INFO] Iniciando Sistema Modular...
.venv\Scripts\python.exe scripts\main.py

pause

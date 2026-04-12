@echo off
chcp 65001 >nul
title Agente Autonomo de Melhoria Continua

cd /d "C:\chatgpt_simulator"
if not exist "logs" mkdir logs

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

echo [INFO] Iniciando agente autonomo...
%PYTHON_BOOTSTRAP% Scripts\agente_autonomo.py

pause

@echo off
chcp 65001 >nul
setlocal EnableExtensions

title ChatGPT Simulator
cd /d "%~dp0"

echo [BOOT] Verificando arquivos sensiveis locais...
set "FRESH_INSTALL="
if not exist "Scripts\config.py" (
  if exist "Scripts\config.example.py" (
    copy /Y "Scripts\config.example.py" "Scripts\config.py" >nul
    set "FRESH_INSTALL=1"
    echo [BOOT] Scripts\config.py criado a partir do template.
  )
)
if not exist "Scripts\sync_github_settings.ps1" (
  if exist "Scripts\sync_github_settings.example.ps1" (
    copy /Y "Scripts\sync_github_settings.example.ps1" "Scripts\sync_github_settings.ps1" >nul
    echo [BOOT] Scripts\sync_github_settings.ps1 criado a partir do template.
  )
)

if defined FRESH_INSTALL (
  echo [BOOT] Novo local detectado. Reiniciando credenciais para admin/admin.
  if exist "db\users\users.json" del /Q "db\users\users.json" >nul 2>&1
  if exist "db\app.db" del /Q "db\app.db" >nul 2>&1
)

if not exist ".venv\Scripts\python.exe" (
  echo [BOOT] Criando ambiente virtual...
  py -3 -m venv .venv
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
  echo [ERRO] Falha ao ativar .venv
  pause
  exit /b 1
)

echo [BOOT] Atualizando pip...
python -m pip install --upgrade pip >nul

if exist "requirements.txt" (
  echo [BOOT] Instalando dependencias de runtime...
  pip install -r requirements.txt
)
if exist "requirements-test.txt" (
  echo [BOOT] Instalando dependencias de teste...
  pip install -r requirements-test.txt
)

echo [BOOT] Iniciando sistema...
python Scripts\main.py

endlocal
pause

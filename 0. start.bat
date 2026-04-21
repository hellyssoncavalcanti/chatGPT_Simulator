@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

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

call :resolve_chromium_exe
if not defined CHROMIUM_EXE (
  echo [WARN] Nenhum executavel Chromium/Chrome/Edge encontrado. Pulando validacao assistida de login.
) else (
  call :ensure_chatgpt_profile_login "default" "chrome_profile"
  call :ensure_chatgpt_profile_login "segunda_chance" "chrome_profile_segunda_chance"
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
exit /b 0

:resolve_chromium_exe
set "CHROMIUM_EXE="

if defined CHATGPT_SIMULATOR_CHROMIUM_EXE (
  if exist "%CHATGPT_SIMULATOR_CHROMIUM_EXE%" (
    set "CHROMIUM_EXE=%CHATGPT_SIMULATOR_CHROMIUM_EXE%"
    goto :eof
  )
)

if exist "%LocalAppData%\ms-playwright" (
  for /f "delims=" %%D in ('dir /b /ad /o-n "%LocalAppData%\ms-playwright\chromium-*" 2^>nul') do (
    if exist "%LocalAppData%\ms-playwright\%%D\chrome-win\chrome.exe" (
      set "CHROMIUM_EXE=%LocalAppData%\ms-playwright\%%D\chrome-win\chrome.exe"
      goto :eof
    )
  )
)

for /f "delims=" %%P in ('where chromium.exe 2^>nul') do (
  if not defined CHROMIUM_EXE set "CHROMIUM_EXE=%%P"
)
if defined CHROMIUM_EXE goto :eof

for %%P in (
  "%ProgramFiles%\Chromium\Application\chromium.exe"
  "%ProgramFiles(x86)%\Chromium\Application\chromium.exe"
  "%LocalAppData%\Chromium\Application\chromium.exe"
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
  "%LocalAppData%\Microsoft\Edge\Application\msedge.exe"
) do (
  if exist "%%~P" (
    rem Fallback final: evita bloquear o fluxo quando Chromium nao estiver instalado
    set "CHROMIUM_EXE=%%~P"
    goto :eof
  )
)
goto :eof

:ensure_chatgpt_profile_login
set "PROFILE_NAME=%~1"
set "PROFILE_DIR=%~f2"
set "PROFILE_HAS_HINT="
set "PROFILE_CREATED_NOW="
set "PROFILE_EMPTY="
set "PROFILE_ENTRY_COUNT=0"
set "PROFILE_CHECK_DIR="

if not exist "%PROFILE_DIR%" (
  mkdir "%PROFILE_DIR%" >nul 2>&1
  set "PROFILE_CREATED_NOW=1"
)

if not defined PROFILE_CREATED_NOW (
  for /f %%C in ('dir /a /b "%PROFILE_DIR%" 2^>nul ^| find /c /v ""') do set "PROFILE_ENTRY_COUNT=%%C"
  if "!PROFILE_ENTRY_COUNT!"=="0" set "PROFILE_EMPTY=1"
)

if defined PROFILE_CREATED_NOW (
  echo [BOOT] Perfil "!PROFILE_NAME!" criado agora; sera necessario configurar login no ChatGPT.
  goto :prompt_profile_setup
)

if defined PROFILE_EMPTY (
  echo [BOOT] Perfil "!PROFILE_NAME!" vazio; sera necessario configurar login no ChatGPT.
  goto :prompt_profile_setup
)

call :check_profile_artifacts "%PROFILE_DIR%"
call :check_profile_artifacts "%PROFILE_DIR%\Default"

for /d %%D in ("%PROFILE_DIR%\Profile *") do (
  call :check_profile_artifacts "%%~fD"
)

if defined PROFILE_HAS_HINT (
  echo [BOOT] Perfil "!PROFILE_NAME!" possui indicios de sessao ja utilizada no ChatGPT.
  goto :eof
)

:prompt_profile_setup
echo [BOOT] Perfil "!PROFILE_NAME!" sem indicios de login no ChatGPT. Abrindo Chromium para configuracao guiada...
set "INSTR_FILE=%TEMP%\chatgpt_profile_setup_!PROFILE_NAME!.html"
call :write_profile_html "!INSTR_FILE!" "!PROFILE_NAME!" "!PROFILE_DIR!"

start "" /wait "%CHROMIUM_EXE%" --user-data-dir="%PROFILE_DIR%" --new-window "file:///!INSTR_FILE:\=/!"

del /Q "!INSTR_FILE!" >nul 2>&1

echo [BOOT] Chromium do perfil "!PROFILE_NAME!" foi fechado. Prosseguindo com a inicializacao...
goto :eof

:check_profile_artifacts
set "PROFILE_CHECK_DIR=%~f1"
if not exist "!PROFILE_CHECK_DIR!" goto :eof
if defined PROFILE_HAS_HINT goto :eof

if exist "!PROFILE_CHECK_DIR!\IndexedDB\https_chatgpt.com_0.indexeddb.leveldb" set "PROFILE_HAS_HINT=1"
if exist "!PROFILE_CHECK_DIR!\IndexedDB\https_chat.openai.com_0.indexeddb.leveldb" set "PROFILE_HAS_HINT=1"
if exist "!PROFILE_CHECK_DIR!\Local Storage\leveldb" (
  findstr /S /I /M /C:"chatgpt.com" "!PROFILE_CHECK_DIR!\Local Storage\leveldb\*" >nul 2>&1 && set "PROFILE_HAS_HINT=1"
  findstr /S /I /M /C:"chat.openai.com" "!PROFILE_CHECK_DIR!\Local Storage\leveldb\*" >nul 2>&1 && set "PROFILE_HAS_HINT=1"
  findstr /S /I /M /C:"openai.com" "!PROFILE_CHECK_DIR!\Local Storage\leveldb\*" >nul 2>&1 && set "PROFILE_HAS_HINT=1"
  findstr /S /I /M /C:"auth0.openai.com" "!PROFILE_CHECK_DIR!\Local Storage\leveldb\*" >nul 2>&1 && set "PROFILE_HAS_HINT=1"
)
if exist "!PROFILE_CHECK_DIR!\Network\Cookies" (
  for %%A in ("!PROFILE_CHECK_DIR!\Network\Cookies") do (
    if %%~zA GTR 0 set "PROFILE_HAS_HINT=1"
  )
)
if exist "!PROFILE_CHECK_DIR!\Cookies" (
  for %%A in ("!PROFILE_CHECK_DIR!\Cookies") do (
    if %%~zA GTR 0 set "PROFILE_HAS_HINT=1"
  )
)
goto :eof

:write_profile_html
set "HTML_FILE=%~1"
set "HTML_PROFILE=%~2"
set "HTML_DIR=%~3"
>"%HTML_FILE%" echo ^<!doctype html^>
>>"%HTML_FILE%" echo ^<html lang="pt-BR"^>
>>"%HTML_FILE%" echo ^<head^>
>>"%HTML_FILE%" echo   ^<meta charset="utf-8" /^>
>>"%HTML_FILE%" echo   ^<title^>Configurar perfil %HTML_PROFILE% - ChatGPT Simulator^</title^>
>>"%HTML_FILE%" echo ^</head^>
>>"%HTML_FILE%" echo ^<body^>
>>"%HTML_FILE%" echo   ^<h1^>Configuracao obrigatoria do perfil "%HTML_PROFILE%"^</h1^>
>>"%HTML_FILE%" echo   ^<p^>Abra https://chatgpt.com/ nesta mesma janela.^</p^>
>>"%HTML_FILE%" echo   ^<p^>Faca login completo e aguarde a tela inicial carregar.^</p^>
>>"%HTML_FILE%" echo   ^<p^>Depois feche totalmente esta janela para o script continuar.^</p^>
>>"%HTML_FILE%" echo   ^<p^>Pasta do perfil: %HTML_DIR%^</p^>
>>"%HTML_FILE%" echo ^</body^>
>>"%HTML_FILE%" echo ^</html^>
goto :eof

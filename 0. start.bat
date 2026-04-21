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
  call :ensure_chatgpt_profile_login "analisador" "chrome_profile_analisador"
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
for %%I in (chrome.exe msedge.exe chromium.exe) do (
  for /f "delims=" %%P in ('where %%I 2^>nul') do (
    if not defined CHROMIUM_EXE set "CHROMIUM_EXE=%%P"
  )
)
if defined CHROMIUM_EXE goto :eof

for %%P in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
  "%LocalAppData%\Microsoft\Edge\Application\msedge.exe"
  "%ProgramFiles%\Chromium\Application\chromium.exe"
  "%ProgramFiles(x86)%\Chromium\Application\chromium.exe"
) do (
  if exist "%%~P" (
    set "CHROMIUM_EXE=%%~P"
    goto :eof
  )
)
goto :eof

:ensure_chatgpt_profile_login
set "PROFILE_NAME=%~1"
set "PROFILE_DIR=%~f2"
set "PROFILE_HAS_HINT="

if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%" >nul 2>&1

if exist "%PROFILE_DIR%\IndexedDB\https_chatgpt.com_0.indexeddb.leveldb" set "PROFILE_HAS_HINT=1"
if exist "%PROFILE_DIR%\IndexedDB\https_chat.openai.com_0.indexeddb.leveldb" set "PROFILE_HAS_HINT=1"
if exist "%PROFILE_DIR%\Local Storage\leveldb" (
  findstr /S /I /M /C:"chatgpt.com" "%PROFILE_DIR%\Local Storage\leveldb\*" >nul 2>&1 && set "PROFILE_HAS_HINT=1"
  findstr /S /I /M /C:"chat.openai.com" "%PROFILE_DIR%\Local Storage\leveldb\*" >nul 2>&1 && set "PROFILE_HAS_HINT=1"
)
if exist "%PROFILE_DIR%\Network\Cookies" (
  for %%A in ("%PROFILE_DIR%\Network\Cookies") do (
    if %%~zA GTR 0 set "PROFILE_HAS_HINT=1"
  )
)

if defined PROFILE_HAS_HINT (
  echo [BOOT] Perfil "!PROFILE_NAME!" possui indicios de sessao ja utilizada no ChatGPT.
  goto :eof
)

echo [BOOT] Perfil "!PROFILE_NAME!" sem indicios de login no ChatGPT. Abrindo Chromium para configuracao guiada...
set "INSTR_FILE=%TEMP%\chatgpt_profile_setup_!PROFILE_NAME!.html"
call :write_profile_html "!INSTR_FILE!" "!PROFILE_NAME!" "!PROFILE_DIR!"

start "" /wait "%CHROMIUM_EXE%" --user-data-dir="%PROFILE_DIR%" --new-window "file:///!INSTR_FILE:\=/!"

del /Q "!INSTR_FILE!" >nul 2>&1

echo [BOOT] Chromium do perfil "!PROFILE_NAME!" foi fechado. Prosseguindo com a inicializacao...
goto :eof

:write_profile_html
set "HTML_FILE=%~1"
set "HTML_PROFILE=%~2"
set "HTML_DIR=%~3"
where powershell.exe >nul 2>&1
if errorlevel 1 goto :write_profile_html_fallback

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$f=$args[0]; $profileName=$args[1]; $profileDir=$args[2];" ^
  "$html=@'" ^
"<!doctype html>" ^
"<html lang=""pt-BR"">" ^
"<head>" ^
"  <meta charset=""utf-8"" />" ^
"  <title>Configurar perfil $profileName ^| ChatGPT Simulator</title>" ^
"  <style>" ^
"    body { font-family: Arial, sans-serif; margin: 0; padding: 32px; background: #111827; color: #e5e7eb; }" ^
"    .card { max-width: 900px; margin: 0 auto; background: #1f2937; border-radius: 12px; padding: 24px; }" ^
"    h1 { margin-top: 0; color: #93c5fd; }" ^
"    code { background: #374151; padding: 2px 6px; border-radius: 4px; color: #bfdbfe; }" ^
"    li { margin: 10px 0; line-height: 1.4; }" ^
"  </style>" ^
"</head>" ^
"<body>" ^
"  <div class=""card"">" ^
"    <h1>Configuracao obrigatoria do perfil ""$profileName""</h1>" ^
"    <p>Este Chromium foi aberto somente para preparar o perfil persistente usado pelo ChatGPT Simulator.</p>" ^
"    <p>Pasta do perfil: <code>$profileDir</code></p>" ^
"    <ol>" ^
"      <li>Abra <strong>https://chatgpt.com/</strong> nesta mesma janela.</li>" ^
"      <li>Realize login completo na conta desejada para este perfil.</li>" ^
"      <li>Aguarde a pagina principal do ChatGPT carregar normalmente.</li>" ^
"      <li>Feche totalmente esta janela do Chromium para liberar a continuacao do script.</li>" ^
"    </ol>" ^
"    <p>Tipo de perfil detectado: <strong>$profileName</strong>. Se for ""default"", use a conta principal. Se for ""analisador"", use a conta dedicada do analisador quando aplicavel.</p>" ^
"  </div>" ^
"</body>" ^
"</html>" ^
"'@; [System.IO.File]::WriteAllText($f, $html, [System.Text.Encoding]::UTF8);" ^
  -- "%HTML_FILE%" "%HTML_PROFILE%" "%HTML_DIR%"
if not errorlevel 1 goto :eof

:write_profile_html_fallback
>"%HTML_FILE%" echo ^<!doctype html^>
>>"%HTML_FILE%" echo ^<html lang="pt-BR"^>
>>"%HTML_FILE%" echo ^<head^>
>>"%HTML_FILE%" echo   ^<meta charset="utf-8" /^>
>>"%HTML_FILE%" echo   ^<title^>Configurar perfil %HTML_PROFILE% ^| ChatGPT Simulator^</title^>
>>"%HTML_FILE%" echo ^</head^>
>>"%HTML_FILE%" echo ^<body^>
>>"%HTML_FILE%" echo   ^<h1^>Configuracao obrigatoria do perfil "%HTML_PROFILE%"^</h1^>
>>"%HTML_FILE%" echo   ^<p^>Abra https://chatgpt.com/, realize login e depois feche esta janela.^</p^>
>>"%HTML_FILE%" echo   ^<p^>Pasta do perfil: %HTML_DIR%^</p^>
>>"%HTML_FILE%" echo ^</body^>
>>"%HTML_FILE%" echo ^</html^>
goto :eof

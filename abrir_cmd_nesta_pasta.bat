@echo off
setlocal EnableExtensions

:: =============================================
:: Abrir CMD como Administrador e mostrar menu
:: =============================================

:: Verifica se já está em modo admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Solicitando permissao de Administrador...
    powershell -NoLogo -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
    exit /b
)

:: Pasta do .bat
set "CURRDIR=%~dp0"

:: Abre NOVA janela elevada já no menu
if /i "%~1" neq "__menu" (
    start "CMD - %CURRDIR%" /D "%CURRDIR%" "%ComSpec%" /v:on /k call "%~f0" __menu
    exit /b
)

:: =========================
:: ==        MENU        ==
:: =========================
:__menu
setlocal EnableExtensions EnableDelayedExpansion

:menu
title CMD - %CURRDIR%  [Menu de BATs]
cls
echo.
echo ==== MENU: escolha o .BAT para executar (elevado) ====
echo.

set "COUNT=0"
for /f "delims=" %%F in ('dir /b /a:-d "%CURRDIR%*.bat" 2^>nul') do (
  if /i not "%%~fF"=="%~f0" (
    set /a COUNT+=1
    set "OPT!COUNT!=%%~fF"
  )
)

if !COUNT! EQU 0 (
  echo Nao ha outros .BATs nesta pasta.
  echo.
  echo P^) Abrir prompt elevado (sair do menu)
  echo Q^) Sair
  echo.
) else (
  for /l %%I in (1,1,!COUNT!) do echo   %%I^) !OPT%%I!!
  echo.
  echo R^) Atualizar   P^) Prompt elevado (sair do menu)   Q^) Sair
  echo.
)

set "CHOICE="
set /p "CHOICE=Sua escolha: "

if /i "!CHOICE!"=="Q" exit /b
if /i "!CHOICE!"=="R" goto :menu
if /i "!CHOICE!"=="P" goto :leave_prompt

:: valida numero
set "NONNUM="
for /f "delims=0123456789" %%A in ("!CHOICE!") do set "NONNUM=%%A"
if defined NONNUM goto :bad
if not defined CHOICE goto :bad
if !CHOICE! LSS 1 goto :bad
if !CHOICE! GTR !COUNT! goto :bad

set "TARGET=!OPT%CHOICE%!"
if not defined TARGET goto :bad

echo.
echo === Executando: "!TARGET!" ===
echo.
call "!TARGET!"
echo.
echo (Concluido) Pressione qualquer tecla para voltar ao menu...
pause >nul
goto :menu

:bad
echo Opcao invalida.
timeout /t 1 >nul
goto :menu

:leave_prompt
echo.
echo Saindo do menu. O CMD elevado permanecera aberto para comandos.
echo.
exit /b

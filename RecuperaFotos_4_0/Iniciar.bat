@echo off
rem =====================================================================
rem  Recupera Fotos 4.0 - inicia a interface grafica
rem  De dois cliques neste arquivo. Nada e instalado nem alterado no seu
rem  cartao/HD: a origem e aberta somente para leitura.
rem =====================================================================
setlocal
cd /d "%~dp0"
title Recupera Fotos 4.0

rem ---- procura um Python 3 ----------------------------------------
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>nul && set "PY=python3"
if defined PY goto :versao

echo.
echo  NAO foi encontrado Python 3 neste computador.
echo.
echo  1) Baixe em https://www.python.org/downloads/windows/
echo  2) Na primeira tela do instalador, MARQUE "Add python.exe to PATH"
echo  3) Execute este arquivo de novo
echo.
pause
exit /b 1

:versao
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>nul
if not errorlevel 1 goto :pillow
echo.
echo  Este programa precisa de Python 3.9 ou mais novo (o seu e mais antigo).
echo.
pause
exit /b 1

:pillow
%PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo.
    echo  Pillow nao encontrado. Ele e opcional, mas melhora a verificacao
    echo  das imagens. Tentando instalar...
    echo.
    %PY% -m pip install --user -r requirements.txt
    echo.
    %PY% -c "import PIL" >nul 2>nul
    if errorlevel 1 echo  AVISO: seguindo sem Pillow - os verificadores proprios serao usados.
)

rem ---- abre o programa --------------------------------------------
echo.
echo  Abrindo o Recupera Fotos 4.0...
echo  Esta janela pode ser minimizada; ela mostra o registro da recuperacao.
echo.
%PY% recupera_fotos.py %*
set "ERRO=%ERRORLEVEL%"

if not "%ERRO%"=="0" (
    echo.
    echo  O programa terminou com o codigo %ERRO%.
    echo  Leia a mensagem acima; se foi um erro de parametro, o codigo e 2.
    echo.
    pause
)
endlocal
exit /b %ERRO%

@echo off
rem =====================================================================
rem  Recupera Fotos 4.0 - gera o executavel do Windows (dist\RecuperaFotos)
rem  Precisa de conexao com a internet na primeira vez (baixa o PyInstaller).
rem =====================================================================
setlocal
cd /d "%~dp0"
title Gerar EXE - Recupera Fotos 4.0

set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>nul && set "PY=python3"
if defined PY goto :pyinstaller

echo.
echo  NAO foi encontrado Python 3. Instale em https://www.python.org/downloads/
echo  marcando "Add python.exe to PATH" e execute este arquivo de novo.
echo.
pause
exit /b 1

:pyinstaller
%PY% -c "import PyInstaller" >nul 2>nul
if not errorlevel 1 goto :conferir
echo.
echo  Instalando o PyInstaller (so na primeira vez)...
echo.
%PY% -m pip install --user pyinstaller
%PY% -c "import PyInstaller" >nul 2>nul
if not errorlevel 1 goto :conferir
echo.
echo  NAO consegui instalar o PyInstaller. Verifique sua conexao e tente:
echo      %PY% -m pip install --user pyinstaller
echo.
pause
exit /b 1

:conferir
rem sem Pillow o .exe fica sem a decodificacao real das imagens
%PY% -c "import PIL" >nul 2>nul
if not errorlevel 1 goto :gerar
echo.
echo  AVISO: Pillow nao esta instalado. O .exe vai funcionar, mas sem a
echo  decodificacao real das imagens. Instale com:
echo      %PY% -m pip install --user -r requirements.txt
echo.
choice /c SN /m "Gerar mesmo assim? (S=sim, N=nao)"
if errorlevel 2 exit /b 1

:gerar
echo.
echo  Gerando o executavel (pode levar alguns minutos)...
echo.
%PY% -m PyInstaller --noconfirm --clean RecuperaFotos.spec
set "ERRO=%ERRORLEVEL%"
echo.
if not "%ERRO%"=="0" (
    echo  FALHOU com o codigo %ERRO%. Leia a saida acima.
    echo.
    pause
    exit /b %ERRO%
)

echo  PRONTO. O executavel ficou em:
echo      %~dp0dist\RecuperaFotos\RecuperaFotos.exe
echo.
echo  Copie a pasta dist\RecuperaFotos inteira (ela e portatil, nao instala nada).
echo  Para usar a linha de comando no mesmo .exe, edite RecuperaFotos.spec,
echo  troque console=False por console=True e rode este arquivo de novo.
echo.
choice /c SN /m "Testar agora? (S=sim, N=nao)"
if errorlevel 2 goto :fim
start "" "%~dp0dist\RecuperaFotos\RecuperaFotos.exe"

:fim
pause
endlocal
exit /b 0

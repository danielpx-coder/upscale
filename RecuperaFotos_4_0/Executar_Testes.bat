@echo off
rem =====================================================================
rem  Recupera Fotos 4.0 - executa TODA a suite de testes e grava o
rem  relatorio real em resultado_testes.txt
rem =====================================================================
setlocal
cd /d "%~dp0"
title Testes - Recupera Fotos 4.0

set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>nul && set "PY=python3"
if defined PY goto :rodar

echo.
echo  NAO foi encontrado Python 3. Instale em https://www.python.org/downloads/
echo  marcando "Add python.exe to PATH" e execute este arquivo de novo.
echo.
pause
exit /b 1

:rodar
echo.
echo  Rodando os testes (leva de 30 segundos a alguns minutos)...
echo.
%PY% rodar_testes.py %*
set "ERRO=%ERRORLEVEL%"

echo.
if "%ERRO%"=="0" (
    echo  TODOS OS TESTES PASSARAM.
) else (
    echo  HOUVE FALHAS. Os blocos FAIL/ERROR acima mostram qual teste e por que.
)
echo  Relatorio completo gravado em: %~dp0resultado_testes.txt
echo.
pause
endlocal
exit /b %ERRO%

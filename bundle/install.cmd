@echo off
rem ---------------------------------------------------------------------------
rem  sql-mcp-simple - offline kurulum (Windows x64, Python 3.10 - 3.14)
rem  Kullanim:  install.cmd            -> %USERPROFILE%\.sql-mcp altina kurar
rem             set SQL_MCP_HOME=D:\x  -> baska bir klasore kurmak icin
rem             set SQL_MCP_PYTHON=C:\Python312\python.exe -> belirli bir Python kullanmak icin
rem  Internet gerekmez: tum paketler wheelhouse klasorunden yuklenir.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
set "HERE=%~dp0"
if "%SQL_MCP_HOME%"=="" set "SQL_MCP_HOME=%USERPROFILE%\.sql-mcp"

rem 1) Python bul (once py launcher, sonra python)
set "PYEXE="
if defined SQL_MCP_PYTHON set "PYEXE="%SQL_MCP_PYTHON%""
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
    if not defined PYEXE (
        py -%%V -c "import sys" >nul 2>&1 && set "PYEXE=py -%%V"
    )
)
if not defined PYEXE (
    python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)" >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
    echo [HATA] Python 3.10 - 3.14 bulunamadi.
    echo        Kurumsal yazilim merkezinden veya python.org'dan Python 3.12 kurun, sonra tekrar calistirin.
    exit /b 10
)
echo [1/4] Python: %PYEXE%

rem 2) Sanal ortam
if exist "%SQL_MCP_HOME%\Scripts\python.exe" (
    echo [2/4] Mevcut kurulum guncellenecek: %SQL_MCP_HOME%
) else (
    echo [2/4] Sanal ortam olusturuluyor: %SQL_MCP_HOME%
    %PYEXE% -m venv "%SQL_MCP_HOME%"
    if errorlevel 1 ( echo [HATA] venv olusturulamadi. & exit /b 11 )
)

rem 3) Offline paket kurulumu
echo [3/4] Paketler kuruluyor (offline)...
"%SQL_MCP_HOME%\Scripts\python.exe" -m pip install --no-index --find-links "%HERE%wheelhouse" --upgrade --disable-pip-version-check --quiet sql-mcp-simple
if errorlevel 1 ( echo [HATA] Paket kurulumu basarisiz. & exit /b 12 )

rem 4) Dogrulama
echo [4/4] Dogrulama...
"%SQL_MCP_HOME%\Scripts\sql-mcp.exe" --version
if errorlevel 1 ( echo [HATA] sql-mcp calismiyor. & exit /b 13 )

echo.
echo KURULUM TAMAM
echo   Komut: %SQL_MCP_HOME%\Scripts\sql-mcp.exe
exit /b 0

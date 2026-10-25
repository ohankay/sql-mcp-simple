@echo off
rem ---------------------------------------------------------------------------
rem  sql-mcp-simple - offline kurulum (Windows x64 veya x86/32-bit, Python 3.10 - 3.14)
rem  Kullanim:  install.cmd            -> %USERPROFILE%\.sql-mcp altina kurar
rem             set SQL_MCP_HOME=D:\x  -> baska bir klasore kurmak icin
rem             set SQL_MCP_PYTHON=C:\Python312\python.exe -> belirli bir Python kullanmak icin
rem  Internet gerekmez: tum paketler wheelhouse klasorunden yuklenir.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
set "HERE=%~dp0"
if "%SQL_MCP_HOME%"=="" set "SQL_MCP_HOME=%USERPROFILE%\.sql-mcp"

rem Paketin mimarisi (build sirasinda bundle_arch.txt yazilir): x64 veya x86
set "ARCH=x64"
if exist "%HERE%bundle_arch.txt" set /p ARCH=<"%HERE%bundle_arch.txt"
if /i "%ARCH%"=="x86" (
    set "BITS=32"
    set "OTHER=x64"
    set "PKG=sql-mcp-simple"
) else (
    set "BITS=64"
    set "OTHER=x86"
    set "PKG=sql-mcp-simple[bundled-driver]"
)
set "PYCHECK=import struct,sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P')*8 == %BITS% else 1)"
set "PYANY=import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)"

rem 1) Paketle ayni mimaride (%BITS%-bit) Python bul
set "PYEXE="
if defined SQL_MCP_PYTHON (
    "%SQL_MCP_PYTHON%" -c "%PYCHECK%" >nul 2>&1 && set "PYEXE="%SQL_MCP_PYTHON%""
    if not defined PYEXE goto :wrong_python
)
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
    if not defined PYEXE (
        py -%%V-%BITS% -c "%PYCHECK%" >nul 2>&1 && set "PYEXE=py -%%V-%BITS%"
    )
)
if not defined PYEXE (
    python -c "%PYCHECK%" >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE goto :wrong_python
echo [1/4] Python: %PYEXE% (%BITS%-bit, paket: %ARCH%)

rem 2) Sanal ortam (var olan ortam farkli mimarideyse uyar)
if exist "%SQL_MCP_HOME%\Scripts\python.exe" (
    "%SQL_MCP_HOME%\Scripts\python.exe" -c "%PYCHECK%" >nul 2>&1
    if errorlevel 1 (
        echo [HATA] %SQL_MCP_HOME% icindeki mevcut kurulum farkli mimaride veya desteklenmeyen Python surumunde.
        echo        Klasoru silip tekrar calistirin ya da SQL_MCP_HOME ile baska bir klasor secin.
        exit /b 15
    )
    echo [2/4] Mevcut kurulum guncellenecek: %SQL_MCP_HOME%
) else (
    echo [2/4] Sanal ortam olusturuluyor: %SQL_MCP_HOME%
    %PYEXE% -m venv "%SQL_MCP_HOME%"
    if errorlevel 1 ( echo [HATA] venv olusturulamadi. & exit /b 11 )
)

rem 3) Offline paket kurulumu
echo [3/4] Paketler kuruluyor (offline)...
"%SQL_MCP_HOME%\Scripts\python.exe" -m pip install --no-index --find-links "%HERE%wheelhouse" --upgrade --disable-pip-version-check --quiet "%PKG%"
if errorlevel 1 ( echo [HATA] Paket kurulumu basarisiz. & exit /b 12 )

rem 4) Dogrulama
echo [4/4] Dogrulama...
"%SQL_MCP_HOME%\Scripts\sql-mcp.exe" --version
if errorlevel 1 ( echo [HATA] sql-mcp calismiyor. & exit /b 13 )

echo.
echo KURULUM TAMAM
echo   Komut: %SQL_MCP_HOME%\Scripts\sql-mcp.exe
exit /b 0

:wrong_python
rem Uygun mimaride Python yok. Baska mimaride Python var mi?
set "FOUND_OTHER="
if defined SQL_MCP_PYTHON "%SQL_MCP_PYTHON%" -c "%PYANY%" >nul 2>&1 && set "FOUND_OTHER=1"
py -c "%PYANY%" >nul 2>&1 && set "FOUND_OTHER=1"
python -c "%PYANY%" >nul 2>&1 && set "FOUND_OTHER=1"
if defined FOUND_OTHER (
    echo [HATA] Bu paket %ARCH% icin, ama bilgisayarda sadece farkli mimaride Python var.
    echo        sql-mcp-simple-windows-%OTHER%.zip paketini indirip onunla kurun
    echo        veya %BITS%-bit Python 3.10 - 3.14 kurun.
    exit /b 14
)
echo [HATA] Python 3.10 - 3.14 bulunamadi.
echo        Kurumsal yazilim merkezinden veya python.org'dan Python 3.12 kurun, sonra tekrar calistirin.
exit /b 10

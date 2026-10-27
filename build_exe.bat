@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%PYTHON%" goto dependencies
echo [1/4] Creating Python 3.12 environment...
py -3.12 -m venv .venv
if errorlevel 1 goto failed

:dependencies
echo [1/4] Installing build dependencies...
"%PYTHON%" -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
echo [2/4] Running regression tests...
"%PYTHON%" -m pytest -q
if errorlevel 1 goto failed
echo [3/4] Building standalone executable...
"%PYTHON%" -m PyInstaller --noconfirm --clean ShiftStudio.spec
if errorlevel 1 goto failed
echo [4/4] Testing the packaged executable...
"%~dp0dist\ShiftStudio.exe" --self-test
if errorlevel 1 goto failed
echo.
echo Build complete: "%~dp0dist\ShiftStudio.exe"
exit /b 0

:failed
echo.
echo BUILD FAILED. Check the errors above. An existing exe may be outdated.
exit /b 1
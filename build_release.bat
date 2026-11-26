@echo off
chcp 65001 >nul
REM One-click release build: preflight -> PyInstaller onedir -> smoke test -> Inno Setup installer.
REM Produces dist\ShootAtZombies\ and dist\installer\ShootAtZombiesSetup.exe
cd /d "%~dp0"
echo ============================================
echo  Release Build  (PyInstaller onedir)
echo ============================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Create it with:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo [1/4] Preflight check ...
"%~dp0.venv\Scripts\python.exe" packaging\build_release.py --check
if errorlevel 1 goto :failed

echo.
echo [2/4] Building PyInstaller onedir package ...
"%~dp0.venv\Scripts\python.exe" packaging\build_release.py --engine pyinstaller
if errorlevel 1 goto :failed

echo.
echo [3/4] Smoke test (real flow, production data untouched) ...
"%~dp0.venv\Scripts\python.exe" packaging\verify_release.py
if errorlevel 1 goto :failed

echo.
echo [4/4] Compile Inno Setup installer ...
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" (
  echo [ERROR] Inno Setup 6 not found. Install it with:
  echo   winget install --id JRSoftware.InnoSetup --exact
  echo.
  goto :failed
)
"%ISCC%" "%~dp0packaging\installer.iss"
if errorlevel 1 goto :failed

echo.
echo ============================================
echo  BUILD OK
echo ============================================
echo  Program:   dist\ShootAtZombies\ShootAtZombies.exe
echo  Installer: dist\installer\ShootAtZombiesSetup.exe
echo  Reminder:  bump MyAppVersion in installer.iss before release.
echo             ([WARN] about fingerprint anchor = commit it to git HEAD)
echo.
pause
exit /b 0

:failed
echo.
echo [ERROR] Build failed at the step above. Nothing was published.
echo.
pause
exit /b 1

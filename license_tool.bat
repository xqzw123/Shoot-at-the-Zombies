@echo off
chcp 65001 >nul
REM Author-side license issuer GUI. NEVER ship this file to customers.
cd /d "%~dp0"
echo ============================================
echo  License Issuer GUI  (AUTHOR ONLY)
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

echo [NOTE] Signing needs reachable NTP - it refuses to sign offline.
echo [NOTE] Private key: licensing\keys\private.json - NEVER share it.
echo.

"%~dp0.venv\Scripts\python.exe" -m licensing.keygen ui
if errorlevel 1 (
  echo.
  echo [ERROR] License issuer exited with an error (see above).
)
echo.
pause

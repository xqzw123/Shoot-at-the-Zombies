@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  Shoot-at-the-Zombies
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

"%~dp0.venv\Scripts\python.exe" game_bot.py
if errorlevel 1 (
  echo.
  echo [ERROR] game_bot.py exited with an error.
)
echo.
pause

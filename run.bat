@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PIP_CACHE_DIR=D:\github\.pip-cache"
set "TEMP=D:\github\.tmp"
set "TMP=D:\github\.tmp"
echo ============================================
echo  Shoot-at-the-Zombies  (D-drive venv)
echo ============================================
echo.
"%~dp0.venv\Scripts\python.exe" game_bot.py
if errorlevel 1 (
  echo.
  echo [ERROR] game_bot.py exited with an error.
)
echo.
pause

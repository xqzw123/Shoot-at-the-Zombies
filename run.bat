@echo off
chcp 65001 >nul
REM [DEPRECATED for release] 开发调试入口，直接跑 game_bot.py，不含授权校验。
REM 正式发布必须用 launcher.py 作为打包入口：python packaging/build_release.py
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

@echo off
REM [DEPRECATED] 本脚本产出的是无授权校验的调试包，禁止对外发布。
REM 正式发布请用：python packaging/build_release.py --engine pyinstaller
echo [DEPRECATED][WARN] 本脚本产物不含授权校验，禁止对外发布！正式发布请用 packaging\build_release.py
echo.
echo 已停止：本脚本会产出无授权校验的 dist\游戏机器人.exe，不能对外分发。
echo 正式发布请执行：.venv\Scripts\python.exe packaging\build_release.py --engine pyinstaller
echo.
pause
exit /b 1

REM === 以下为历史调试命令，保留仅供查阅，已不再执行 ===
REM 使用PyInstaller打包，--windowed表示不显示控制台窗口，--onefile表示生成单个exe文件
REM --add-data参数用于包含必要的目录，格式为 "源路径;目标路径"
REM python -m PyInstaller --windowed --onefile --name="游戏机器人" --add-data "templates;templates" game_bot.py
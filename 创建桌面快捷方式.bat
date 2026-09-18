@echo off
chcp 65001 >nul
rem ============================================================
rem  在桌面创建/修复快捷方式（带程序图标）
rem  说明：桌面若被 OneDrive 重定向（如 桌面 在 OneDrive 下），脚本会自动用真实桌面路径。
rem ============================================================
cd /d "%~dp0"
echo 正在生成图标并创建桌面快捷方式…
python tools\make_shortcut.py --name "AI 错题本"
echo.
echo 完成后回到桌面，双击「AI 错题本」即可启动软件。
echo （如果桌面图标还是旧的白色方块，按 Win 后输入 ie4uinit.exe -show 回车刷新图标缓存）
pause

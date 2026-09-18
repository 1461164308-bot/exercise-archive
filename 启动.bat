@echo off
chcp 65001 >nul
rem ============================================================
rem  AI本地错题习题归档整理工具 —— 一键启动（无控制台窗口）
rem  双击本文件即可打开软件；出错时请改用「启动-控制台.bat」看报错。
rem ============================================================
cd /d "%~dp0"

rem 优先使用 py 启动器，其次 python，最后 pythonw
where py >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 -m exerarchive
    goto :eof
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw -m exerarchive
    goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
    start "" python -m exerarchive
    goto :eof
)

echo.
echo 没有找到 Python，请先安装 Python 3.8 以上版本（安装时勾选 Add Python to PATH）。
echo 安装教程见 docs\01-安装部署教程.md
echo.
pause

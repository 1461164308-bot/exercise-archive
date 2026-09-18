@echo off
chcp 65001 >nul
rem ============================================================
rem  一键验收：把「不需要显卡」的验证全跑一遍
rem    1) 环境自检（跳过模型实测，很快）
rem    2) 67 项单元测试
rem    3) 导出格式验证（TXT/MD/HTML/Word/PDF，产物在 verify\exports）
rem    4) 云端通道验证（本地假 OpenAI 服务，真实 HTTP 往返）
rem    5) 全流程验证（CLI + 界面：导入→识别→归档→分析→搜索→导出→备份）
rem    6) 本地后端验证（Ollama 协议 / LM Studio / 纯本地不联网）
rem    7) 界面冒烟 + 六页截图（产物在 verify）
rem  想连真实识别一起验（需要显卡空闲）：
rem    python tools\verify_recognition.py
rem ============================================================
cd /d "%~dp0"
set PYTHONUTF8=1

echo.
echo ============ 1/7 环境自检 ============
python tools\selfcheck.py --no-model

echo.
echo ============ 2/7 单元测试 ============
python -m unittest discover -s tests -t . 2>&1 | findstr /C:"Ran " /C:"OK" /C:"FAILED"

echo.
echo ============ 3/7 导出格式验证 ============
python tools\verify_exports.py --no-browser 2>&1 | findstr /C:"RESULT"

echo.
echo ============ 4/7 云端通道验证 ============
python tools\verify_cloud.py 2>&1 | findstr /C:"RESULT"

echo.
echo ============ 5/7 全流程验证（CLI + 界面） ============
python tools\verify_workflow.py 2>&1 | findstr /C:"RESULT"

echo.
echo ============ 6/7 本地后端验证（Ollama / LM Studio / 纯本地） ============
python tools\verify_local_backends.py 2>&1 | findstr /C:"RESULT"

echo.
echo ============ 7/7 界面冒烟 + 截图 ============
python tools\gui_smoke.py --no-recognize 2>&1 | findstr /C:"RESULT"

echo.
echo ============ 验收结束 ============
echo 截图与导出产物在 verify\ 目录，完整结论见 verify\验收报告.md
echo 真实识别验证（需显卡空闲）：python tools\verify_recognition.py
echo.
pause

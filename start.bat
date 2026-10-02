@echo off
chcp 65001 >nul
REM ============================================================
REM  QQ 机器人合并版 · Windows 启动脚本（崩溃自动重启）
REM ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 python，请先安装 Python 3.8+ 并加入 PATH。
  pause
  exit /b 1
)

python -c "import flask, requests, websocket" >nul 2>nul
if errorlevel 1 (
  echo [提示] 缺少依赖，正在安装（flask / requests / websocket-client）...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请手动执行：python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

:loop
echo ============================================================
echo [%date% %time%] 正在启动 QQ 机器人合并版 ...
echo ============================================================
python run.py
echo.
echo [%date% %time%] 程序已退出，3 秒后自动重启（按 Ctrl+C 两次可停止）...
timeout /t 3 /nobreak >nul
goto loop

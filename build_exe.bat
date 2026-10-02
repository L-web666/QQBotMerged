@echo off
chcp 65001 >nul
REM ============================================================
REM  QQ 机器人合并版 · 一键打包成 exe（Windows）
REM  需要先安装：python -m pip install pyinstaller
REM ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 python，请先安装 Python 并加入 PATH。
  pause
  exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo [提示] 缺少 PyInstaller，正在安装...
  python -m pip install pyinstaller
  if errorlevel 1 (
    echo [错误] PyInstaller 安装失败，请手动执行：python -m pip install pyinstaller
    pause
    exit /b 1
  )
)

echo.
echo 打包类型：
echo   1 = 单文件 exe（默认，最方便分发）
echo   2 = 文件夹版（启动更快，适合长期挂机）
set /p choice=请输入 1 或 2 后回车（直接回车=1）: 

if "%choice%"=="2" (
  python build_exe.py --onedir
) else (
  python build_exe.py --onefile
)

echo.
echo 打包结束，文件在 dist\ 目录里。按任意键关闭本窗口。
pause >nul

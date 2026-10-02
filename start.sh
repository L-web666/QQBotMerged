#!/usr/bin/env bash
# ============================================================
#  QQ 机器人合并版 · Linux/macOS 启动脚本（崩溃自动重启）
# ============================================================
set -u
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[错误] 没有找到 $PY，请先安装 Python 3.8+"
  exit 1
fi

if ! "$PY" -c "import flask, requests, websocket" >/dev/null 2>&1; then
  echo "[提示] 缺少依赖，正在安装..."
  "$PY" -m pip install -r requirements.txt || {
    echo "[错误] 依赖安装失败，请手动执行：$PY -m pip install -r requirements.txt"
    exit 1
  }
fi

while true; do
  echo "============================================================"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 正在启动 QQ 机器人合并版 ..."
  echo "============================================================"
  "$PY" run.py
  code=$?
  if [ "$code" -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 程序正常退出。"
    break
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 程序异常退出（code=$code），3 秒后重启（Ctrl+C 停止）..."
  sleep 3
done
